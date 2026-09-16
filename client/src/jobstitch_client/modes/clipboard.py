"""``jobstitch clipboard`` — watch the clipboard, one posting at a time."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..sources.clipboard import NO_CLIPBOARD_HINT, ClipboardSource, clipboard_error
from ..ui import fail
from . import Session


def run(config: Config, out: Path, *, assume_yes: bool = False, track: bool = True) -> int:
    problem = clipboard_error()
    if problem:
        fail(f"{problem}\n{NO_CLIPBOARD_HINT}")

    session = Session.build(config, out, assume_yes=assume_yes, track=track)
    session.recover()
    print(f"📋 watching the clipboard. Output: {session.workspace.root}", flush=True)
    print("   Copy a job posting to start. Ctrl+C to stop.", flush=True)
    try:
        return session.drain(ClipboardSource())
    except KeyboardInterrupt:
        print("\n⏹ stopped.", flush=True)
        return 0
