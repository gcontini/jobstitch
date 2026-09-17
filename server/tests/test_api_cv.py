"""The CV endpoints end to end over HTTP, with fake models.

``POST /v1/cv`` answers before the work is done, so almost nothing here
asserts on its response: it starts a job and then waits for the job to settle.
The fakes reply instantly, so a job may well be finished before the first
poll — which is fine, because the state is a file and reading it consumes
nothing. Where a test needs the job to be provably *still running*, it closes
the model's gate first.
"""

from __future__ import annotations

import json
import shutil
import threading

import pytest

from jobstitch_contracts import RenderedCV

from server_helpers import sample_cv_data, start_cv, wait_for_job

needs_latex = pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
OK_REVIEW = '{"status": "OK", "violations": []}'


def load_replies(models, *replies):
    models["cv"].replies = list(replies)


def a_good_run(fake_models, **overrides):
    load_replies(fake_models, sample_cv_data(**overrides).model_dump_json(), OK_REVIEW)
    fake_models["highlight"].replies = [sample_cv_data(**overrides).model_dump_json()]


# --- accepting ---------------------------------------------------------------
def test_the_job_is_accepted_not_waited_for(client, fake_models, parts):
    """202 and an id: the only thing the caller gets up front is the handle."""
    a_good_run(fake_models)
    fake_models["cv"].gate = threading.Event()
    try:
        response = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts)
        assert response.status_code == 202
        assert set(response.json()) == {"request_id", "ok"}
        assert response.headers["x-request-id"] == response.json()["request_id"]
    finally:
        fake_models["cv"].gate.set()


@needs_latex
def test_a_job_in_flight_says_where_it_is_and_will_not_hand_over_a_cv(
    client, fake_models, parts
):
    a_good_run(fake_models)
    fake_models["cv"].gate = threading.Event()
    try:
        request_id = start_cv(client, parts)
        status = client.get(f"/v1/cv/{request_id}/status")
        assert status.status_code == 200
        assert status.json()["data"] == {"status": "generate", "detail": ""}

        early = client.get(f"/v1/cv/{request_id}")
        assert early.status_code == 409
        assert "still running" in early.json()["error"]
    finally:
        fake_models["cv"].gate.set()
    assert wait_for_job(client, request_id).json()["data"]["status"] == "END"


@needs_latex
def test_polling_leaves_nothing_behind(client, fake_models, parts, app_state):
    """A poll is not a request that did anything, so it must not cost a
    directory — a minutes-long job attracts a few hundred of them."""
    a_good_run(fake_models)
    fake_models["cv"].gate = threading.Event()
    try:
        request_id = start_cv(client, parts)
        for _ in range(20):
            client.get(f"/v1/cv/{request_id}/status")
    finally:
        fake_models["cv"].gate.set()
    wait_for_job(client, request_id)

    assert [p.name for p in app_state.settings.work_root.iterdir()] == [request_id]


# --- what comes back ---------------------------------------------------------
@needs_latex
def test_the_finished_job_hands_over_the_document_the_tex_and_the_pdf(
    client, fake_models, parts
):
    """One call, everything the client files — no second round trip to turn
    the content into a PDF."""
    a_good_run(fake_models)
    fake_models["highlight"].replies = [
        sample_cv_data(summary="**Bold**.").model_dump_json()
    ]
    request_id = start_cv(client, parts)
    wait_for_job(client, request_id)

    body = client.get(f"/v1/cv/{request_id}").json()
    assert body["request_id"] == request_id
    rendered = RenderedCV.model_validate(body["data"])
    assert rendered.document["summary"] == "**Bold**."          # highlighted
    assert rendered.document["job_title"] == "Staff Platform Engineer"
    assert rendered.pdf_bytes().startswith(b"%PDF")
    assert r"\documentclass" in rendered.tex


@needs_latex
def test_your_own_data_comes_back_in_the_document(client, fake_models, parts, candidate_data):
    """The server was sent it so it could count the pages; it is part of the
    CV, so it is part of what comes back."""
    a_good_run(fake_models)
    request_id = start_cv(client, parts)
    wait_for_job(client, request_id)

    document = client.get(f"/v1/cv/{request_id}").json()["data"]["document"]
    assert document["name"] == candidate_data["name"]
    assert document["email"] == candidate_data["email"]


@needs_latex
def test_a_field_the_schema_never_declared_is_handed_back_too(client, fake_models, parts):
    """The reply is validated, not filtered: your prompt and your template can
    agree on something this server has never heard of."""
    written = {**sample_cv_data().model_dump(), "availability": "Immediate"}
    load_replies(fake_models, json.dumps(written), OK_REVIEW)
    fake_models["highlight"].replies = [json.dumps(written)]
    request_id = start_cv(client, parts)
    wait_for_job(client, request_id)

    document = client.get(f"/v1/cv/{request_id}").json()["data"]["document"]
    assert document["availability"] == "Immediate"


@needs_latex
def test_the_status_reports_each_step_and_what_it_cost(client, fake_models, parts):
    a_good_run(fake_models)
    request_id = start_cv(client, parts)
    final = wait_for_job(client, request_id).json()["data"]

    assert final["status"] == "END"
    assert "tokens used=" in final["detail"]
    assert "page(s)" in final["detail"]


@needs_latex
def test_the_scratch_directory_goes_and_the_record_stays(client, fake_models, parts, app_state):
    a_good_run(fake_models)
    request_id = start_cv(client, parts)
    wait_for_job(client, request_id)

    job = app_state.settings.work_root / request_id
    assert not (job / "work").exists()
    assert {p.name for p in job.iterdir()} == {"status.json", "result.json", "log.json"}


@needs_latex
def test_the_slot_comes_back_when_the_job_ends(client, fake_models, parts, app_state):
    """Released by the worker, not by the request — so a leak only shows here."""
    a_good_run(fake_models)
    wait_for_job(client, start_cv(client, parts))

    taken = [app_state.job_slots.acquire(blocking=False)
             for _ in range(app_state.settings.max_concurrent_jobs)]
    for _ in [t for t in taken if t]:
        app_state.job_slots.release()
    assert all(taken)


# --- the log -----------------------------------------------------------------
@needs_latex
def test_what_the_run_did_is_waiting_under_its_request_id(client, fake_models, parts):
    a_good_run(fake_models)
    request_id = start_cv(client, parts)
    wait_for_job(client, request_id)

    entries = client.get(f"/logs/{request_id}").json()["data"]["entries"]

    stages = {entry["stage"] for entry in entries}
    assert {"cv.generate", "cv.review", "render"} <= stages
    assert any("Review OK" in entry["message"] for entry in entries)
    # Every model call reports its cost as a log line; there is no second ledger.
    costs = [e["message"] for e in entries if "prompt=" in e["message"]]
    assert costs and all("completion=" in line for line in costs)


def test_an_unknown_request_id_is_a_404(client):
    for path in ("/logs/never-happened", "/v1/cv/never-happened/status",
                 "/v1/cv/never-happened"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "unknown_request" in response.json()["error"]


# --- what is sent ------------------------------------------------------------
@needs_latex
def test_an_uploaded_prompt_is_the_one_used(client, fake_models, parts):
    a_good_run(fake_models)
    files = {**parts, "sys_prompt_cv": ("sys_prompt_cv.txt", "MY OWN PROMPT", "text/plain")}
    wait_for_job(client, start_cv(client, files))

    assert fake_models["cv"].calls[0]["messages"][0]["content"] == "MY OWN PROMPT"


@needs_latex
def test_without_an_override_the_shipped_prompt_is_used(client, fake_models, parts, bundle):
    a_good_run(fake_models)
    wait_for_job(client, start_cv(client, parts))
    assert fake_models["cv"].calls[0]["messages"][0]["content"] == bundle.sys_prompt_cv


@needs_latex
def test_no_model_is_ever_shown_your_candidate_data(client, fake_models, parts):
    """It is sent so the page count is real, and it goes to the template only."""
    a_good_run(fake_models)
    files = {**parts, "candidate_data": (
        "candidate_data.json",
        json.dumps({"name": "Jordan Rivera", "email": "SENTINEL@nowhere.invalid"}),
        "application/json",
    )}
    request_id = start_cv(client, files)
    wait_for_job(client, request_id)

    for model in fake_models.values():
        sent = json.dumps([call["messages"] for call in model.calls])
        assert "SENTINEL" not in sent
    # It did reach the template, though — that is what it is for.
    document = client.get(f"/v1/cv/{request_id}").json()["data"]["document"]
    assert document["email"] == "SENTINEL@nowhere.invalid"


def test_a_missing_jd_is_a_400_naming_the_part(client, parts):
    body = client.post("/v1/cv", files=parts).json()
    assert body["ok"] is False
    assert "missing_part" in body["error"]
    assert "jd" in body["error"]


@pytest.mark.parametrize("dropped", ["candidate_profile", "candidate_data"])
def test_a_missing_personal_part_is_a_400(client, parts, dropped):
    files = {k: v for k, v in parts.items() if k != dropped}
    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=files)
    assert response.status_code == 400
    assert dropped in response.json()["error"]


@pytest.mark.parametrize("broken", ["candidate_profile", "candidate_data"])
def test_an_unparsable_personal_part_is_a_400(client, parts, broken):
    files = {**parts, broken: ("p.json", "{not json", "application/json")}
    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=files)
    assert response.status_code == 400
    assert "bad_part" in response.json()["error"]


def test_an_oversized_part_is_a_413(client, app_state, parts):
    files = {**parts, "sys_prompt_cv": ("p.txt", "x" * (app_state.settings.max_part_bytes + 1))}
    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=files)
    assert response.status_code == 413


def test_a_request_id_cannot_name_a_directory_of_its_own_choosing(
    client, fake_models, parts, app_state
):
    """The id is a path component now, so anything that is not one is replaced."""
    response = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts,
                           headers={"X-Request-Id": "../../evil"})
    assert response.json()["request_id"] != "../../evil"
    assert not (app_state.settings.work_root.parent / "evil").exists()


def test_the_same_request_id_twice_is_refused(client, fake_models, parts):
    a_good_run(fake_models)
    fake_models["cv"].gate = threading.Event()
    headers = {"X-Request-Id": "reused-id"}
    try:
        assert client.post("/v1/cv", data={"jd_text": "jd"}, files=parts,
                           headers=headers).status_code == 202
        again = client.post("/v1/cv", data={"jd_text": "jd"}, files=parts, headers=headers)
        assert again.status_code == 400
        assert "already in use" in again.json()["error"]
    finally:
        fake_models["cv"].gate.set()


# --- failing -----------------------------------------------------------------
def test_a_failed_job_answers_the_poll_with_its_cause(client, fake_models, parts):
    """Exactly what the blocking call used to return, a poll later."""
    load_replies(fake_models, "never valid json")
    request_id = start_cv(client, parts)

    response = wait_for_job(client, request_id)
    body = response.json()

    assert response.status_code == 502
    assert body["ok"] is False
    assert body["request_id"] == request_id          # the job's id, not the poll's
    assert "model_output" in body["error"]
    assert "cv.generate" in body["error"]

    # Asking for the CV afterwards says the same thing rather than 404ing.
    assert client.get(f"/v1/cv/{request_id}").status_code == 502

    # The attempts that were paid for are still readable.
    entries = client.get(f"/logs/{request_id}").json()["data"]["entries"]
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


# --- rendering ---------------------------------------------------------------
@needs_latex
def test_render_returns_the_tex_and_the_pdf(client, sample_document):
    files = {"document": ("cv.json", json.dumps(sample_document), "application/json")}
    body = client.post("/v1/cv/render", files=files).json()

    rendered = RenderedCV.model_validate(body["data"])
    assert rendered.pdf_bytes().startswith(b"%PDF")
    assert r"\documentclass" in rendered.tex
    # Nothing echoed back: whoever asked for this render already has the data.
    assert rendered.document is None


@needs_latex
def test_render_compiles_a_hand_edited_tex(client, sample_document):
    tex = client.post(
        "/v1/cv/render",
        files={"document": ("cv.json", json.dumps(sample_document), "application/json")},
    ).json()["data"]["tex"].replace("Staff Platform Engineer", "Edited By Hand")

    body = client.post("/v1/cv/render", files={"tex": ("cv.tex", tex, "text/plain")}).json()
    assert body["ok"] is True
    assert "Edited By Hand" in body["data"]["tex"]


def test_render_needs_something_to_render(client):
    response = client.post("/v1/cv/render")
    assert response.status_code == 400
    assert "missing_part" in response.json()["error"]


def test_render_refuses_both_inputs_at_once(client, sample_document):
    files = {
        "document": ("cv.json", json.dumps(sample_document), "application/json"),
        "tex": ("cv.tex", "x", "text/plain"),
    }
    assert client.post("/v1/cv/render", files=files).status_code == 400


@needs_latex
def test_a_broken_template_is_422_with_a_scrubbed_log(client, sample_document, tmp_path):
    files = {
        "document": ("cv.json", json.dumps(sample_document), "application/json"),
        "template": ("t.tex.jinja", r"\documentclass{article}\begin{document}\nope\end{document}"),
    }
    response = client.post("/v1/cv/render", files=files)
    body = response.json()
    assert response.status_code == 422
    assert "compile" in body["error"]
    assert str(tmp_path) not in json.dumps(body)

    # The TeX log itself is behind /logs, so a failed render costs the caller
    # nothing until they ask for it.
    entries = client.get(f"/logs/{body['request_id']}").json()["data"]["entries"]
    assert any("Undefined control sequence" in e["message"] for e in entries)
    assert str(tmp_path) not in json.dumps(entries)
