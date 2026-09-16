"""Failures the pipeline raises, so the API can map them without guessing.

Each one names a stage and carries enough context for the client's ``log.log``
to say what went wrong. Anything not listed here reaching the API layer is an
unexpected bug and is reported as such.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class PipelineError(RuntimeError):
    """Base: a stage failed for a reason worth reporting verbatim."""

    kind = "pipeline"

    def __init__(
        self, message: str, *, stage: Optional[str] = None, detail: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.detail = detail or {}


class LatexCompileError(PipelineError):
    """pdflatex refused the document (usually a bad template or bad escaping)."""

    kind = "latex_compile"


class LatexTimeoutError(PipelineError):
    """pdflatex ran past its timeout — a loop in a supplied template, normally."""

    kind = "latex_timeout"


class ModelOutputError(PipelineError):
    """The model never produced something valid, after every retry."""

    kind = "model_output"


class BudgetExceededError(PipelineError):
    """The run hit its wall-clock budget between attempts."""

    kind = "budget_exceeded"


__all__ = [
    "PipelineError",
    "LatexCompileError",
    "LatexTimeoutError",
    "ModelOutputError",
    "BudgetExceededError",
]
