"""Fetching what a request did.

Separate from the response on purpose: a caller that only wants a CV should
not have to carry twenty log lines to get it, and a caller debugging a failure
wants all of them. The request id in every response — and in the
``X-Request-Id`` header — is the handle.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from jobstitch_contracts import Envelope, RequestLog

from ...jobstore import LOG
from ..deps import envelope_for, get_state
from ..errors import UnknownRequest

router = APIRouter()


@router.get("/logs/{request_id}", response_model=Envelope[RequestLog],
            response_model_exclude_none=True)
async def request_log(request: Request, request_id: str) -> dict:
    """Every line one request produced, model calls and their cost included.

    Read from the request's directory, so a job that is still running answers
    with what it has said so far. A 404 means the directory is gone — pruned,
    never created because the request failed before any work started, or made
    by another instance.
    """
    job = get_state(request).jobs.open(request_id)
    entries = job.read(LOG) if job is not None else None
    if entries is None:
        raise UnknownRequest(
            f"no log kept for request {request_id} — it is too old, it produced "
            "nothing, or it was served by another instance"
        )
    return envelope_for(request_id, {"request_id": request_id, "entries": entries})
