"""Server configuration, all of it from the environment.

Every value has a default that works, so an unconfigured container starts and
serves; the environment is for tuning and for the one secret (the token).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from jobstitch_contracts import MAX_JD_CHARS, MIN_JD_CHARS


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    """Everything the API needs to know that is not in a request."""

    #: Bearer token clients must present. Unset means auth is off.
    api_token: Optional[str] = None
    #: Parent of the per-request scratch directories.
    work_root: Path = Path(tempfile.gettempdir())
    #: Cap on any single multipart part (a profile is a few KB; a signature tens).
    max_part_bytes: int = 2_000_000
    #: Whole requests allowed to run at once; the rest get 429.
    max_concurrent_jobs: int = 10
    #: Per-compile timeout — a supplied template can loop forever otherwise.
    latex_timeout: float = 120.0
    #: Wall-clock budget for one CV run, checked between attempts.
    request_budget_seconds: float = 1200.0
    #: Generate -> review -> render rounds before giving up.
    max_attempts: int = 4
    #: Length band for the free JD pre-check.
    jd_min_chars: int = MIN_JD_CHARS
    jd_max_chars: int = MAX_JD_CHARS

    @classmethod
    def from_env(cls) -> "Settings":
        work_root = Path(os.getenv("JOBSTITCH_WORK_DIR", tempfile.gettempdir()))
        work_root.mkdir(parents=True, exist_ok=True)
        return cls(
            api_token=os.getenv("JOBSTITCH_API_TOKEN") or None,
            work_root=work_root,
            max_part_bytes=_int("JOBSTITCH_MAX_PART_BYTES", 2_000_000),
            max_concurrent_jobs=_int("JOBSTITCH_MAX_CONCURRENT_JOBS", 10),
            latex_timeout=_float("JOBSTITCH_LATEX_TIMEOUT", 120.0),
            request_budget_seconds=_float("JOBSTITCH_REQUEST_BUDGET_SECONDS", 1200.0),
            max_attempts=_int("JOBSTITCH_MAX_ATTEMPTS", 4),
            jd_min_chars=_int("JOBSTITCH_JD_MIN_CHARS", MIN_JD_CHARS),
            jd_max_chars=_int("JOBSTITCH_JD_MAX_CHARS", MAX_JD_CHARS),
        )


__all__ = ["Settings"]
