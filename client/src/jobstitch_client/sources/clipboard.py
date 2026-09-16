"""The clipboard as a source of job descriptions."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Iterator, Optional

import pyperclip
from jobstitch_contracts import static_jd_guess

from . import JDCandidate

POLL_SECONDS = 2.0

# pyperclip.paste() returns "" both for an empty clipboard and for a helper
# that never reached the display (it discards xclip's stderr), so the loop
# below cannot tell "nothing copied yet" from "there is no clipboard here":
# it waits forever for a change that can never arrive. Ask the helper itself,
# once, at startup. These are how the helpers say the display is unreachable;
# an *empty* clipboard fails differently ("target STRING not available",
# "No selection") and is not a problem.
UNREACHABLE_DISPLAY = ("open display", "wayland")

NO_CLIPBOARD_HINT = """  On Linux the clipboard needs a helper: apt install xclip (X11) or
  wl-clipboard (Wayland), and a reachable DISPLAY/WAYLAND_DISPLAY.
  Over SSH, forward X11 (ssh -X) or use `jobstitch watch` instead, which
  needs no clipboard at all."""


def clipboard_error() -> Optional[str]:
    """Return why the clipboard cannot be read, or None when it can be.

    The helper and the order are pyperclip's own (``determine_clipboard``),
    so what is probed here is what ``pyperclip.paste()`` actually runs.
    """
    if os.name == "nt" or os.uname().sysname == "Darwin":
        return None  # both have a clipboard without a helper binary
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        probe = ["wl-paste", "--no-newline"]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        probe = ["xclip", "-selection", "c", "-o"]
    elif os.environ.get("DISPLAY") and shutil.which("xsel"):
        probe = ["xsel", "-b", "-o"]
    else:
        return (
            "no clipboard here: neither DISPLAY nor WAYLAND_DISPLAY names a "
            "display with a helper installed (xclip, xsel, wl-clipboard)"
        )

    try:
        done = subprocess.run(probe, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{probe[0]} could not be run: {exc}"

    stderr = (done.stderr or "").strip()
    if done.returncode != 0 and any(
        phrase in stderr.lower() for phrase in UNREACHABLE_DISPLAY
    ):
        return f"{probe[0]} cannot reach the display: {stderr}"
    return None


class ClipboardSource:
    """Yields the clipboard whenever it changes into something plausible.

    The free structural check runs here so that copying a password or a line
    of code never reaches the network; deciding whether plausible text really
    is a posting is the server's job.
    """

    def __init__(self, poll_seconds: float = POLL_SECONDS) -> None:
        self.poll_seconds = poll_seconds

    def candidates(self) -> Iterator[JDCandidate]:
        seen = ""
        while True:
            current = (pyperclip.paste() or "").strip()
            if current and current != seen:
                seen = current
                if static_jd_guess(current):
                    yield JDCandidate(text=current, label="clipboard")
                else:
                    print(f"📋 clipboard: not a posting "
                          f"({len(current)} chars, nothing sent)", flush=True)
            time.sleep(self.poll_seconds)


__all__ = ["ClipboardSource", "clipboard_error", "NO_CLIPBOARD_HINT"]
