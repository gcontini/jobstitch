"""Starting a CV job and following it to the end.

The server accepts the job and answers immediately; writing the CV takes
minutes, so this asks where it has got to every few seconds and reports each
new status. Both the full pipeline and ``submit-raw`` need exactly this, and
neither should have its own copy.
"""

from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from jobstitch_contracts import RenderedCV

from .api import JobstitchApi, JobstitchError, read_text
from .config import Config

#: How often to ask. The job takes minutes; asking faster only adds noise.
POLL_SECONDS = 4.0


def write_cv(
    api: JobstitchApi, config: Config, jd_text: str, *,
    say: Callable[[str], None], resume: Optional[str] = None,
) -> Tuple[str, RenderedCV]:
    """Write one CV, reporting progress through ``say``.

    Returns the job's request id and what it produced — the merged document,
    its LaTeX and its PDF. A job that fails raises :class:`JobstitchError`
    from the poll, carrying the same cause and status the work would have.

    ``resume`` skips submitting a new job and picks an existing one back up
    from ``/v1/cv/{id}/status`` instead — for a job that was already started
    but whose answer never came back (a network timeout, a killed client).
    """
    if resume:
        request_id = resume
        say(f"   resuming request {request_id}")
    else:
        request_id = api.create_cv(
            jd_text,
            profile=config.require("candidate_profile.json").read_bytes(),
            candidate_data=config.require("candidate_data.json").read_bytes(),
            prompts=config.prompt_overrides(),
            template=read_text(config.path("resume.tex.jinja")),
            images=config.image_parts(),
            temperature=config.temperature,
            pages=config.pages,
        ).request_id
        if config.verbose:
            say(f"   request id: {request_id} (jobstitch logs {request_id})")

    deadline = time.monotonic() + config.timeout
    seen_status, seen_detail = "", ""
    while True:
        status = api.cv_status(request_id).data
        if config.verbose and status.detail and status.detail != seen_detail:
            seen_detail = status.detail
            say(status.detail)
        if status.status != seen_status:
            seen_status = status.status
            say(f"   {status.status}")
        if status.status == "END":
            return request_id, api.cv_result(request_id).data
        if time.monotonic() > deadline:
            raise JobstitchError(
                f"the CV job gave no answer within {config.timeout:.0f}s "
                f"(last status: {status.status})",
                request_id=request_id,
            )
        time.sleep(POLL_SECONDS)


__all__ = ["write_cv", "POLL_SECONDS"]
