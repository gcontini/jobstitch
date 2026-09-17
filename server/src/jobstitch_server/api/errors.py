"""Turning failures into a status code and a sentence.

One table, one handler. A request that fails gets the same body shape as one
that succeeds: a cause and the request id that unlocks the full log of the
attempts that led there. The cause is one string — the detail that would not
fit in it is in the log, which is where a reader who wants it is going anyway.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from jobstitch_contracts import Envelope
from openai import APIConnectionError, APIError, APITimeoutError
from pydantic import ValidationError

from ..observability import LOGGER_ROOT, current_run
from ..pipeline.errors import (
    BudgetExceededError,
    LatexCompileError,
    LatexTimeoutError,
    ModelOutputError,
    PipelineError,
)

logger = logging.getLogger(f"{LOGGER_ROOT}.api")


class ApiError(Exception):
    """A request the server refuses before any work starts."""

    status = 400
    kind = "bad_request"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MissingPart(ApiError):
    kind = "missing_part"


class BadPart(ApiError):
    kind = "bad_part"


class PartTooLarge(ApiError):
    status = 413
    kind = "part_too_large"


class Unauthorized(ApiError):
    status = 401
    kind = "unauthorized"


class TooManyJobs(ApiError):
    status = 429
    kind = "too_many_jobs"


class UnknownRequest(ApiError):
    status = 404
    kind = "unknown_request"


class JobNotReady(ApiError):
    status = 409
    kind = "job_not_ready"


#: Pipeline failures that are the client's or the provider's fault, not a bug.
_PIPELINE_STATUS = {
    LatexCompileError: 422,
    LatexTimeoutError: 504,
    BudgetExceededError: 504,
    ModelOutputError: 502,
}


def status_for(exc: Exception) -> int:
    """HTTP status for an exception that escaped a handler.

    Anything the model provider did wrong is a gateway problem, not ours: the
    server is working, the thing behind it is not. Only a genuine bug here
    earns a 500.
    """
    if isinstance(exc, ApiError):
        return exc.status
    for kind, status in _PIPELINE_STATUS.items():
        if isinstance(exc, kind):
            return status
    if isinstance(exc, PipelineError):
        return 502
    if isinstance(exc, ValidationError):
        return 422
    if isinstance(exc, (APITimeoutError, TimeoutError)):
        return 504
    if isinstance(exc, APIError):
        return 502
    return 500


def error_message(exc: Exception) -> str:
    """One line for the failure, never echoing a key or a local path."""
    if isinstance(exc, ApiError):
        return f"{exc.kind}: {exc.message}"
    if isinstance(exc, PipelineError):
        where = f" [{exc.stage}]" if exc.stage else ""
        return f"{exc.kind}{where}: {exc}"
    if isinstance(exc, ValidationError):
        return "validation_error: the uploaded document does not match the expected schema"
    if isinstance(exc, APIError):
        # Never echo the body: it can carry the endpoint and the key.
        kind = {
            APITimeoutError: "provider_timeout",
            APIConnectionError: "provider_unreachable",
        }.get(type(exc), "provider_error")
        return f"{kind}: the model endpoint failed: {type(exc).__name__}"
    return f"internal_error: {type(exc).__name__}: {exc}"


def failure_response(request_id: str, status: int, message: str) -> JSONResponse:
    """The failure envelope for one request id.

    Takes the id rather than reading the current one, because a poll reporting
    a job's failure must carry the *job's* id, not the poll's.
    """
    envelope = Envelope[str](request_id=request_id, ok=False, error=message)
    headers = {"X-Request-Id": request_id}
    if status == 429:
        headers["Retry-After"] = "30"
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


def log_failure(exc: Exception, where: str) -> None:
    """Log a failure the way the handler does: a traceback only for a real bug."""
    status = status_for(exc)
    if status >= 500 and not isinstance(exc, (ApiError, PipelineError, APIError)):
        logger.exception("unhandled error on %s", where)
    else:
        logger.warning("%s -> %d: %s", where, status, exc)


async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """Single exception handler for the whole app."""
    log_failure(exc, request.url.path)
    return failure_response(
        current_run().request_id, status_for(exc), error_message(exc)
    )


__all__ = [
    "ApiError",
    "MissingPart",
    "BadPart",
    "PartTooLarge",
    "Unauthorized",
    "TooManyJobs",
    "UnknownRequest",
    "JobNotReady",
    "failure_response",
    "handle_exception",
    "log_failure",
    "status_for",
    "error_message",
]
