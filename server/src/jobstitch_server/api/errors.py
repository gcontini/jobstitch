"""Turning failures into a status code and a sentence.

One table, one handler, one walk: :func:`describe_failure` is the single place
that answers "what does this exception produce on the wire", so a new failure
type is added once rather than once per question asked about it. A request
that fails gets the same body shape as one that succeeds: a cause and the
request id that unlocks the full log of the attempts that led there.
"""

from __future__ import annotations

import logging
from typing import Tuple

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
#: Anything else derived from :class:`PipelineError` is a 502 — the model never
#: produced something usable, which is a gateway problem rather than ours.
_PIPELINE_STATUS = {
    LatexCompileError: 422,
    LatexTimeoutError: 504,
    BudgetExceededError: 504,
}


def _pipeline_message(exc: PipelineError) -> str:
    """``kind [stage]: message (detail)`` — the stage and the detail are what
    tell a reader which of several attempts failed, and on which file."""
    where = f" [{exc.stage}]" if exc.stage else ""
    detail = "".join(f" {key}={value}" for key, value in exc.detail.items())
    return f"{exc.kind}{where}: {exc}{f' ({detail.strip()})' if detail else ''}"


def _provider_message(exc: APIError) -> str:
    # Never echo the body: it can carry the endpoint and the key.
    kind = {
        APITimeoutError: "provider_timeout",
        APIConnectionError: "provider_unreachable",
    }.get(type(exc), "provider_error")
    return f"{kind}: the model endpoint failed: {type(exc).__name__}"


def describe_failure(exc: Exception) -> Tuple[int, str]:
    """The HTTP status and the one-line cause for a failure, decided once.

    Anything the model provider did wrong is a gateway problem, not ours: the
    server is working, the thing behind it is not. Only a genuine bug here
    earns a 500.
    """
    if isinstance(exc, ApiError):
        return exc.status, f"{exc.kind}: {exc.message}"
    if isinstance(exc, PipelineError):
        for kind, status in _PIPELINE_STATUS.items():
            if isinstance(exc, kind):
                return status, _pipeline_message(exc)
        return 502, _pipeline_message(exc)
    if isinstance(exc, ValidationError):
        return 422, "validation_error: the uploaded document does not match the expected schema"
    if isinstance(exc, APIError):
        # APITimeoutError is an APIError, but a timeout is 504 rather than 502.
        return (504 if isinstance(exc, APITimeoutError) else 502), _provider_message(exc)
    # A bare TimeoutError is still the thing behind us being slow; anything
    # left over is a bug here.
    return (504 if isinstance(exc, TimeoutError) else 500), (
        f"internal_error: {type(exc).__name__}: {exc}"
    )


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
    status = describe_failure(exc)[0]
    if status >= 500 and not isinstance(exc, (ApiError, PipelineError, APIError)):
        logger.exception("unhandled error on %s", where)
    else:
        logger.warning("%s -> %d: %s", where, status, exc)


async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """Single exception handler for the whole app."""
    log_failure(exc, request.url.path)
    status, message = describe_failure(exc)
    return failure_response(current_run().request_id, status, message)


__all__ = [
    "ApiError",
    "MissingPart",
    "BadPart",
    "PartTooLarge",
    "Unauthorized",
    "TooManyJobs",
    "UnknownRequest",
    "JobNotReady",
    "describe_failure",
    "failure_response",
    "handle_exception",
    "log_failure",
]
