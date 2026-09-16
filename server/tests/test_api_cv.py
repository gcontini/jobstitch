"""POST /v1/cv and /v1/cv/render, end to end over HTTP with fake models."""

from __future__ import annotations

import json
import shutil

import pytest

from jobstitch_contracts import CVDocument, RenderedCV

from server_helpers import FakeSelector, sample_cv_data

needs_latex = pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
OK_REVIEW = '{"status": "OK", "violations": []}'


def load_replies(models, *replies):
    models["cv"].replies = list(replies)


@needs_latex
def test_create_cv_returns_a_document_with_the_candidate_data(client, fake_models, parts):
    load_replies(fake_models, sample_cv_data().model_dump_json(), OK_REVIEW)
    fake_models["highlight"].replies = [sample_cv_data(summary="**Bold**.").model_dump_json()]

    response = client.post("/v1/cv", data={"jd_text": "a job description"}, files=parts)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    doc = CVDocument.model_validate(body["data"])
    assert doc.candidate["name"] == "Jordan Rivera"       # copied through, not generated
    assert doc.cv.summary == "**Bold**."                  # highlighted
    assert doc.job_description == "a job description"


@needs_latex
def test_the_response_carries_the_result_and_nothing_else(client, fake_models, parts):
    """Logs are fetched by request id, not carried by every response."""
    load_replies(fake_models, sample_cv_data().model_dump_json(), OK_REVIEW)

    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts)

    assert set(response.json()) == {"request_id", "ok", "data"}
    assert response.headers["x-request-id"] == response.json()["request_id"]


@needs_latex
def test_what_the_run_did_is_waiting_under_its_request_id(client, fake_models, parts):
    load_replies(fake_models, sample_cv_data().model_dump_json(), OK_REVIEW)
    request_id = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts).json()["request_id"]

    entries = client.get(f"/logs/{request_id}").json()["data"]["entries"]

    stages = {entry["stage"] for entry in entries}
    assert {"cv.generate", "cv.review", "render"} <= stages
    assert any("Review OK" in entry["message"] for entry in entries)
    # Every model call reports its cost as a log line; there is no second ledger.
    costs = [e["message"] for e in entries if "prompt=" in e["message"]]
    assert costs and all("completion=" in line for line in costs)


def test_an_unknown_request_id_is_a_404(client):
    response = client.get("/logs/never-happened")
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "unknown_request"


@needs_latex
def test_an_uploaded_prompt_is_the_one_used(client, fake_models, parts):
    load_replies(fake_models, sample_cv_data().model_dump_json(), OK_REVIEW)
    files = {**parts, "sys_prompt_cv": ("sys_prompt_cv.txt", "MY OWN PROMPT", "text/plain")}

    client.post("/v1/cv", data={"jd_text": "jd"}, files=files)

    assert fake_models["cv"].calls[0]["messages"][0]["content"] == "MY OWN PROMPT"


@needs_latex
def test_without_an_override_the_shipped_prompt_is_used(client, fake_models, parts, bundle):
    load_replies(fake_models, sample_cv_data().model_dump_json(), OK_REVIEW)
    client.post("/v1/cv", data={"jd_text": "jd"}, files=parts)
    assert fake_models["cv"].calls[0]["messages"][0]["content"] == bundle.sys_prompt_cv


def test_a_missing_jd_is_a_400_naming_the_part(client, parts):
    body = client.post("/v1/cv", files=parts).json()
    assert body["ok"] is False
    assert body["error"]["type"] == "missing_part"
    assert "jd" in body["error"]["message"]


def test_a_missing_profile_is_a_400(client):
    response = client.post("/v1/cv", data={"jd_text": "jd"})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "missing_part"


def test_an_unparsable_profile_is_a_400(client, parts):
    files = {**parts, "candidate_profile": ("p.json", "{not json", "application/json")}
    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=files)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "bad_part"


@needs_latex
def test_candidate_data_is_passed_through_whatever_shape_it_is(client, fake_models, parts):
    """It is your data going to your template: jobstitch does not police it."""
    load_replies(fake_models, sample_cv_data().model_dump_json(), OK_REVIEW)
    mine = {"name": "X", "email": "x@example.com", "phone": "1", "linkedin": "l",
            "languages": "en", "location": "Lisbon", "education": "MSc",
            "certifications": ["CKA"]}
    files = {**parts, "candidate_data": ("d.json", json.dumps(mine), "application/json")}

    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=files)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["candidate"] == mine


def test_an_oversized_part_is_a_413(client, app_state, parts):
    files = {**parts, "sys_prompt_cv": ("p.txt", "x" * (app_state.settings.max_part_bytes + 1))}
    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=files)
    assert response.status_code == 413


def test_a_failure_names_its_cause_and_keeps_the_evidence(client, fake_models, parts):
    load_replies(fake_models, "never valid json")

    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts)
    body = response.json()

    assert response.status_code == 502
    assert body["error"]["type"] == "model_output"
    assert body["error"]["stage"] == "cv.generate"

    # The attempts that were paid for are still readable afterwards.
    entries = client.get(f"/logs/{body['request_id']}").json()["data"]["entries"]
    assert sum("Validation failed" in e["message"] for e in entries) >= 1


def test_a_full_server_says_429_rather_than_queueing(client, app_state, parts):
    taken = [app_state.job_slots.acquire(blocking=False)
             for _ in range(app_state.settings.max_concurrent_jobs)]
    try:
        response = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts)
    finally:
        for _ in [t for t in taken if t]:
            app_state.job_slots.release()
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


# --- rendering --------------------------------------------------------------
@needs_latex
def test_render_returns_the_tex_and_the_pdf(client, sample_document):
    files = {"document": ("cv.json", sample_document.model_dump_json(), "application/json")}
    body = client.post("/v1/cv/render", files=files).json()

    rendered = RenderedCV.model_validate(body["data"])
    assert rendered.pages == 1 or rendered.pages == 2
    assert rendered.pdf_bytes().startswith(b"%PDF")
    assert r"\documentclass" in rendered.tex
    assert rendered.advice == "length OK"


@needs_latex
def test_render_compiles_a_hand_edited_tex(client, sample_document):
    tex = client.post(
        "/v1/cv/render",
        files={"document": ("cv.json", sample_document.model_dump_json(), "application/json")},
    ).json()["data"]["tex"].replace("Staff Platform Engineer", "Edited By Hand")

    body = client.post("/v1/cv/render", files={"tex": ("cv.tex", tex, "text/plain")}).json()
    assert body["ok"] is True
    assert "Edited By Hand" in body["data"]["tex"]


def test_render_needs_something_to_render(client):
    response = client.post("/v1/cv/render")
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "missing_part"


def test_render_refuses_both_inputs_at_once(client, sample_document):
    files = {
        "document": ("cv.json", sample_document.model_dump_json(), "application/json"),
        "tex": ("cv.tex", "x", "text/plain"),
    }
    assert client.post("/v1/cv/render", files=files).status_code == 400


def test_render_rejects_an_unknown_document_version(client, sample_document):
    data = json.loads(sample_document.model_dump_json())
    data["version"] = 99
    response = client.post(
        "/v1/cv/render", files={"document": ("cv.json", json.dumps(data), "application/json")}
    )
    assert response.status_code == 409
    assert response.json()["error"]["type"] == "unsupported_version"


@needs_latex
def test_a_broken_template_is_422_with_a_scrubbed_log(client, sample_document, tmp_path):
    files = {
        "document": ("cv.json", sample_document.model_dump_json(), "application/json"),
        "template": ("t.tex.jinja", r"\documentclass{article}\begin{document}\nope\end{document}"),
    }
    response = client.post("/v1/cv/render", files=files)
    body = response.json()
    assert response.status_code == 422
    assert body["error"]["stage"] == "compile"
    assert str(tmp_path) not in json.dumps(body)

    # The TeX log itself is behind /logs, so a failed render costs the caller
    # nothing until they ask for it.
    entries = client.get(f"/logs/{body['request_id']}").json()["data"]["entries"]
    assert any("Undefined control sequence" in e["message"] for e in entries)
    assert str(tmp_path) not in json.dumps(entries)
