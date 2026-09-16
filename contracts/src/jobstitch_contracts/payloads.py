"""Payloads that are neither CV data nor JD analysis: what render and letter
return.

The rendered CV comes back as JSON rather than as a PDF body so that every
endpoint can carry the same envelope — the logs and the token spend are worth
more than saving a base64 pass over 300 KB.
"""

from __future__ import annotations

import base64

from pydantic import BaseModel, Field


class RenderedCV(BaseModel):
    """A compiled CV: the LaTeX that was compiled and the PDF it produced."""

    tex: str = Field(description="The LaTeX source, for editing and re-rendering")
    pdf_base64: str = Field(description="The compiled PDF, base64-encoded")
    pages: int = Field(description="Page count of the compiled PDF")
    advice: str = Field(description="'length OK', or what to cut if it is too long")

    @classmethod
    def from_bytes(cls, *, tex: str, pdf: bytes, pages: int, advice: str) -> "RenderedCV":
        return cls(tex=tex, pdf_base64=base64.b64encode(pdf).decode("ascii"),
                   pages=pages, advice=advice)

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


__all__ = ["RenderedCV", "CoverLetter", "ServerStatus"]
