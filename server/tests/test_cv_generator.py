"""The generate -> review -> render -> condense loop, without a model or LaTeX."""

from __future__ import annotations

import json
import shutil

import pytest

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

    def fake_render(self, cv_data, stem):
        return RenderResult(tex="", pdf=b"%PDF", pages=pages["n"],
                            advice="length OK" if pages["n"] <= 2 else "REMOVE 1 bullet point.")

    monkeypatch.setattr(CVGenerator, "_render", fake_render)
    return pages


def test_returns_the_document_the_latex_and_the_pdf(bundle, candidate, tmp_path, no_latex):
    """One call, everything a client needs to file: no second round trip to
    turn the content into a PDF."""
    model = FakeSelector(cv_json(), OK_REVIEW)
    document, tex, pdf, summary = build(bundle, candidate, tmp_path, model).generate("JD text")

    assert document["job_title"] == "Staff Platform Engineer"
    assert pdf == b"%PDF"
    assert "tokens used=" in summary


def test_your_own_data_wins_over_the_model_s(bundle, candidate, tmp_path, no_latex):
    """The two halves are one namespace now, so a field the model invents must
    not be able to replace a real contact detail."""
    model = FakeSelector(cv_json(), OK_REVIEW)
    document, *_ = build(bundle, candidate, tmp_path, model).generate("JD")
    assert document["name"] == candidate.data["name"]


def test_progress_names_each_step_and_what_the_one_before_cost(
    bundle, candidate, tmp_path, no_latex
):
    steps = []
    model = FakeSelector(cv_json(), OK_REVIEW)
    build(bundle, candidate, tmp_path, model,
          progress=lambda status, detail: steps.append((status, detail))).generate("JD")

    assert [status for status, _ in steps] == [
        "generate", "review", "page_check", "highlight"
    ]
    assert steps[0][1] == ""                                  # nothing has finished yet
    assert "generation finished, tokens used=" in steps[1][1]
    assert "review passed" in steps[2][1]
    assert "page check passed: 1 page(s)" in steps[3][1]


def test_a_rejected_review_reports_re_generate_with_the_violations(
    bundle, candidate, tmp_path, no_latex
):
    """Verbatim: the client is shown the words the model is about to be given."""
    steps = []
    model = FakeSelector(
        cv_json(),
        '{"status": "REVIEW", "violations": ["invented a job at NASA"]}',
        cv_json(), OK_REVIEW,
    )
    build(bundle, candidate, tmp_path, model,
          progress=lambda status, detail: steps.append((status, detail))).generate("JD")

    status, detail = next(s for s in steps if s[0] == "re-generate")
    assert "review rejected the CV" in detail
    assert detail.endswith("- invented a job at NASA")


def test_an_overlong_pdf_reports_the_condense_instruction(
    bundle, candidate, tmp_path, no_latex
):
    steps = []
    no_latex["n"] = 3
    model = FakeSelector(cv_json(), OK_REVIEW)
    with pytest.raises(ModelOutputError):
        build(bundle, candidate, tmp_path, model, max_attempts=2,
              progress=lambda status, detail: steps.append((status, detail))).generate("JD")

    _, detail = next(s for s in steps if s[0] == "re-generate")
    assert detail == "page check failed: 3 page(s)\nREMOVE 1 bullet point."


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


def test_an_endpoint_that_takes_the_schema_is_not_sent_it_twice(
    bundle, candidate, tmp_path, no_latex
):
    model = FakeSelector(cv_json(), OK_REVIEW, structured_output="json_schema_strict")
    build(bundle, candidate, tmp_path, model).generate("JD")

    first = model.calls[0]
    assert first["response_format"]["json_schema"]["strict"] is True
    assert "JSON_SCHEMA (TailoredCVData)" not in first["messages"][1]["content"]


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
    document, *_ = build(bundle, candidate, tmp_path, model).generate("JD")
    assert document["job_title"] == "Rewritten"
    assert any("invented a job at NASA" in (m["content"] or "")
               for call in model.calls for m in call["messages"])


def test_review_without_violations_is_treated_as_a_pass(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector(cv_json(), '{"status": "REVIEW", "violations": []}')
    document, *_ = build(bundle, candidate, tmp_path, model).generate("JD")
    assert document["job_title"] == "Staff Platform Engineer"


def test_highlighting_failure_keeps_the_unhighlighted_cv(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector(cv_json(), OK_REVIEW)
    highlighter = FakeSelector("}{ not json")
    document, *_ = build(bundle, candidate, tmp_path, model,
                         highlight_model=highlighter).generate("JD")
    assert document["job_title"] == "Staff Platform Engineer"


def test_highlighting_replaces_the_content_when_it_works(bundle, candidate, tmp_path, no_latex):
    model = FakeSelector(cv_json(), OK_REVIEW)
    highlighter = FakeSelector(cv_json(summary="**Bold** summary."))
    document, *_ = build(bundle, candidate, tmp_path, model,
                         highlight_model=highlighter).generate("JD")
    assert document["summary"] == "**Bold** summary."


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
    document, tex, pdf, _ = build(bundle, candidate, tmp_path, model).generate("JD")
    assert document["job_title"] == "Staff Platform Engineer"
    assert (tmp_path / "attempt_1.pdf").is_file()
    # The one that is kept is rendered after highlighting, so it is its own pass.
    assert (tmp_path / "cv.pdf").is_file()
    assert pdf.startswith(b"%PDF") and tex.startswith("\\documentclass")
