"""The only module that knows the server exists.

Everything else in the client talks to :class:`JobstitchApi`, the protocol
below, which is why the modes can be tested without a server and why swapping
transport would touch one file.

Every call returns the server's :class:`Envelope` rather than just its
payload, because the ``request_id`` in it is what :meth:`HttpApi.logs` needs
to fetch what the server actually did — separately, and only when someone
wants it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol

import httpx
from jobstitch_contracts import (
    CoverLetter,
    CVDocument,
    Envelope,
    JDAnalysis,
    JDDetection,
    RenderedCV,
    RequestLog,
    ServerStatus,
)

#: A CV run is minutes of model calls; the default has to allow for that.
DEFAULT_TIMEOUT = 1800.0
CONNECT_TIMEOUT = 10.0

#: Statuses where trying the same request later is reasonable.
RETRYABLE = frozenset({429, 502, 503, 504})


class JobstitchError(RuntimeError):
    """A call the server refused, or could not be made at all."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        kind: str = "transport",
        stage: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.stage = stage
        #: The handle for ``/logs/{id}`` — what the server did before failing.
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        """True when the job should go back to the queue rather than to error/."""
        return self.status in RETRYABLE or self.status == 0


class JobstitchApi(Protocol):
    """What the client needs a jobstitch server to do."""

    def health(self) -> Envelope[ServerStatus]: ...

    def logs(self, request_id: str) -> Envelope[RequestLog]: ...

    def detect(self, text: str) -> Envelope[JDDetection]: ...

    def analyze(
        self, text: str, *, profile: bytes, preferences: str,
        temperature: Optional[float] = None,
    ) -> Envelope[JDAnalysis]: ...

    def create_cv(
        self, text: str, *, profile: bytes, candidate_data: bytes,
        prompts: Optional[Mapping[str, str]] = None, template: Optional[str] = None,
        signature: Optional[bytes] = None, temperature: Optional[float] = None,
    ) -> Envelope[CVDocument]: ...

    def render(
        self, *, document: Optional[CVDocument] = None, tex: Optional[str] = None,
        template: Optional[str] = None, signature: Optional[bytes] = None,
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
    timeout: float = DEFAULT_TIMEOUT
    #: Print each call and its request id — the client's ``--verbose``.
    verbose: bool = False
    #: Injection point for tests: a client that reaches the app in-process,
    #: so both halves are exercised together with no socket.
    client: Optional[httpx.Client] = None
    _client: httpx.Client = field(init=False, repr=False)

    def __post_init__(self) -> None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self.client is not None:
            self.client.headers.update(headers)
            self._client = self.client
            return
        self._client = httpx.Client(
            base_url=self.base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(self.timeout, connect=CONNECT_TIMEOUT),
        )

    @classmethod
    def from_env(cls, url: Optional[str] = None, token: Optional[str] = None) -> "HttpApi":
        return cls(
            base_url=url or os.getenv("JOBSTITCH_API_URL", "http://localhost:8080"),
            token=token or os.getenv("JOBSTITCH_API_TOKEN") or None,
        )

    def close(self) -> None:
        self._client.close()

    # --- the calls ----------------------------------------------------------
    def health(self) -> Envelope[ServerStatus]:
        return self._get("/healthz", ServerStatus)

    def logs(self, request_id: str) -> Envelope[RequestLog]:
        """What the server did during one request. 404s once it is too old."""
        return self._get(f"/logs/{request_id}", RequestLog)

    def detect(self, text: str) -> Envelope[JDDetection]:
        return self._post("/v1/jd/detect", JDDetection, data={"jd_text": text})

    def analyze(self, text, *, profile, preferences, temperature=None):
        return self._post(
            "/v1/jd/analysis", JDAnalysis,
            data=_clean({"jd_text": text, "pers_preferences_text": preferences,
                         "temperature": temperature}),
            files={"candidate_profile": ("candidate_profile.json", profile,
                                         "application/json")},
        )

    def create_cv(self, text, *, profile, candidate_data, prompts=None, template=None,
                  signature=None, temperature=None):
        files: Dict[str, Any] = {
            "candidate_profile": ("candidate_profile.json", profile, "application/json"),
            "candidate_data": ("candidate_data.json", candidate_data, "application/json"),
        }
        for name, content in (prompts or {}).items():
            files[name] = (f"{name}.txt", content, "text/plain")
        if template is not None:
            files["template"] = ("resume3.tex.jinja", template, "text/plain")
        if signature is not None:
            files["signature"] = ("candidate_signature.png", signature, "image/png")
        return self._post("/v1/cv", CVDocument,
                          data=_clean({"jd_text": text, "temperature": temperature}),
                          files=files)

    def render(self, *, document=None, tex=None, template=None, signature=None):
        files: Dict[str, Any] = {}
        if document is not None:
            files["document"] = ("cv.json", document.model_dump_json(), "application/json")
        if tex is not None:
            files["tex"] = ("cv.tex", tex, "text/plain")
        if template is not None:
            files["template"] = ("resume3.tex.jinja", template, "text/plain")
        if signature is not None:
            files["signature"] = ("candidate_signature.png", signature, "image/png")
        return self._post("/v1/cv/render", RenderedCV, files=files)

    def letter(self, text, *, profile, analysis=None, prompt=None, temperature=None):
        files: Dict[str, Any] = {
            "candidate_profile": ("candidate_profile.json", profile, "application/json")
        }
        if analysis is not None:
            files["analysis"] = ("analysis.json", analysis, "application/json")
        if prompt is not None:
            files["sys_prompt_letter"] = ("sys_prompt_letter.txt", prompt, "text/plain")
        return self._post("/v1/letter", CoverLetter,
                          data=_clean({"jd_text": text, "temperature": temperature}),
                          files=files)

    # --- plumbing -----------------------------------------------------------
    def _get(self, path: str, payload: type) -> Envelope:
        return self._send("GET", path, payload)

    def _post(self, path: str, payload: type, **kwargs) -> Envelope:
        return self._send("POST", path, payload, **kwargs)

    def _send(self, method: str, path: str, payload: type, **kwargs) -> Envelope:
        if self.verbose:
            print(f"→ {method} {self.base_url.rstrip('/')}{path}", flush=True)
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JobstitchError(
                f"cannot reach the jobstitch server at {self.base_url}: {exc}"
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
        if self.verbose:
            print(f"← {response.status_code} request {envelope.request_id}", flush=True)
        if response.is_success and envelope.ok:
            return envelope

        error = envelope.error
        raise JobstitchError(
            error.message if error else f"{path} failed with {response.status_code}",
            status=response.status_code,
            kind=error.type if error else "http_error",
            stage=error.stage if error else None,
            request_id=envelope.request_id,
        )


def _clean(values: Dict[str, Any]) -> Dict[str, Any]:
    """Drop unset form fields so the server sees its own defaults."""
    return {k: v for k, v in values.items() if v is not None}


def read_bytes(path: Optional[Path]) -> Optional[bytes]:
    return path.read_bytes() if path is not None else None


def read_text(path: Optional[Path]) -> Optional[str]:
    return path.read_text(encoding="utf-8") if path is not None else None


__all__ = ["JobstitchApi", "HttpApi", "JobstitchError", "read_bytes", "read_text"]
