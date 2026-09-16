"""What every request needs: the shared state, the token check, a job slot.

The state is built once at startup and never mutated: three model clients, the
default resource bundle, and one semaphore. Sharing a
:class:`~jobstitch_server.model_selector.ModelSelector` across requests is
safe — after construction it is read-only and its HTTP client is thread-safe.
"""

from __future__ import annotations

import hmac
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any, Callable, Dict, Optional

from fastapi import Header, Request
from starlette.concurrency import run_in_threadpool

from ..bundle import ResourceBundle, default_bundle
from ..logstore import LogStore
from ..model_selector import ModelSelector, build_models
from ..observability import LOGGER_ROOT, current_run
from .errors import TooManyJobs, Unauthorized
from .settings import Settings

logger = logging.getLogger(f"{LOGGER_ROOT}.api")


@dataclass
class AppState:
    """Built once in the lifespan, read-only afterwards."""

    settings: Settings
    models: Dict[str, ModelSelector]
    bundle: ResourceBundle
    job_slots: BoundedSemaphore
    pdflatex: bool
    #: Finished requests' log lines, for GET /logs/{request_id}.
    logs: LogStore = field(default_factory=LogStore.from_env)

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        bundle = default_bundle()
        models = build_models()
        pdflatex = shutil.which("pdflatex") is not None
        if not pdflatex:
            logger.warning(
                "  ⚠ pdflatex is not on PATH — /v1/cv and /v1/cv/render will fail"
            )
        return cls(
            settings=settings,
            models=models,
            bundle=bundle,
            job_slots=BoundedSemaphore(settings.max_concurrent_jobs),
            pdflatex=pdflatex,
            logs=LogStore.from_env(),
        )


def get_state(request: Request) -> AppState:
    """The app state, for a route to depend on."""
    return request.app.state.jobstitch


def require_token(
    request: Request, authorization: Optional[str] = Header(None)
) -> None:
    """Bearer check. No token configured means the server is open on purpose."""
    expected = get_state(request).settings.api_token
    if not expected:
        return
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), expected):
        raise Unauthorized("a valid bearer token is required")


async def execute(
    state: AppState, fn: Callable[..., Any], *, work_dir: bool = False
) -> Any:
    """Run one blocking pipeline job off the event loop.

    Three things happen here and nowhere else: the concurrency limit (a full
    server says 429 immediately rather than queueing a caller for minutes),
    the hop into the threadpool (which carries the contextvars, so the job's
    log lines reach this request's envelope), and the scratch directory's
    lifetime.
    """
    if not state.job_slots.acquire(blocking=False):
        raise TooManyJobs(
            f"all {state.settings.max_concurrent_jobs} job slots are busy; retry shortly"
        )
    try:
        if not work_dir:
            return await run_in_threadpool(fn)
        work = Path(tempfile.mkdtemp(prefix="jobstitch-", dir=state.settings.work_root))
        try:
            return await run_in_threadpool(fn, work)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        state.job_slots.release()


def envelope_of(data: Any) -> Dict[str, Any]:
    """Wrap a successful payload. The commentary is fetched separately."""
    return {"request_id": current_run().request_id, "ok": True, "data": data}


__all__ = ["AppState", "get_state", "require_token", "execute", "envelope_of"]
