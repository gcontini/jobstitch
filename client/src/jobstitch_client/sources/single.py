"""One named file as a source — ``jobstitch submit``."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from . import JDCandidate
from .folder import read_text
from ..workspace import Workspace


class SingleFileSource:
    """Yields exactly one candidate: the file you named.

    It is *copied* into ``working/``, so it follows exactly the same path as a
    watched file — same trail on failure, same folder on success — while the
    file you pointed at stays where you left it. A watched folder is an inbox
    and gets emptied; an argument is not.
    """

    def __init__(self, path: Path, workspace: Workspace) -> None:
        self.path = Path(path)
        self.workspace = workspace

    def candidates(self) -> Iterator[JDCandidate]:
        if not self.path.is_file():
            raise FileNotFoundError(f"no such file: {self.path}")
        claimed = self.workspace.take_in(self.path, move=False)
        yield JDCandidate(text=read_text(claimed), origin=claimed, label=self.path.name)


__all__ = ["SingleFileSource"]
