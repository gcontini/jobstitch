"""Client and server, together, in one process.

The client's HTTP calls go through an ASGI transport straight into the real
FastAPI app — real routing, real multipart parsing, real envelopes — with only
the model endpoint faked. It is the one test that would catch the two halves
disagreeing about the wire format.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server" / "tests"))

from jobstitch_client.api import HttpApi, JobstitchError          # noqa: E402
from jobstitch_client.config import Config                        # noqa: E402
from jobstitch_client.runner import JobRunner                     # noqa: E402
from jobstitch_client.sources import JDCandidate                  # noqa: E402
from jobstitch_client.tracking import build_tracker               # noqa: E402
from jobstitch_client.ui import Decision                          # noqa: E402
from jobstitch_client.workspace import Workspace                  # noqa: E402
from jobstitch_server.api.app import create_app                   # noqa: E402
from jobstitch_server.api.deps import AppState                    # noqa: E402
from jobstitch_server.api.settings import Settings                # noqa: E402
from jobstitch_server.bundle import default_bundle                # noqa: E402

from server_helpers import FakeSelector, sample_cv_data           # noqa: E402

needs_latex = pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
EXAMPLE = REPO_ROOT / "examples" / "candidate"
JD_TEXT = "We are hiring a Head of IT in Milan. Responsibilities and requirements. " * 30

ANALYSIS = {
    "match_percentage": 77, "match_rationale": "Good overlap.", "job_title": "Head of IT",
    "work_location": "Milan, Italy", "work_mode": "hybrid", "expected_salary": None,
    "max_salary": -1, "experience_level": "manager", "hard_skills": ["AWS"],
    "soft_skills": ["Communication"], "company_name": "Acme Corp", "posting_type": "direct",
    "posting_url": None, "gaps": "No SAP.", "pers_preferences": "Hybrid is fine.",
    "pers_preference_score": 1.5,
}


class Scripted:
    """One decision, no terminal."""

    def __init__(self, decision=Decision(submit=True)):
        self.decision = decision

    def confirm(self, analysis):
        return self.decision


@pytest.fixture
def models():
    return {
        # YES to the detection call, then the analysis; the CV model follows.
        "summary": FakeSelector("YES", json.dumps(ANALYSIS),
                                profile="summary", model="fake-summary"),
        "cv": FakeSelector(sample_cv_data().model_dump_json(),
                           '{"status": "OK", "violations": []}',
                           profile="cv", model="fake-cv"),
        "highlight": FakeSelector(sample_cv_data(summary="**Bold** summary.").model_dump_json(),
                                  profile="highlight", model="fake-highlight"),
    }


@pytest.fixture
def api(models, tmp_path):
    from threading import BoundedSemaphore

    settings = Settings(work_root=tmp_path / "work", request_budget_seconds=120.0)
    settings.work_root.mkdir(parents=True, exist_ok=True)
    state = AppState(
        settings=settings, models=models, bundle=default_bundle(),
        job_slots=BoundedSemaphore(settings.max_concurrent_jobs),
        pdflatex=shutil.which("pdflatex") is not None,
    )
    from fastapi.testclient import TestClient

    app = create_app(state=state)
    # TestClient runs the real app in-process and is an httpx.Client, so the
    # client's own request code is what makes the calls.
    with TestClient(app) as http:
        yield HttpApi("http://server.invalid", client=http)


@pytest.fixture
def make_runner(api, tmp_path):
    """A runner against the in-process server; ``debug`` picks the log policy."""
    def build(*, debug: bool = False) -> JobRunner:
        workspace = Workspace(tmp_path / "out").ensure()
        config = Config(
            server_url="http://server.invalid",
            debug=debug,
            files={name: EXAMPLE / name for name in (
                "candidate_profile.json", "candidate_data.json", "candidate_preferences.md",
                "candidate_signature.png")},
        )
        return JobRunner(api=api, workspace=workspace, config=config,
                         confirmer=Scripted(), tracker=build_tracker(workspace.root, True))
    return build


@pytest.fixture
def runner(make_runner):
    return make_runner()


@pytest.fixture(autouse=True)
def no_polling_delay(monkeypatch):
    """The fakes answer instantly; waiting 4s between polls proves nothing."""
    monkeypatch.setattr("jobstitch_client.cvjob.POLL_SECONDS", 0)


@needs_latex
def test_a_posting_becomes_a_real_pdf(runner, models):
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    assert outcome.status == "delivered", (outcome.path / "log.log").read_text()
    folder = outcome.path
    assert folder.name == "Acme_Corp_Head_of_IT"
    pdf = folder / "cv_Jordan_Rivera.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    assert r"\documentclass" in (folder / "cv_Jordan_Rivera.tex").read_text()

    log = (folder / "log.log").read_text()
    assert "writing the CV" in log
    # The statuses the job went through, reported as they happened.
    assert "generate" in log and "END" in log
    assert "server request" not in log, "a quiet run does not pull the server's log"


@needs_latex
def test_debug_pulls_every_request_s_log_off_the_server(make_runner):
    """The other half of the envelope: /logs/{id} over the same HTTP client."""
    outcome = make_runner(debug=True).handle(JDCandidate(text=JD_TEXT))

    log = (outcome.path / "log.log").read_text()
    assert "jd.analysis" in log and "cv.generate" in log
    assert "prompt=" in log, "each model call logs what it spent"


@needs_latex
def test_the_rendered_pdf_can_be_re_rendered_from_its_document(runner, api, tmp_path):
    runner.handle(JDCandidate(text=JD_TEXT))
    document = next(tmp_path.rglob("cv_Jordan_Rivera.json"))

    stored = json.loads(document.read_text())
    assert stored["name"] == "Jordan Rivera", "your own data, merged in server-side"
    assert stored["job_title"] == "Staff Platform Engineer", "and the model's"

    envelope = api.render(document=stored)
    assert envelope.data.pdf_bytes().startswith(b"%PDF")


def test_a_server_error_reaches_the_client_as_a_typed_failure(runner, api, models, tmp_path):
    models["summary"].replies = ["YES", "not an analysis"]
    dropped = tmp_path / "posting.txt"
    dropped.write_text(JD_TEXT)
    claimed = runner.workspace.take_in(dropped)

    outcome = runner.handle(JDCandidate(text=JD_TEXT, origin=claimed, label="posting.txt"))

    assert outcome.status == "failed"
    assert outcome.path.parent == runner.workspace.error, "never left in working/"
    log = Path(str(outcome.path) + ".log").read_text()
    assert "jd.analysis" in log and "model_output" in log


def test_the_client_reports_an_unreachable_server_clearly():
    api = HttpApi("http://127.0.0.1:1")  # nothing listens here
    with pytest.raises(JobstitchError, match="cannot reach the jobstitch server"):
        api.detect("x" * 1500)
