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
from dataclasses import dataclass
from threading import BoundedSemaphore
from typing import Any, Callable, Dict, Optional

from fastapi import Header, Request
from starlette.concurrency import run_in_threadpool

from ..bundle import ResourceBundle, default_bundle
from ..jobstore import LOG, JobDir, JobStore
from ..model_selector import ModelSelector, build_models
from ..observability import LOGGER_ROOT, Run, current_run
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

    @property
    def jobs(self) -> JobStore:
        """The request directories. A value over a path, not shared state."""
        return JobStore(self.settings.work_root)

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        pdflatex = shutil.which("pdflatex") is not None
        if not pdflatex:
            logger.warning(
                "  ⚠ pdflatex is not on PATH — /v1/cv and /v1/cv/render will fail"
            )
        return cls(
            settings=settings,
            models=build_models(),
            bundle=default_bundle(),
            job_slots=BoundedSemaphore(settings.max_concurrent_jobs),
            pdflatex=pdflatex,
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


def claim_slot(state: AppState) -> None:
    """Take one of the job slots, or refuse.

    A full server answers 429 immediately rather than queueing a caller for
    minutes. Released by whoever claimed it — for a CV job, by the worker
    thread, which is why this is not a context manager.
    """
    if not state.job_slots.acquire(blocking=False):
        raise TooManyJobs(
            f"all {state.settings.max_concurrent_jobs} job slots are busy; retry shortly"
        )


def save_log(job: JobDir, run: Run) -> None:
    """Persist what a request said about itself, for ``GET /logs/{id}``."""
    job.write(LOG, [entry.model_dump(mode="json") for entry in run.logs()])


async def execute(
    state: AppState, fn: Callable[..., Any], *, work_dir: bool = False
) -> Any:
    """Run one blocking pipeline job off the event loop.

    The CV endpoint does not come through here — it answers before its job is
    done (see :mod:`jobstitch_server.api.jobs`). What is left are the short
    ones, which still want the concurrency limit, the scratch directory and
    their log on disk afterwards.
    """
    claim_slot(state)
    run = current_run()
    jobs = state.jobs
    job = jobs.open(run.request_id) or jobs.create(run.request_id)
    try:
        return await (run_in_threadpool(fn, job.work) if work_dir else run_in_threadpool(fn))
    finally:
        job.drop_work()
        save_log(job, run)
        state.job_slots.release()


def envelope_for(request_id: str, data: Any) -> Dict[str, Any]:
    """Wrap a successful payload under the id it belongs to."""
    return {"request_id": request_id, "ok": True, "data": data}


def envelope_of(data: Any) -> Dict[str, Any]:
    """Wrap a successful payload. The commentary is fetched separately."""
    return envelope_for(current_run().request_id, data)


__all__ = [
    "AppState",
    "get_state",
    "require_token",
    "claim_slot",
    "save_log",
    "execute",
    "envelope_for",
    "envelope_of",
]
