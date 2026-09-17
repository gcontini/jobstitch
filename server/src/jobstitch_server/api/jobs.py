"""Running a CV job after its request has already been answered.

``POST /v1/cv`` accepts and returns; the writing happens in a thread of its
own, which reports where it is by rewriting ``status.json``. That is the whole
mechanism: a poll reads a file, so nothing has to be held open for the minutes
a CV takes, and a client that stops polling costs nothing.

A plain thread, not the request's: the job has to outlive the response, and
the shared threadpool is where uploads are read, so a job parked there would
block them. The bound on live jobs stays the one semaphore — claimed on the
event loop before the thread starts, released by the thread when it ends.
"""

from __future__ import annotations

from pathlib import Path
from threading import Thread
from typing import Any, Callable, Dict, Tuple

from jobstitch_contracts import RenderedCV

from ..jobstore import RESULT, STATUS, JobDir
from ..observability import Run, use_run
from .deps import AppState, save_log
from .errors import describe_failure, log_failure


def start(
    state: AppState,
    job: JobDir,
    run: Run,
    build: Callable[[Path, Callable[[str, str], None]], Tuple[Dict[str, Any], str, bytes, str]],
) -> None:
    """Hand one accepted job to a thread. Returns as soon as it is running.

    ``build`` is what produces the CV: it is given the scratch directory and a
    progress sink, and returns the merged document, the LaTeX and the PDF it
    was rendered into, and one line saying what the run cost.
    """

    def progress(status: str, detail: str) -> None:
        job.write(STATUS, {"state": "running", "status": status, "detail": detail})
        save_log(job, run)

    def work() -> None:
        # A fresh thread starts with an empty context, so the run it logs into
        # has to be re-established here or every pipeline line is discarded.
        with use_run(run):
            try:
                document, tex, pdf, summary = build(job.work, progress)
                job.write(RESULT, RenderedCV.from_bytes(
                    tex=tex, pdf=pdf, document=document
                ).model_dump(mode="json"))
                ending = {"state": "done", "status": "END", "detail": summary}
            except Exception as exc:  # noqa: BLE001 — stored, not swallowed
                log_failure(exc, "POST /v1/cv")
                status, message = describe_failure(exc)
                ending = {"state": "failed", "status": "END", "detail": "",
                          "http_status": status, "error": message}
            state.job_slots.release()
            job.drop_work()
            save_log(job, run)
            # Last of all, so that END means the result is on disk, the scratch
            # is not, and a client can act on it without racing the worker.
            job.write(STATUS, ending)

    Thread(target=work, name=f"jobstitch-{job.request_id}", daemon=True).start()


__all__ = ["start"]
