"""``jobstitch submit`` — one file, one CV, then exit."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Config
from ..sources.single import SingleFileSource
from ..ui import fail
from . import Session


def run(config: Config, jd_file: Path, out: Path, *, assume_yes: bool = False,
        track: bool = True, resume: Optional[str] = None) -> int:
    jd_file = Path(jd_file).expanduser()
    if not jd_file.is_file():
        fail(f"no such file: {jd_file}")

    # Single shot: no recovery prompt. Whatever a previous run left behind is
    # its own business, and a one-off run must not start by asking about it.
    session = Session.build(config, out, assume_yes=assume_yes, track=track, resume=resume)
    return session.drain(SingleFileSource(jd_file, session.workspace))
