"""The output folder and every move inside it.

One directory holds the whole workflow::

    <out>/
        working/      in flight, and nothing else
        error/        rejected or failed, timestamp-prefixed
        discarded/    you said no, by day
        cv/           delivered, by day
        applications.xlsx

A job is assembled under ``working/`` and moved into place in one step when it
is finished, so a folder under ``cv/`` is never half-written. This module owns
every path and every move; nothing else in the client builds a path by hand.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

#: Prefix for a file in flight or in error — sorts chronologically.
TS_FORMAT = "%y-%m-%d-%H-%M-%S"
#: Daily folders under cv/ and discarded/.
DAY_FORMAT = "%y-%m-%d"

_TS_PREFIX = re.compile(r"^\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}_")

JD_FILENAME = "jd.txt"
ANALYSIS_FILENAME = "analysis.json"
LOG_FILENAME = "log.log"
LETTER_FILENAME = "cover_letter.txt"


def timestamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime(TS_FORMAT)


def day(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime(DAY_FORMAT)


def sanitize_name(text: str, max_len: int = 40) -> str:
    """Turn a company or job title into a filesystem-safe folder fragment."""
    clean = re.sub(r"\W+", "_", (text or "").strip()).strip("_")
    return clean[:max_len].strip("_") or "unknown"


def strip_timestamp(name: str) -> str:
    """``26-01-15-09-30-00_JD.txt`` -> ``JD.txt``."""
    return _TS_PREFIX.sub("", name)


@dataclass(frozen=True)
class Artifacts:
    """What a finished job folder is called, all derived from your name."""

    candidate: str

    @property
    def stem(self) -> str:
        return f"cv_{sanitize_name(self.candidate)}"

    @property
    def pdf(self) -> str:
        return f"{self.stem}.pdf"

    @property
    def tex(self) -> str:
        return f"{self.stem}.tex"

    @property
    def document(self) -> str:
        return f"{self.stem}.json"


class Workspace:
    """Owns the output tree and every move within it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.working = self.root / "working"
        self.error = self.root / "error"
        self.discarded = self.root / "discarded"
        self.cv = self.root / "cv"

    def ensure(self) -> "Workspace":
        for directory in (self.working, self.error, self.discarded, self.cv):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    # --- intake -------------------------------------------------------------
    def take_in(self, path: Path, *, move: bool = True) -> Path:
        """Claim an incoming file: put it in ``working/`` under a timestamp.

        Immediate and before anything else happens, so a watched folder never
        shows the same file twice and a crash leaves the file somewhere
        recoverable.

        ``move=False`` copies instead, for an input the caller named by path
        and still owns: ``jobstitch submit posting.txt`` must not make
        ``posting.txt`` disappear.
        """
        target = self._free(self.working / f"{timestamp()}_{path.name}")
        if move:
            shutil.move(str(path), str(target))
        else:
            shutil.copy2(str(path), str(target))
        return target

    def open_job(self, company: str, title: str) -> Path:
        """The working folder for an analyzed job: ``working/<Company>_<Title>``."""
        job = self.working / f"{sanitize_name(company)}_{sanitize_name(title)}"
        if job.exists():
            shutil.rmtree(job, ignore_errors=True)
        job.mkdir(parents=True)
        return job

    # --- outcomes -----------------------------------------------------------
    def deliver(self, job: Path) -> Path:
        """Finished: ``cv/<day>/<Company>_<Title>``."""
        return self._move_into(job, self.cv / day())

    def discard(self, job: Path) -> Path:
        """You said no: ``discarded/<day>/<Company>_<Title>``."""
        return self._move_into(job, self.discarded / day())

    def to_error(self, entry: Path) -> Path:
        """Failed or not a job description: ``error/<timestamp>_<name>``.

        A file that already carries a timestamp keeps it — the time it arrived
        is more useful than the time it failed.
        """
        name = entry.name if _TS_PREFIX.match(entry.name) else f"{timestamp()}_{entry.name}"
        self.error.mkdir(parents=True, exist_ok=True)
        target = self._free(self.error / name)
        shutil.move(str(entry), str(target))
        return target

    # --- recovery -----------------------------------------------------------
    def pending(self) -> Tuple[List[Path], List[Path]]:
        """What is left in ``working/``: loose files and job folders.

        A loose file was taken in but never analyzed; a folder was analyzed
        but never finished. They resume at different points, so they are
        reported apart.
        """
        if not self.working.is_dir():
            return [], []
        entries = sorted(self.working.iterdir())
        return ([e for e in entries if e.is_file()], [e for e in entries if e.is_dir()])

    def clean(self) -> int:
        """Empty ``working/``. Returns how many entries were removed."""
        files, folders = self.pending()
        for entry in files + folders:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        return len(files) + len(folders)

    # --- helpers ------------------------------------------------------------
    def _move_into(self, entry: Path, parent: Path) -> Path:
        parent.mkdir(parents=True, exist_ok=True)
        target = self._free(parent / entry.name)
        shutil.move(str(entry), str(target))
        return target

    @staticmethod
    def _free(target: Path) -> Path:
        """A path that does not exist yet: never overwrite a previous run."""
        if not target.exists():
            return target
        stem, suffix = target.stem, target.suffix
        for n in range(2, 1000):
            candidate = target.with_name(f"{stem}_{n}{suffix}")
            if not candidate.exists():
                return candidate
        raise FileExistsError(f"cannot find a free name near {target}")


__all__ = [
    "Workspace", "Artifacts", "sanitize_name", "strip_timestamp", "timestamp", "day",
    "JD_FILENAME", "ANALYSIS_FILENAME", "LOG_FILENAME", "LETTER_FILENAME",
]
