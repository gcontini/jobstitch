"""``jobstitch-api`` — run the server with uvicorn.

Reads only what a platform gives it: ``$PORT``, ``$HOST``,
``$WEB_CONCURRENCY``. Everything else is :class:`Settings`.
"""

from __future__ import annotations

import os

from .observability import configure_logging


def main() -> None:
    import uvicorn

    configure_logging()
    uvicorn.run(
        # An import string, not the app object: uvicorn needs it to spawn
        # workers.
        "jobstitch_server.api.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        workers=int(os.getenv("WEB_CONCURRENCY", "1")),
        timeout_keep_alive=int(os.getenv("JOBSTITCH_KEEPALIVE", "75")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        # jobstitch installs its own handlers; uvicorn's would duplicate every
        # line and drop the request id.
        log_config=None,
    )


if __name__ == "__main__":
    main()
