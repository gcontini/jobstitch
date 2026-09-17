"""Payloads that are neither a job description analysis nor a log.

The rendered CV comes back as JSON rather than as a PDF body so that every
endpoint can carry the same envelope — the logs and the token spend are worth
more than saving a base64 pass over 300 KB.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class CVStatus(BaseModel):
    """Where a CV job is, and what the step before it cost.

    No history: a poll answers with the current status only. ``detail`` is
    written by the server and printed as-is, and may span several lines — the
    reviewer's complaints and the condense instruction are both quoted in it
    verbatim, because they are what was sent to the model.
    """

    status: Literal[
        "generate", "review", "re-generate", "page_check", "highlight", "END"
    ] = Field(description="The step running now")
    detail: str = Field("", description="One line about the step that just finished")


class RenderedCV(BaseModel):
    """A compiled CV: the sources, the PDF, and the data both came from."""

    tex: str = Field(description="The LaTeX source, for editing and re-rendering")
    pdf_base64: str = Field(description="The compiled PDF, base64-encoded")
    document: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "The tailored content merged with your candidate data, exactly as "
            "it was rendered. Set by GET /v1/cv/{id}; a re-render leaves it out "
            "because the caller already has it."
        ),
    )

    @classmethod
    def from_bytes(
        cls, *, tex: str, pdf: bytes, document: Optional[Dict[str, Any]] = None
    ) -> "RenderedCV":
        return cls(
            tex=tex,
            pdf_base64=base64.b64encode(pdf).decode("ascii"),
            document=document,
        )

    def pdf_bytes(self) -> bytes:
        """Decode the PDF — what a client writes to disk."""
        return base64.b64decode(self.pdf_base64)


class CoverLetter(BaseModel):
    """A generated cover letter."""

    text: str = Field(description="The letter, plain text")
    words: int = Field(description="Word count, already validated server-side")


class ServerStatus(BaseModel):
    """What ``/healthz`` reports."""

    status: str = Field(description="'ok' when the server can do its job")
    version: str = Field(description="jobstitch-server version")
    pdflatex: bool = Field(description="Whether a pdflatex binary is available")
    models: dict[str, str] = Field(description="Model name configured per role")
    auth_required: bool = Field(description="Whether a bearer token is required")


__all__ = ["CVStatus", "RenderedCV", "CoverLetter", "ServerStatus"]
