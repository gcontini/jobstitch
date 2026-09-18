"""``jobstitch logs <request_id>`` — what the server did, on stdout.

Every envelope carries a ``request_id``; this is the other half of that.
It exists so a failure reported hours ago can still be explained without
re-running the job, for as long as the server keeps the request.
"""

from __future__ import annotations

from ..api import HttpApi, JobstitchError
from ..config import Config
from ..joblog import format_entries
from ..ui import fail


def run(config: Config, request_id: str) -> int:
    api = HttpApi(config.server_url, token=config.token, verbose=config.verbose)
    try:
        envelope = api.logs(request_id)
    except JobstitchError as exc:
        fail(f"{exc}")
    print(format_entries(request_id, envelope.data.entries), flush=True)
    return 0
