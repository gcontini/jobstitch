"""The only module that knows the server exists.

Everything else in the client talks to :class:`JobstitchApi`, the protocol
below, which is why the modes can be tested without a server and why swapping
transport would touch one file.

Every call returns the server's :class:`Envelope` rather than just its
payload, because the ``request_id`` in it is what the caller needs next — to
poll a CV job, and to fetch what the server actually did from ``/logs``.
"""

from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

import httpx
from jobstitch_contracts import (
    CoverLetter,
    CVStatus,
    Envelope,
    JDAnalysis,
    JDDetection,
    RenderedCV,
    RequestLog,
    ServerStatus,
)

#: One multipart part: the field name, then the file name, bytes and type.
Part = Tuple[str, Tuple[str, Any, str]]

#: No call blocks for long any more — a CV job is polled, not waited on — but
#: a render still has to sit through pdflatex.
REQUEST_TIMEOUT = 180.0
CONNECT_TIMEOUT = 10.0

#: How many times to knock on /healthz before giving up on a cold server.
HEALTH_ATTEMPTS = 3
HEALTH_RETRY_DELAY = 2.0


class JobstitchError(RuntimeError):
    """A call the server refused, or could not be made at all."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        #: The handle for ``/logs/{id}`` — what the server did before failing.
        self.request_id = request_id


class JobstitchApi(Protocol):
    """What the client needs a jobstitch server to do."""

    def logs(self, request_id: str) -> Envelope[RequestLog]: ...

    def detect(self, text: str) -> Envelope[JDDetection]: ...

    def analyze(
        self, text: str, *, profile: bytes, preferences: str,
        temperature: Optional[float] = None,
    ) -> Envelope[JDAnalysis]: ...

    def create_cv(
        self, text: str, *, profile: bytes, candidate_data: bytes,
        prompts: Optional[Mapping[str, str]] = None, template: Optional[str] = None,
        images: Optional[Mapping[str, bytes]] = None, temperature: Optional[float] = None,
        pages: Optional[int] = None,
    ) -> Envelope[None]: ...

    def cv_status(self, request_id: str) -> Envelope[CVStatus]: ...

    def cv_result(self, request_id: str) -> Envelope[RenderedCV]: ...

    def render(
        self, *, document: Optional[Mapping[str, Any]] = None, tex: Optional[str] = None,
        template: Optional[str] = None, images: Optional[Mapping[str, bytes]] = None,
    ) -> Envelope[RenderedCV]: ...

    def letter(
        self, text: str, *, profile: bytes, analysis: Optional[str] = None,
        prompt: Optional[str] = None, temperature: Optional[float] = None,
    ) -> Envelope[CoverLetter]: ...


@dataclass
class HttpApi:
    """:class:`JobstitchApi` over HTTP."""

    base_url: str
    token: Optional[str] = None
    #: Injection point for tests: a client that reaches the app in-process,
    #: so both halves are exercised together with no socket.
    client: Optional[httpx.Client] = None
    #: --verbose: report each /healthz attempt while the server wakes up.
    verbose: bool = False
    _client: httpx.Client = field(init=False, repr=False)
    _health_checked: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self.client is not None:
            self.client.headers.update(headers)
            self._client = self.client
            return
        self._client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT),
        )

    # --- the calls ----------------------------------------------------------
    def logs(self, request_id: str) -> Envelope[RequestLog]:
        """What the server did during one request. 404s once it is too old."""
        return self._get(f"/logs/{request_id}", RequestLog, request_id=request_id)

    def detect(self, text: str) -> Envelope[JDDetection]:
        self._ensure_healthy()
        return self._post("/v1/jd/detect", JDDetection, data={"jd_text": text})

    def analyze(self, text, *, profile, preferences, temperature=None):
        self._ensure_healthy()
        return self._post(
            "/v1/jd/analysis", JDAnalysis,
            data=_clean({"jd_text": text, "pers_preferences_text": preferences,
                         "temperature": temperature}),
            files=[("candidate_profile", ("candidate_profile.json", profile,
                                          "application/json"))],
        )

    def create_cv(self, text, *, profile, candidate_data, prompts=None, template=None,
                  images=None, temperature=None, pages=None):
        """Start a CV job. The envelope's ``request_id`` is the job's handle.

        ``candidate_data`` goes with it because the server renders the CV to
        count its pages, and an empty contact block is not the page count of
        the CV you will send. No model is shown it.
        """
        files: List[Part] = [
            ("candidate_profile", ("candidate_profile.json", profile, "application/json")),
            ("candidate_data", ("candidate_data.json", candidate_data, "application/json")),
        ]
        for name, content in (prompts or {}).items():
            files.append((name, (f"{name}.txt", content, "text/plain")))
        if template is not None:
            files.append(("template", ("resume.tex.jinja", template, "text/plain")))
        files.extend(_image_parts(images))
        self._ensure_healthy()
        return self._post("/v1/cv", type(None),
                          data=_clean({"jd_text": text, "temperature": temperature,
                                       "pages": pages}),
                          files=files)

    def cv_status(self, request_id: str) -> Envelope[CVStatus]:
        return self._get(f"/v1/cv/{request_id}/status", CVStatus, request_id=request_id)

    def cv_result(self, request_id: str) -> Envelope[RenderedCV]:
        return self._get(f"/v1/cv/{request_id}", RenderedCV, request_id=request_id)

    def render(self, *, document=None, tex=None, template=None, images=None):
        files: List[Part] = []
        if document is not None:
            files.append(("document", ("cv.json", json.dumps(document), "application/json")))
        if tex is not None:
            files.append(("tex", ("cv.tex", tex, "text/plain")))
        if template is not None:
            files.append(("template", ("resume.tex.jinja", template, "text/plain")))
        files.extend(_image_parts(images))
        return self._post("/v1/cv/render", RenderedCV, files=files)

    def letter(self, text, *, profile, analysis=None, prompt=None, temperature=None):
        files: List[Part] = [
            ("candidate_profile", ("candidate_profile.json", profile, "application/json"))
        ]
        if analysis is not None:
            files.append(("analysis", ("analysis.json", analysis, "application/json")))
        if prompt is not None:
            files.append(("sys_prompt_letter", ("sys_prompt_letter.txt", prompt, "text/plain")))
        return self._post("/v1/letter", CoverLetter,
                          data=_clean({"jd_text": text, "temperature": temperature}),
                          files=files)

    # --- plumbing -----------------------------------------------------------
    def _ensure_healthy(self) -> None:
        """Before the first real call: make sure the server is up.

        A freshly started server can take a moment to become reachable, so
        this knocks on ``/healthz`` a few times rather than failing on the
        first refused connection. Checked once per client, not once per call.
        """
        if self._health_checked:
            return
        last_error: Optional[JobstitchError] = None
        for attempt in range(1, HEALTH_ATTEMPTS + 1):
            if self.verbose:
                print(f"spinning up the server attempt {attempt}/{HEALTH_ATTEMPTS}")
            try:
                self._get("/healthz", ServerStatus)
                self._health_checked = True
                return
            except JobstitchError as exc:
                last_error = exc
                if attempt < HEALTH_ATTEMPTS:
                    time.sleep(HEALTH_RETRY_DELAY)
        raise JobstitchError(
            f"the jobstitch server at {self.base_url} did not come up after "
            f"{HEALTH_ATTEMPTS} attempts: {last_error}",
            status=last_error.status if last_error else 0,
        )

    def _get(self, path: str, payload: type, *, request_id: Optional[str] = None) -> Envelope:
        return self._send("GET", path, payload, request_id=request_id)

    def _post(self, path: str, payload: type, **kwargs) -> Envelope:
        return self._send("POST", path, payload, **kwargs)

    def _send(
        self, method: str, path: str, payload: type,
        *, request_id: Optional[str] = None, **kwargs,
    ) -> Envelope:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # A network error (e.g. a timeout) means no response came back to
            # read a request_id from, so the id already known to the caller
            # (a job being polled) is the only one there is to report.
            raise JobstitchError(
                f"cannot reach the jobstitch server at {self.base_url}: {exc}",
                request_id=request_id,
            ) from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise JobstitchError(
                f"{path} returned {response.status_code} and not JSON "
                f"({response.text[:200]!r})",
                status=response.status_code,
                request_id=response.headers.get("x-request-id"),
            ) from exc

        envelope = Envelope[payload].model_validate(body)
        if response.is_success and envelope.ok:
            return envelope

        raise JobstitchError(
            envelope.error or f"{path} failed with {response.status_code}",
            status=response.status_code,
            request_id=envelope.request_id,
        )


def _image_parts(images: Optional[Mapping[str, bytes]]) -> List[Part]:
    """Every image as its own ``images`` part, named after the file it came from.

    A list of parts and not a mapping because they all share one field name:
    the server keys the assets off each part's file name, which is what the
    template includes them under.
    """
    return [
        ("images", (name, blob, mimetypes.guess_type(name)[0] or "application/octet-stream"))
        for name, blob in (images or {}).items()
    ]


def _clean(values: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unset form fields so the server sees its own defaults."""
    return {k: v for k, v in values.items() if v is not None}


def read_bytes(path: Optional[Path]) -> Optional[bytes]:
    return path.read_bytes() if path is not None else None


def read_text(path: Optional[Path]) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path is not None else None


__all__ = ["JobstitchApi", "HttpApi", "JobstitchError", "read_bytes", "read_text"]
