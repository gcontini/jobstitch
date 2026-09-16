"""Turning failures into a status code and an envelope.

One table, one handler. A request that fails gets the same body shape as one
that succeeds: a cause, a stage, and the request id that unlocks the full log
of the attempts that led there.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from jobstitch_contracts import Envelope, ErrorInfo
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

    def __init__(self, message: str, *, detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


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


class UnsupportedVersion(ApiError):
    status = 409
    kind = "unsupported_version"


class TooManyJobs(ApiError):
    status = 429
    kind = "too_many_jobs"


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


def error_info(exc: Exception) -> ErrorInfo:
    """Describe the failure without ever echoing a key or a local path."""
    if isinstance(exc, ApiError):
        return ErrorInfo(type=exc.kind, message=exc.message, detail=exc.detail or None)
    if isinstance(exc, PipelineError):
        return ErrorInfo(
            type=exc.kind, message=str(exc), stage=exc.stage, detail=exc.detail or None
        )
    if isinstance(exc, ValidationError):
        return ErrorInfo(
            type="validation_error",
            message="the uploaded document does not match the expected schema",
            detail={"errors": exc.errors(include_url=False)[:20]},
        )
    if isinstance(exc, APIError):
        # Never echo the body: it can carry the endpoint and the key.
        kind = {
            APITimeoutError: "provider_timeout",
            APIConnectionError: "provider_unreachable",
        }.get(type(exc), "provider_error")
        return ErrorInfo(
            type=kind,
            message=f"the model endpoint failed: {type(exc).__name__}",
            detail={"status": getattr(exc, "status_code", None)}
            if getattr(exc, "status_code", None)
            else None,
        )
    return ErrorInfo(type="internal_error", message=f"{type(exc).__name__}: {exc}")


def error_response(exc: Exception) -> JSONResponse:
    """The failure envelope: what went wrong, and the id to look up why.

    The attempts that led here are in this request's log, which a client
    fetches from ``/logs/{request_id}`` — every failure is worth reading, and
    most successes are not.
    """
    run = current_run()
    status = status_for(exc)
    envelope = Envelope[Any](request_id=run.request_id, ok=False, error=error_info(exc))
    headers = {"X-Request-Id": run.request_id}
    if status == 429:
        headers["Retry-After"] = "30"
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


async def handle_exception(request: Request, exc: Exception) -> JSONResponse:
    """Single exception handler for the whole app."""
    status = status_for(exc)
    if status >= 500 and not isinstance(exc, (ApiError, PipelineError, APIError)):
        logger.exception("unhandled error on %s", request.url.path)
    else:
        logger.warning("%s -> %d: %s", request.url.path, status, exc)
    return error_response(exc)


__all__ = [
    "ApiError",
    "MissingPart",
    "BadPart",
    "PartTooLarge",
    "Unauthorized",
    "UnsupportedVersion",
    "TooManyJobs",
    "error_response",
    "handle_exception",
    "status_for",
]
