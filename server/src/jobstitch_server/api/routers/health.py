"""Liveness and configuration readout — the one route with no auth."""

from __future__ import annotations

from fastapi import APIRouter, Request
from jobstitch_contracts import Envelope, ServerStatus

from ..deps import envelope_of, get_state

router = APIRouter()


@router.get("/healthz", response_model=Envelope[ServerStatus], response_model_exclude_none=True)
async def healthz(request: Request) -> dict:
    """What this server is and whether it can do its job.

    Unauthenticated on purpose: a platform health check has no token, and
    nothing here is a secret — model names and whether LaTeX is installed.
    """
    state = get_state(request)
    from importlib.metadata import version

    try:
        server_version = version("jobstitch-server")
    except Exception:  # running from a source tree that was never installed
        server_version = "unknown"

    return envelope_of(
        ServerStatus(
            status="ok" if state.pdflatex else "degraded",
            version=server_version,
            pdflatex=state.pdflatex,
            models={role: model.model for role, model in state.models.items()},
            auth_required=bool(state.settings.api_token),
        )
    )
