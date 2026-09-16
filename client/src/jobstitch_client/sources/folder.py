"""A watched folder as a source of job descriptions."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from . import JDCandidate
from ..workspace import Workspace

POLL_SECONDS = 2.0
#: A file whose size stopped changing for this long is finished being written.
SETTLE_SECONDS = 1.0


def read_text(path: Path) -> str:
    """Read whatever is there. Binary payloads are rejected downstream, so
    they must survive the read rather than raise."""
    return path.read_bytes().decode("utf-8", errors="replace")


class FolderWatchSource:
    """Yields every file dropped into ``inbox``, claiming it immediately.

    Claiming first is the whole trick: the file leaves the watched folder the
    moment it is seen, so nothing is ever processed twice and a crash leaves
    it in ``working/`` where the recovery prompt can find it.
    """

    def __init__(
        self,
        inbox: Path,
        workspace: Workspace,
        *,
        poll_seconds: float = POLL_SECONDS,
        settle_seconds: float = SETTLE_SECONDS,
        once: bool = False,
    ) -> None:
        self.inbox = Path(inbox)
        self.workspace = workspace
        self.poll_seconds = poll_seconds
        self.settle_seconds = settle_seconds
        self.once = once

    def candidates(self) -> Iterator[JDCandidate]:
        while True:
            for path in sorted(self.inbox.iterdir()) if self.inbox.is_dir() else []:
                if not path.is_file() or path.name.startswith("."):
                    continue
                if not self._settled(path):
                    continue
                claimed = self.workspace.take_in(path)
                print(f"📥 {path.name} -> working/{claimed.name}", flush=True)
                yield JDCandidate(text=read_text(claimed), origin=claimed, label=path.name)
            if self.once:
                return
            time.sleep(self.poll_seconds)

    def _settled(self, path: Path) -> bool:
        """Still being copied in? Let it finish."""
        try:
            first = path.stat().st_size
            time.sleep(self.settle_seconds)
            return path.is_file() and path.stat().st_size == first
        except OSError:
            return False


__all__ = ["FolderWatchSource"]
