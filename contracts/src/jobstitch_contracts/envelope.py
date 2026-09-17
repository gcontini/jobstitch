"""The response envelope every endpoint returns, success or failure.

Deliberately small: what you asked for, or why you cannot have it. The
running commentary — including one line per model call with its token spend —
is kept out of the response and fetched separately, by request id, from
``GET /logs/{request_id}``. Most callers never want it; the ones that do want
it after something went wrong.

Every response carries its ``request_id``, and so does the ``X-Request-Id``
header, which is the handle for that lookup.
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class LogEntry(BaseModel):
    """One line of the server's account of a request.

    Model calls appear here like anything else: the line for a completed call
    carries its duration and its token counts. There is no separate usage
    ledger to keep in step with the log.
    """

    ts: datetime = Field(description="When the line was emitted")
    level: str = Field(description="INFO | WARNING | ERROR")
    stage: str = Field(description="Pipeline stage, e.g. 'cv.generate' or 'render'")
    message: str = Field(description="The message itself")


class Envelope(BaseModel, Generic[T]):
    """Uniform response body. ``data`` is present iff ``ok`` is true."""

    request_id: str = Field(description="Echoed in X-Request-Id; the handle for /logs")
    ok: bool = Field(description="True when 'data' holds a result")
    data: Optional[T] = Field(None, description="The endpoint's payload")
    error: Optional[str] = Field(None, description="Why it failed. Set iff ok is false")


class RequestLog(BaseModel):
    """What ``GET /logs/{request_id}`` returns."""

    request_id: str = Field(description="The request these lines belong to")
    entries: List[LogEntry] = Field(description="In the order they were emitted")


__all__ = ["LogEntry", "Envelope", "RequestLog"]
