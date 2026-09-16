"""Where job descriptions come from.

The four modes differ in exactly one thing: what hands them the next posting.
That difference lives here, behind :class:`JDSource`, so ``clipboard``,
``watch`` and ``submit`` share one pipeline instead of three copies of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Protocol


@dataclass(frozen=True)
class JDCandidate:
    """One thing that might be a job description.

    ``origin`` is the file it arrived as, already claimed into ``working/``
    when there is one; the clipboard has none, so its text is written out
    later under :data:`~jobstitch_client.workspace.JD_FILENAME`.
    """

    text: str
    origin: Optional[Path] = None
    label: str = "clipboard"

    @property
    def filename(self) -> str:
        from ..workspace import JD_FILENAME, strip_timestamp

        return strip_timestamp(self.origin.name) if self.origin else JD_FILENAME


class JDSource(Protocol):
    """Yields candidates until it has no more (or forever, for a watcher)."""

    def candidates(self) -> Iterator[JDCandidate]: ...


__all__ = ["JDCandidate", "JDSource"]
