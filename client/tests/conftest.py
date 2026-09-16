"""Fixtures for the client tests.

The client is tested against :class:`FakeApi` — an in-memory
:class:`~jobstitch_client.api.JobstitchApi` — so every test runs with no
server, no network and no model. One test file goes the other way and drives
the real server in-process; see ``test_client_server.py``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from jobstitch_contracts import (
    CoverLetter,
    CVDocument,
    Envelope,
    JDAnalysis,
    JDDetection,
    LogEntry,
    RenderedCV,
    RequestLog,
    TailoredCVData,
)

from jobstitch_client.api import JobstitchError
from jobstitch_client.config import Config
from jobstitch_client.ui import Decision
from jobstitch_client.workspace import Workspace

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CANDIDATE = REPO_ROOT / "examples" / "candidate"

#: Long enough to pass the free structural check.
JD_TEXT = "We are hiring a Head of IT. Responsibilities and requirements. " * 30


def analysis(**overrides) -> JDAnalysis:
    data = dict(
        match_percentage=82, match_rationale="Strong overlap.", job_title="Head of IT",
        work_location="Milan, Italy", work_mode="hybrid", expected_salary=None,
        max_salary=-1, experience_level="manager", hard_skills=["AWS"],
        soft_skills=["Communication"], company_name="Acme Corp", posting_type="direct",
        posting_url=None, gaps="No SAP.", pers_preferences="Hybrid is fine.",
        pers_preference_score=1.5,
    )
    data.update(overrides)
    return JDAnalysis(**data)


def document() -> CVDocument:
    return CVDocument(
        cv=TailoredCVData(
            company_name="Acme Corp", job_title="Head of IT", summary="A summary.",
            skills=[f"skill {i}" for i in range(6)],
            experiences=[
                {"title": "Engineer", "company": "Initech", "location": "Rome, Italy",
                 "dates": "2020 -- 2024", "project_name": None,
                 "bullet_points": ["Did a thing."]}
                for _ in range(3)
            ],
        ),
        candidate=json.loads((EXAMPLE_CANDIDATE / "candidate_data.json").read_text()),
        generated_at=datetime(2026, 1, 15),
    )


def envelope(data, *, request_id: str = "test-request") -> Envelope:
    return Envelope(request_id=request_id, ok=True, data=data)


class FakeApi:
    """Records what it was asked and replies with whatever it was given."""

    def __init__(self, **replies):
        self.is_jd = replies.get("is_jd", True)
        self.analysis = replies.get("analysis", analysis())
        self.document = replies.get("document", document())
        self.rendered = replies.get(
            "rendered", RenderedCV.from_bytes(tex=r"\documentclass{article}", pdf=b"%PDF-fake",
                                              pages=2, advice="length OK")
        )
        self.cover_letter = replies.get(
            "cover_letter", CoverLetter(text="Dear hiring manager.", words=3)
        )
        self.fail_on = replies.get("fail_on")
        self.log_entries = replies.get("log_entries", [
            LogEntry(ts=datetime(2026, 1, 15, 9, 0), level="INFO", stage="cv.generate",
                     message="[cv] gpt-fake 1.2s | prompt=10, completion=20"),
        ])
        self.calls: list[str] = []

    def _record(self, name):
        self.calls.append(name)
        if self.fail_on == name:
            raise JobstitchError(f"{name} failed", status=502, kind="model_output",
                                 stage=name, request_id="failed-request")

    def health(self):
        self._record("health")
        return envelope(None)

    def logs(self, request_id):
        self._record("logs")
        return envelope(RequestLog(request_id=request_id, entries=self.log_entries))

    def detect(self, text):
        self._record("detect")
        return envelope(JDDetection(is_job_description=self.is_jd))

    def analyze(self, text, *, profile, preferences, temperature=None):
        self._record("analyze")
        self.seen_profile = profile
        self.seen_preferences = preferences
        return envelope(self.analysis)

    def create_cv(self, text, *, profile, candidate_data, prompts=None, template=None,
                  signature=None, temperature=None):
        self._record("create_cv")
        self.seen_prompts = dict(prompts or {})
        self.seen_template = template
        self.seen_signature = signature
        self.seen_temperature = temperature
        return envelope(self.document)

    def render(self, *, document=None, tex=None, template=None, signature=None):
        self._record("render")
        return envelope(self.rendered)

    def letter(self, text, *, profile, analysis=None, prompt=None, temperature=None):
        self._record("letter")
        return envelope(self.cover_letter)


class ScriptedConfirmer:
    """Answers the confirm prompt from a list, without a terminal."""

    def __init__(self, *decisions: Decision):
        self.decisions = list(decisions) or [Decision(submit=True)]
        self.seen: list = []

    def confirm(self, analysis):
        self.seen.append(analysis)
        return self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]


@pytest.fixture
def config() -> Config:
    """Points at the shipped fictional candidate; no server needed."""
    return Config(
        server_url="http://test.invalid",
        files={name: EXAMPLE_CANDIDATE / name for name in (
            "candidate_profile.json", "candidate_data.json",
            "pers_preferences.md", "candidate_signature.png")},
    )


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    return Workspace(tmp_path / "out").ensure()


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def runner(api, workspace, config):
    from jobstitch_client.runner import JobRunner
    from jobstitch_client.tracking import build_tracker

    return JobRunner(
        api=api, workspace=workspace, config=config,
        confirmer=ScriptedConfirmer(Decision(submit=True)),
        tracker=build_tracker(workspace.root, enabled=True),
    )
