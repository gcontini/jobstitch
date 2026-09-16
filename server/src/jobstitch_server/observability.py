"""What the server reports about a run: its log lines and its token spend.

Responses say what happened, not how. The commentary — including the line
each model call writes with its duration and token counts — is collected here
per request and kept in :mod:`jobstitch_server.logstore` for
``GET /logs/{request_id}`` to serve.

One piece does the collecting: a :class:`logging.Handler` that copies every
record emitted during a request into that request's :class:`Run`. Pipeline
code keeps calling ``logger.info(...)`` and never imports this module.

The active run is found through a :class:`~contextvars.ContextVar`, which is
what makes it work under ``run_in_threadpool``: anyio copies the context into
the worker thread, so a line emitted deep inside the CV loop still lands in
the right request's log. Outside a request — a test, a REPL — the default is
:data:`NULL_RUN`, which discards.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterator, List, Optional

from jobstitch_contracts import LogEntry

#: Root logger name; everything in the server logs under it.
LOGGER_ROOT = "jobstitch_server"

#: Bounds, so one pathological run cannot produce an unbounded response body.
MAX_LOG_ENTRIES = 2000
MAX_MESSAGE_CHARS = 4000


class Run:
    """The log lines and LLM calls of one request."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.started = time.monotonic()
        self._logs: List[LogEntry] = []
        self._dropped = 0

    # --- collection (called by the handler and by ModelSelector) -----------
    def add_log(self, level: str, stage: str, message: str) -> None:
        if len(self._logs) >= MAX_LOG_ENTRIES:
            self._dropped += 1
            return
        self._logs.append(
            LogEntry(
                ts=datetime.now(timezone.utc),
                level=level,
                stage=stage,
                message=message[:MAX_MESSAGE_CHARS],
            )
        )

    # --- readout (called when the request ends) ----------------------------
    def logs(self) -> List[LogEntry]:
        if self._dropped:
            return self._logs + [
                LogEntry(
                    ts=datetime.now(timezone.utc),
                    level="WARNING",
                    stage="run",
                    message=f"{self._dropped} further log lines dropped (cap {MAX_LOG_ENTRIES})",
                )
            ]
        return list(self._logs)

    def elapsed(self) -> float:
        return time.monotonic() - self.started


class NullRun(Run):
    """Discards everything. The default outside a request."""

    def __init__(self) -> None:
        super().__init__(request_id="-")

    def add_log(self, level: str, stage: str, message: str) -> None:
        return None


NULL_RUN = NullRun()

_run: ContextVar[Run] = ContextVar("jobstitch_run", default=NULL_RUN)
_stage: ContextVar[str] = ContextVar("jobstitch_stage", default="-")
_attempt: ContextVar[int] = ContextVar("jobstitch_attempt", default=1)


def current_run() -> Run:
    return _run.get()


def current_stage() -> str:
    return _stage.get()


def current_attempt() -> int:
    return _attempt.get()


@contextmanager
def use_run(run: Run) -> Iterator[Run]:
    """Make ``run`` the active one for this context (and any thread it hands
    work to through anyio)."""
    token = _run.set(run)
    try:
        yield run
    finally:
        _run.reset(token)


@contextmanager
def stage(name: str, attempt: int = 1) -> Iterator[None]:
    """Label everything logged or called inside as belonging to ``name``."""
    tokens = (_stage.set(name), _attempt.set(attempt))
    try:
        yield
    finally:
        _stage.reset(tokens[0])
        _attempt.reset(tokens[1])


class RunCollectingHandler(logging.Handler):
    """Copies each record into the active :class:`Run`."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            current_run().add_log(record.levelname, current_stage(), record.getMessage())
        except Exception:  # logging must never break the request
            self.handleError(record)


class _RequestIdFilter(logging.Filter):
    """Adds ``%(request_id)s`` to every record, for the stdout format."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_run().request_id
        return True


_configured = False


def configure_logging(level: Optional[str] = None) -> None:
    """Install stdout logging + the run collector. Idempotent.

    Called by the API's lifespan and by the ``jobstitch-api`` entry point;
    importing the package installs nothing.
    """
    global _configured
    if _configured:
        return
    _configured = True

    logger = logging.getLogger(LOGGER_ROOT)
    logger.setLevel(level or os.getenv("JOBSTITCH_LOG_LEVEL", "INFO").upper())
    logger.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s")
    )
    stream.addFilter(_RequestIdFilter())
    logger.addHandler(stream)
    logger.addHandler(RunCollectingHandler())


__all__ = [
    "LOGGER_ROOT",
    "Run",
    "NULL_RUN",
    "configure_logging",
    "current_run",
    "current_stage",
    "stage",
    "use_run",
]
