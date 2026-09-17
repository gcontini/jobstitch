"""``log.log``: what happened to one job.

The client's own steps always; the server's account of each request when it
is worth having — which is on failure, or whenever you ask with ``--debug``.
The server's lines include one per model call with its duration and token
counts, so the bill is readable without a provider dashboard.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import List, Sequence

from jobstitch_contracts import LogEntry


def _header(request_id: str, count: int) -> str:
    """One request's banner, the same in ``log.log`` and on stdout."""
    return f"--- server request {request_id} ({count} line(s)) ---"


def _entry(entry: LogEntry) -> str:
    """One server line, indented under the banner it belongs to."""
    return (f"  {entry.ts.isoformat(timespec='seconds')} "
            f"{entry.level:<7} [{entry.stage}] {entry.message}")


class JobLog:
    """Collects lines for one job, echoing them as it goes."""

    def __init__(self, *, echo: bool = True) -> None:
        self.lines: List[str] = []
        self.echo = echo

    def step(self, message: str) -> None:
        """A client-side step, for the terminal and the file."""
        self._write(f"{datetime.now().isoformat(timespec='seconds')} {message}")
        if self.echo:
            print(message, flush=True)

    def server(self, request_id: str, entries: Sequence[LogEntry]) -> None:
        """Fold in what the server did during one request."""
        self._write(_header(request_id, len(entries)))
        for entry in entries:
            self._write(_entry(entry))

    def write(self, path: Path) -> Path:
        """Write the log next to the CV it belongs to."""
        path.write_text("\n".join([*self.lines, ""]), encoding="utf-8")
        return path

    def _write(self, line: str) -> None:
        self.lines.append(line)


def format_entries(request_id: str, entries: Sequence[LogEntry]) -> str:
    """The same rendering, for ``jobstitch logs <request_id>`` on stdout."""
    return "\n".join([_header(request_id, len(entries)), *map(_entry, entries)])


__all__ = ["JobLog", "format_entries"]
