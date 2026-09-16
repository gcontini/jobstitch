"""The wire format shared by the jobstitch server and its clients.

Nothing here talks to a network, a filesystem or a model — it is the set of
shapes both sides agree on, so neither has to import the other:

- ``cv`` — :class:`TailoredCVData` (what the LLM writes) and
  :class:`CVDocument` (that plus the candidate data, copied through).
- ``jd`` — :class:`JDAnalysis` (``analysis.json``) and :class:`JDDetection`.
- ``envelope`` — :class:`Envelope`, the uniform response body, and
  :class:`RequestLog`, fetched separately by request id.
- ``guess`` — :func:`static_jd_guess`, the free pre-check both sides run.
"""

from .cv import CV_DOCUMENT_VERSION, CVDocument, TailoredCVData, WorkExperienceItem
from .envelope import Envelope, ErrorInfo, LogEntry, RequestLog
from .guess import MAX_JD_CHARS, MIN_JD_CHARS, static_jd_guess
from .jd import JDAnalysis, JDDetection
from .payloads import CoverLetter, RenderedCV, ServerStatus

__all__ = [
    "CV_DOCUMENT_VERSION",
    "CVDocument",
    "TailoredCVData",
    "WorkExperienceItem",
    "JDAnalysis",
    "JDDetection",
    "RenderedCV",
    "CoverLetter",
    "ServerStatus",
    "Envelope",
    "ErrorInfo",
    "LogEntry",
    "RequestLog",
    "static_jd_guess",
    "MIN_JD_CHARS",
    "MAX_JD_CHARS",
]
