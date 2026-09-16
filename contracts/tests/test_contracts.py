"""The wire format itself: what both sides have to agree on."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from jobstitch_contracts import (
    CV_DOCUMENT_VERSION,
    CVDocument,
    Envelope,
    JDAnalysis,
    LogEntry,
    RequestLog,
    TailoredCVData,
    static_jd_guess,
)

CANDIDATE = {
    "name": "Jordan Rivera", "email": "j@example.com", "phone": "+1 555 0142",
    "linkedin": "https://example.com/in/jordan", "languages": "English",
    "location": "Lisbon", "education": "MSc",
}


def cv(**overrides) -> TailoredCVData:
    data = dict(
        company_name="Acme", job_title="Head of IT", summary="A summary.",
        skills=[f"skill {i}" for i in range(6)],
        experiences=[
            {"title": "Engineer", "company": "Initech", "location": "Rome",
             "dates": "2020 -- 2024", "bullet_points": ["Did a thing."]}
        ] * 3,
    )
    data.update(overrides)
    return TailoredCVData(**data)


def document(**overrides) -> CVDocument:
    return CVDocument(cv=cv(), candidate=dict(CANDIDATE), **overrides)


# --- the CV document --------------------------------------------------------
def test_the_render_context_merges_both_halves(document=document()):
    context = document.render_context()
    assert context["name"] == "Jordan Rivera"      # never LLM-written
    assert context["job_title"] == "Head of IT"    # LLM-written
    assert "generation_date" in context


def test_the_generation_date_follows_generated_at():
    doc = document(generated_at=datetime(2026, 1, 15, 9, 30))
    assert doc.render_context()["generation_date"] == "2026/01/15"


def test_a_collision_between_the_halves_is_refused():
    """A model-written field must never be able to overwrite a real contact
    detail, so the ambiguity is raised rather than silently resolved."""
    doc = document()
    doc.candidate["job_title"] = "Injected"       # nothing stops the file saying this
    with pytest.raises(ValueError, match="both define"):
        doc.render_context()


def test_extra_candidate_fields_reach_the_template():
    """The candidate half is yours: whatever the template reads, put it in."""
    candidate = {**CANDIDATE, "certifications": ["CKA"]}
    assert CVDocument(cv=cv(), candidate=candidate).render_context()["certifications"] == ["CKA"]


def test_a_document_survives_a_round_trip():
    doc = document(job_description="a posting", generated_at=datetime(2026, 1, 15))
    assert CVDocument.model_validate_json(doc.model_dump_json()) == doc


def test_documents_declare_their_version():
    assert document().version == CV_DOCUMENT_VERSION


def test_the_cv_schema_enforces_the_template_s_limits():
    with pytest.raises(ValidationError):
        cv(skills=["only", "three", "here"])       # min 6
    with pytest.raises(ValidationError):
        cv(experiences=[])                         # min 3


def test_a_sparse_candidate_is_accepted_as_given():
    """No schema on this half — a template that wants fewer fields is fine."""
    assert CVDocument(cv=cv(), candidate={"name": "Jordan Rivera"}).candidate == {
        "name": "Jordan Rivera"
    }


# --- the envelope -----------------------------------------------------------
def test_an_envelope_round_trips_with_its_payload():
    envelope = Envelope[JDAnalysis](request_id="abc", ok=False)
    assert Envelope[JDAnalysis].model_validate_json(envelope.model_dump_json()).ok is False


def test_an_envelope_carries_only_the_payload():
    """Logs travel on /logs/{id}, so nothing large rides on every reply."""
    assert set(Envelope[JDAnalysis].model_fields) == {"request_id", "ok", "data", "error"}


def test_a_request_log_round_trips():
    log = RequestLog(request_id="abc", entries=[
        LogEntry(ts=datetime(2026, 1, 15, 9, 0), level="INFO", stage="cv.generate",
                 message="gpt-fake 1.2s | prompt=10, completion=20"),
    ])
    assert RequestLog.model_validate_json(log.model_dump_json()) == log


# --- the free JD check ------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected, why",
    [
        ("x" * 1500, True, "plausible"),
        ("x" * 999, False, "too short"),
        ("x" * 10001, False, "too long"),
        ("x" * 1500 + "\x00", False, "binary"),
    ],
)
def test_the_static_check_decides_what_it_can(text, expected, why):
    assert static_jd_guess(text) is expected, why


def test_the_bounds_are_adjustable():
    assert static_jd_guess("x" * 100, min_chars=50)


def test_a_control_character_dump_is_not_text():
    assert not static_jd_guess("\x01\x02\x03" * 500 + "x" * 500)
