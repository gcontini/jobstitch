"""Where a request's scratch space, its log and its CV job state live.

One directory per request under ``JOBSTITCH_WORK_DIR``::

    <root>/<request_id>/
        status.json   a CV job's state; only CV jobs have one
        result.json   the finished RenderedCV; only once the job is done
        log.json      the lines the request produced
        work/         the scratch directory, removed when the work ends

Writes are atomic — a temporary file in the same directory, then
``os.replace`` — because a poll may read a file while the worker is rewriting
it, and half a status is worse than an old one. That is also why there are no
locks: one worker writes a given directory, everyone else only reads.

The directory *is* the state, so a restart no longer loses what a request did
and a job survives the request that started it. It is still disposable: the
newest :data:`KEEP_JOBS` are kept and the rest are dropped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, List, Optional

#: A request id becomes a path component, so it may only look like one.
SAFE_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

#: How many requests' directories to keep. Each is a few dozen KB.
KEEP_JOBS = 200

STATUS = "status.json"
RESULT = "result.json"
LOG = "log.json"


class JobDir:
    """One request's directory. Reads return ``None`` when the file is not there."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.request_id = path.name

    @property
    def work(self) -> Path:
        """The scratch directory the pipeline is handed."""
        path = self.path / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def drop_work(self) -> None:
        shutil.rmtree(self.path / "work", ignore_errors=True)

    # --- the three files ----------------------------------------------------
    def read(self, name: str) -> Optional[Any]:
        try:
            return json.loads((self.path / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write(self, name: str, value: Any) -> None:
        """Atomically, and never at the cost of the request: a full disk must
        not turn a finished CV into a 500."""
        tmp = self.path / f".{name}.tmp"
        try:
            tmp.write_text(json.dumps(value), encoding="utf-8")
            os.replace(tmp, self.path / name)
        except OSError:
            pass


class JobStore:
    """The directories under one root."""

    def __init__(self, root: Path, keep: int = KEEP_JOBS) -> None:
        self.root = Path(root)
        self.keep = max(1, keep)

    def create(self, request_id: str) -> JobDir:
        """A fresh directory. Raises ``FileExistsError`` if the id is in use."""
        path = self.root / request_id
        path.mkdir(parents=True)
        self.prune()
        return JobDir(path)

    def open(self, request_id: str) -> Optional[JobDir]:
        """An existing directory, or ``None`` — unknown, pruned, or not an id."""
        if not SAFE_ID.match(request_id):
            return None
        path = self.root / request_id
        return JobDir(path) if path.is_dir() else None

    def all(self) -> List[JobDir]:
        """Every directory that is one of ours, newest first.

        The root may be the system temp directory, so a directory only counts
        when it is named like a request id *and* holds one of our files — and
        one we cannot even look inside (a shared ``/tmp`` holds other
        processes' directories) is provably not one of ours.
        """
        found = []
        for path in self.root.glob("*"):
            if not SAFE_ID.match(path.name):
                continue
            try:
                if not path.is_dir():
                    continue
                if (path / STATUS).is_file() or (path / LOG).is_file():
                    found.append(path)
            except OSError:
                continue
        found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [JobDir(p) for p in found]

    def prune(self) -> None:
        """Drop the oldest, never a job still running."""
        for job in self.all()[self.keep:]:
            if (job.read(STATUS) or {}).get("state") == "running":
                continue
            shutil.rmtree(job.path, ignore_errors=True)


__all__ = ["JobStore", "JobDir", "SAFE_ID", "KEEP_JOBS", "STATUS", "RESULT", "LOG"]
