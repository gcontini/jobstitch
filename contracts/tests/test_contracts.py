"""The wire format itself: what both sides have to agree on."""

from __future__ import annotations

import base64
from datetime import datetime

import pytest

from jobstitch_contracts import (
    CVStatus,
    Envelope,
    JDAnalysis,
    LogEntry,
    RenderedCV,
    RequestLog,
    static_jd_guess,
)

# --- what a CV job reports and returns --------------------------------------
def test_a_status_carries_the_current_step_and_nothing_else():
    """No history: a poll answers where the job is, not where it has been."""
    assert set(CVStatus.model_fields) == {"status", "detail"}


def test_a_status_starts_with_no_detail():
    """The first poll lands before any step has finished."""
    assert CVStatus(status="generate").detail == ""


def test_a_detail_may_span_lines():
    """The reviewer's complaints and the condense instruction are quoted
    verbatim, and both are lists or paragraphs."""
    status = CVStatus(status="re-generate", detail="review rejected the CV\n- Invented Kubernetes")
    assert status.detail.splitlines()[-1] == "- Invented Kubernetes"


def test_an_unknown_status_is_refused():
    with pytest.raises(ValueError):
        CVStatus(status="thinking-about-it")


def test_a_rendered_cv_carries_the_pdf_back_as_bytes():
    rendered = RenderedCV.from_bytes(tex="\\documentclass{article}", pdf=b"%PDF-1.7")
    assert rendered.pdf_bytes() == b"%PDF-1.7"
    assert base64.b64decode(rendered.pdf_base64) == b"%PDF-1.7"


def test_a_re_render_echoes_no_document():
    """Whoever asked for a render already has the data it rendered."""
    assert RenderedCV.from_bytes(tex="x", pdf=b"y").document is None


def test_the_document_has_no_schema():
    """It is whatever the model wrote with your own data over the top, and only
    your template has an opinion about it."""
    rendered = RenderedCV.from_bytes(tex="x", pdf=b"y", document={"hobbies": ["climbing"]})
    assert rendered.document == {"hobbies": ["climbing"]}


# --- the envelope -----------------------------------------------------------
def test_an_envelope_round_trips_with_its_payload():
    envelope = Envelope[JDAnalysis](request_id="abc", ok=False)
    assert Envelope[JDAnalysis].model_validate_json(envelope.model_dump_json()).ok is False


def test_an_envelope_carries_only_the_payload():
    """Logs travel on /logs/{id}, so nothing large rides on every reply."""
    assert set(Envelope[JDAnalysis].model_fields) == {"request_id", "ok", "data", "error"}


def test_a_failure_says_why_in_one_line():
    """One string, not a structure: the detail that would not fit is in the
    log, which is where a reader who wants it is going anyway."""
    envelope = Envelope[JDAnalysis](
        request_id="abc", ok=False, error="model_output [cv.generate]: no valid CV"
    )
    assert "cv.generate" in envelope.error


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
