"""The generate -> review -> render -> condense loop, without a model or LaTeX."""

from __future__ import annotations

import json
import shutil

import pytest

from jobstitch_contracts import CVDocument
from jobstitch_server.pipeline.cv_generator import CVGenerator
from jobstitch_server.pipeline.cv_renderer import RenderResult
from jobstitch_server.pipeline.errors import ModelOutputError

from server_helpers import FakeSelector, sample_cv_data

OK_REVIEW = '{"status": "OK", "violations": []}'


def cv_json(**overrides) -> str:
    return sample_cv_data(**overrides).model_dump_json()


def build(bundle, candidate, tmp_path, cv_model, highlight_model=None, **kw) -> CVGenerator:
    return CVGenerator(
        cv_model=cv_model,
        highlight_model=highlight_model,
        bundle=bundle,
        candidate=candidate,
        work_dir=tmp_path,
        **kw,
    )


@pytest.fixture
def no_latex(monkeypatch):
    """Skip the compile: the page check is exercised separately."""
    pages = {"n": 1}

    def fake_render(self, cv_data, attempt):
        return RenderResult(tex="", pdf=b"%PDF", pages=pages["n"],
                            advice="length OK" if pages["n"] <= 2 else "REMOVE 1 bullet point.")

    monkeypatch.setattr(CVGenerator, "_render", fake_render)
    return pages


def test_returns_a_document_carrying_the_candidate_data_verbatim(
    bundle, candidate, tmp_path, no_latex
):
    model = FakeSelector(cv_json(), OK_REVIEW)
    doc = build(bundle, candidate, tmp_path, model).generate("JD text")

    assert isinstance(doc, CVDocument)
    assert doc.candidate == dict(candidate.data)
    assert doc.job_description == "JD text"
    assert doc.cv.job_title == "Staff Platform Engineer"


def test_the_prompt_carries_the_system_prompt_schema_profile_and_jd(
    bundle, candidate, tmp_path, no_latex
):
    model = FakeSelector(cv_json(), OK_REVIEW)
    build(bundle, candidate, tmp_path, model).generate("SENTINEL JD")

    first = model.calls[0]["messages"]
    assert first[0]["content"] == bundle.sys_prompt_cv
    user = first[1]["content"]
    # The schema is restated in the prompt because a json_object-only endpoint
    # never sees it through response_format.
    assert "JSON_SCHEMA (TailoredCVData)" in user
    assert json.dumps(model.calls[0]["response_format"]) == '{"type": "json_object"}'
    assert candidate.profile["name"] in user
    assert "SENTINEL JD" in user


def test_an_overridden_prompt_is_what_the_model_sees(bundle, candidate, tmp_path, no_latex):
    custom = bundle.with_overrides(sys_prompt_cv="WRITE IT MY WAY")
    model = FakeSelector(cv_json(), OK_REVIEW)
    build(custom, candidate, tmp_path, model).generate("JD")
    assert model.calls[0]["messages"][0]["content"] == "WRITE IT MY WAY"


def test_invalid_json_is_fed_back_and_retried(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector("not json at all", cv_json(), OK_REVIEW)
    build(bundle, candidate, tmp_path, model).generate("JD")
    retry_prompt = model.calls[1]["messages"][-1]["content"]
    assert "NOT valid against the TailoredCVData schema" in retry_prompt


def test_giving_up_names_the_stage(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector("never valid")
    with pytest.raises(ModelOutputError) as excinfo:
        build(bundle, candidate, tmp_path, model, max_validation_attempts=2).generate("JD")
    assert excinfo.value.stage == "cv.generate"


def test_review_violations_are_fed_back_and_the_cv_regenerated(
    bundle, candidate, tmp_path, no_latex
):
    model = FakeSelector(
        cv_json(),
        '{"status": "REVIEW", "violations": ["invented a job at NASA"]}',
        cv_json(job_title="Rewritten"),
        OK_REVIEW,
    )
    doc = build(bundle, candidate, tmp_path, model).generate("JD")
    assert doc.cv.job_title == "Rewritten"
    assert any("invented a job at NASA" in (m["content"] or "")
               for call in model.calls for m in call["messages"])


def test_review_without_violations_is_treated_as_a_pass(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector(cv_json(), '{"status": "REVIEW", "violations": []}')
    doc = build(bundle, candidate, tmp_path, model).generate("JD")
    assert doc.cv.job_title == "Staff Platform Engineer"


def test_an_overlong_pdf_sends_the_advice_back(bundle, candidate, tmp_path, no_latex):
    no_latex["n"] = 3
    model = FakeSelector(cv_json(), OK_REVIEW)
    with pytest.raises(ModelOutputError):
        build(bundle, candidate, tmp_path, model, max_attempts=2).generate("JD")
    assert any("REMOVE 1 bullet point." in (m["content"] or "")
               for call in model.calls for m in call["messages"])


def test_highlighting_failure_keeps_the_unhighlighted_cv(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector(cv_json(), OK_REVIEW)
    highlighter = FakeSelector("}{ not json")
    doc = build(bundle, candidate, tmp_path, model, highlight_model=highlighter).generate("JD")
    assert doc.cv.job_title == "Staff Platform Engineer"


def test_highlighting_replaces_the_content_when_it_works(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector(cv_json(), OK_REVIEW)
    highlighter = FakeSelector(cv_json(summary="**Bold** summary."))
    doc = build(bundle, candidate, tmp_path, model, highlight_model=highlighter).generate("JD")
    assert doc.cv.summary == "**Bold** summary."


def test_the_time_budget_stops_the_loop(bundle, candidate, tmp_path, no_latex):
    from jobstitch_server.pipeline.errors import BudgetExceededError

    model = FakeSelector(cv_json(), OK_REVIEW)
    gen = build(bundle, candidate, tmp_path, model, deadline=0.0)
    with pytest.raises(BudgetExceededError):
        gen.generate("JD")


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
def test_end_to_end_with_a_real_compile(bundle, candidate, tmp_path):
    """No model, but a real render: the page check runs on a real PDF."""
    model = FakeSelector(cv_json(), OK_REVIEW)
    doc = build(bundle, candidate, tmp_path, model).generate("JD")
    assert doc.cv.job_title == "Staff Platform Engineer"
    assert (tmp_path / "attempt_1.pdf").is_file()
