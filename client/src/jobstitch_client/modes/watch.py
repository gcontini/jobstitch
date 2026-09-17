"""``jobstitch watch`` — watch a folder for job descriptions."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Config
from ..sources.folder import FolderWatchSource
from ..ui import fail, warn
from . import Session


def run(config: Config, inbox: Optional[Path], out: Path, *, assume_yes: bool = False,
        track: bool = True) -> int:
    if inbox is None:
        inbox = Path.cwd() / "incoming"
        warn(f"no --in given, watching {inbox}")
        inbox.mkdir(exist_ok=True)
    else:
        inbox = Path(inbox).expanduser().resolve()
        if not inbox.is_dir():
            fail(f"input folder does not exist: {inbox}")

    session = Session.build(config, out, assume_yes=assume_yes, track=track)
    if session.workspace.root == inbox:
        fail("the input folder and the output folder must be different")
    session.recover()
    print(f"👀 watching {inbox}. Output: {session.workspace.root}", flush=True)
    print("   Drop a job description in. Ctrl+C to stop.", flush=True)
    try:
        return session.drain(FolderWatchSource(inbox, session.workspace))
    except KeyboardInterrupt:
        print("\n⏹ stopped. Anything in working/ resumes at the next start.", flush=True)
        return 0
