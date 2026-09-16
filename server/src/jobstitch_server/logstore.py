"""Where a finished request's log lines wait to be fetched.

Responses carry no commentary, so the lines a request produced are kept here,
keyed by request id, for ``GET /logs/{request_id}``. A bounded, in-memory
ring: the newest ``capacity`` requests, then the oldest are dropped.

This is the one piece of state the server holds, and it is deliberately
disposable. Two consequences worth knowing before deploying more than one
instance:

- behind a load balancer, the fetch may land on an instance that never saw
  the request and will answer 404 — run one instance, or use sticky routing,
  or read the logs from your platform's log stream instead;
- a restart loses them. They are a convenience, not an audit trail.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from threading import Lock
from typing import List, Optional

from jobstitch_contracts import LogEntry

#: How many requests' logs to keep. Each is a few dozen short lines.
DEFAULT_CAPACITY = 200


class LogStore:
    """The last ``capacity`` requests' log lines, oldest evicted first."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self.capacity = max(1, capacity)
        self._entries: "OrderedDict[str, List[LogEntry]]" = OrderedDict()
        self._lock = Lock()

    @classmethod
    def from_env(cls) -> "LogStore":
        raw = os.getenv("JOBSTITCH_LOG_HISTORY")
        return cls(int(raw) if raw else DEFAULT_CAPACITY)

    def put(self, request_id: str, entries: List[LogEntry]) -> None:
        if not entries:
            return
        with self._lock:
            self._entries[request_id] = entries
            self._entries.move_to_end(request_id)
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def get(self, request_id: str) -> Optional[List[LogEntry]]:
        with self._lock:
            return self._entries.get(request_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["LogStore", "DEFAULT_CAPACITY"]
