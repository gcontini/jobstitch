"""Plain helpers for the server tests (fixtures live in conftest.py)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from jobstitch_server.model_selector import ModelSelector
from jobstitch_server.pipeline.cv_schema import TailoredCVData

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CANDIDATE = REPO_ROOT / "examples" / "candidate"
GOLDEN = Path(__file__).parent / "golden"


def sample_cv_data(**overrides) -> TailoredCVData:
    """Plain, valid CV content — no escaping edge cases."""
    data = dict(
        company_name="Globex",
        job_title="Staff Platform Engineer",
        summary="Engineer with 12 years of experience building distributed systems.",
        skills=["Kubernetes", "Terraform", "Go", "PostgreSQL", "AWS", "Observability"],
        experiences=[
            {
                "title": "Principal Platform Engineer",
                "company": "Northwind Logistics",
                "location": "Lisbon, Portugal",
                "dates": "March 2022 -- Present",
                "project_name": "Freight tracking",
                "bullet_points": ["Owned the **event ingestion** architecture."],
            },
            {
                "title": "Senior SRE",
                "company": "Meridian Health",
                "location": "Barcelona, Spain",
                "dates": "August 2018 -- February 2022",
                "project_name": None,
                "bullet_points": ["Designed multi-region failover."],
            },
            {
                "title": "Backend Engineer",
                "company": "Cobalt Analytics",
                "location": "Berlin, Germany",
                "dates": "May 2014 -- July 2018",
                "project_name": None,
                "bullet_points": ["Built a Go query engine."],
            },
        ],
    )
    data.update(overrides)
    return TailoredCVData(**data)


class FakeSelector(ModelSelector):
    """A real :class:`ModelSelector` with a fake HTTP client underneath.

    Only ``llm`` is replaced, so everything the server actually relies on —
    request assembly, ``response_format`` negotiation, usage recording — is
    the production code path. ``replies`` is consumed in order and the last
    one repeats, so a retry loop can be handed one failure then a success.
    """

    def __init__(
        self,
        *replies: str,
        profile: str = "fake",
        model: str = "fake-model",
        structured_output: str = "json_object",
    ):
        super().__init__(
            profile=profile, api_key="test-key", base_url="http://fake.invalid/v1",
            model=model, structured_output=structured_output,
        )
        self.replies = list(replies) or ["{}"]
        self.calls: list[dict] = []
        self.gate: threading.Event | None = None
        self.llm = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self._create))
        )

    def _create(self, **kwargs):
        if self.gate is not None:
            assert self.gate.wait(timeout=10), "the gate was never opened"
        # Snapshot the messages: the pipeline appends to the same list
        # across retries, so a reference would show only the final state.
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        content = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, reasoning_content=None),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22, total_tokens=33,
                                  completion_tokens_details=None),
        )

    @property
    def last_prompt(self) -> str:
        return "\n".join(m["content"] or "" for m in self.calls[-1]["messages"])


def wait_for_job(client, request_id: str, *, timeout: float = 10.0):
    """Poll a CV job until it settles. Returns the last response, 2xx or not.

    A job may finish before the first poll — the fakes answer instantly — and
    that is fine: the state is a file, and reading it consumes nothing.
    """
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/v1/cv/{request_id}/status")
        if response.status_code != 200:
            return response
        if response.json()["data"]["status"] == "END":
            return response
        assert time.monotonic() < deadline, f"job {request_id} never finished"
        time.sleep(0.02)


def start_cv(client, parts, **data):
    """POST /v1/cv and return the job id."""
    response = client.post("/v1/cv", data={"jd_text": "x" * 1200, **data}, files=parts)
    assert response.status_code == 202, response.text
    return response.json()["request_id"]
