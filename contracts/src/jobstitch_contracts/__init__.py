"""The wire format shared by the jobstitch server and its clients.

Nothing here talks to a network, a filesystem or a model — it is the set of
shapes both sides agree on, so neither has to import the other:

- ``payloads`` — :class:`CVStatus`, polled while a CV job runs, and
  :class:`RenderedCV`, the finished document, LaTeX and PDF in one.
- ``jd`` — :class:`JDAnalysis` (``analysis.json``) and :class:`JDDetection`.
- ``envelope`` — :class:`Envelope`, the uniform response body, and
  :class:`RequestLog`, fetched separately by request id.
- ``guess`` — :func:`static_jd_guess`, the free pre-check both sides run.

The CV document itself has no schema here: it is whatever the model wrote
merged with whatever your ``candidate_data.json`` holds, and only your LaTeX
template has an opinion about it.
"""

from .envelope import Envelope, LogEntry, RequestLog
from .guess import MAX_JD_CHARS, MIN_JD_CHARS, static_jd_guess
from .jd import JDAnalysis, JDDetection
from .payloads import CoverLetter, CVStatus, RenderedCV, ServerStatus

__all__ = [
    "JDAnalysis",
    "JDDetection",
    "CVStatus",
    "RenderedCV",
    "CoverLetter",
    "ServerStatus",
    "Envelope",
    "LogEntry",
    "RequestLog",
    "static_jd_guess",
    "MIN_JD_CHARS",
    "MAX_JD_CHARS",
]
