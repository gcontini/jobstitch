"""Fixtures shared by the server tests.

Nothing here needs an API key, a network or a real model: the pipeline talks
to a :class:`FakeSelector` that replays canned replies and records what it was
asked, which is how the prompt-assembly assertions stay honest.
"""

from __future__ import annotations

import json
import shutil

import pytest

from jobstitch_contracts import CVDocument, TailoredCVData
from jobstitch_server.bundle import CandidateInputs, ResourceBundle, default_bundle

from server_helpers import FakeSelector, sample_cv_data

from server_helpers import EXAMPLE_CANDIDATE, GOLDEN


@pytest.fixture(scope="session")
def bundle() -> ResourceBundle:
    """The server's own shipped defaults."""
    return default_bundle()


@pytest.fixture(scope="session")
def candidate() -> CandidateInputs:
    """The fictional Jordan Rivera set, as a client would post it."""
    return CandidateInputs(
        profile=json.loads((EXAMPLE_CANDIDATE / "candidate_profile.json").read_text()),
        data=json.loads((EXAMPLE_CANDIDATE / "candidate_data.json").read_text()),
        preferences=(EXAMPLE_CANDIDATE / "pers_preferences.md").read_text(),
    )


@pytest.fixture(scope="session")
def fixture_document() -> CVDocument:
    """A document whose strings exercise every escaping rule."""
    raw = json.loads((GOLDEN / "cv_fixture.json").read_text())
    from datetime import datetime

    return CVDocument(
        cv=TailoredCVData.model_validate(raw["cv"]),
        candidate=raw["candidate"],
        generated_at=datetime(2026, 1, 15),
    )


@pytest.fixture
def sample_document(candidate) -> CVDocument:
    return CVDocument(cv=sample_cv_data(), candidate=dict(candidate.data))


@pytest.fixture
def fake_models() -> dict:
    """The three roles, all fake. Replace ``replies`` per test."""
    return {role: FakeSelector(profile=role, model=f"fake-{role}")
            for role in ("summary", "cv", "highlight")}


@pytest.fixture
def app_state(fake_models, tmp_path, bundle):
    from threading import BoundedSemaphore

    from jobstitch_server.api.deps import AppState
    from jobstitch_server.api.settings import Settings

    settings = Settings(work_root=tmp_path, request_budget_seconds=60.0)
    return AppState(
        settings=settings,
        models=fake_models,
        bundle=bundle,
        job_slots=BoundedSemaphore(settings.max_concurrent_jobs),
        pdflatex=shutil.which("pdflatex") is not None,
    )


@pytest.fixture
def client(app_state):
    """A TestClient over the real app, wired to fake models."""
    from fastapi.testclient import TestClient

    from jobstitch_server.api.app import create_app

    with TestClient(create_app(state=app_state)) as test_client:
        yield test_client


@pytest.fixture
def parts(candidate) -> dict:
    """The multipart parts a client always sends, as httpx files."""
    return {
        "candidate_profile": ("candidate_profile.json", json.dumps(dict(candidate.profile)),
                              "application/json"),
        "candidate_data": ("candidate_data.json", json.dumps(dict(candidate.data)),
                           "application/json"),
    }
