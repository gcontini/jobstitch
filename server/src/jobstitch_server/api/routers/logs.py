"""Fetching what a request did.

Separate from the response on purpose: a caller that only wants a CV should
not have to carry twenty log lines to get it, and a caller debugging a failure
wants all of them. The request id in every response — and in the
``X-Request-Id`` header — is the handle.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from jobstitch_contracts import Envelope, RequestLog

from ..deps import envelope_of, get_state
from ..errors import ApiError

router = APIRouter()


class UnknownRequest(ApiError):
    status = 404
    kind = "unknown_request"


@router.get("/logs/{request_id}", response_model=Envelope[RequestLog],
            response_model_exclude_none=True)
async def request_log(request: Request, request_id: str) -> dict:
    """Every line one request produced, model calls and their cost included.

    Kept in memory for the last few hundred requests (see
    :mod:`jobstitch_server.logstore`), so a 404 here means "too old, or a
    different instance", not "that never happened".
    """
    entries = get_state(request).logs.get(request_id)
    if entries is None:
        raise UnknownRequest(
            f"no log kept for request {request_id} — it is too old, it produced "
            "nothing, or it was served by another instance"
        )
    return envelope_of(RequestLog(request_id=request_id, entries=entries))
