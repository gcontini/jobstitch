"""One posting through the pipeline: what lands where, and what it calls."""

from __future__ import annotations

import json

import pytest
from jobstitch_contracts import JDAnalysis

from jobstitch_client.runner import JobRunner
from jobstitch_client.sources import JDCandidate
from jobstitch_client.tracking import build_tracker
from jobstitch_client.ui import Decision
from jobstitch_client.workspace import Workspace

from jobstitch_client.api import JobstitchError

from conftest import JD_TEXT, FakeApi, ScriptedConfirmer, document


def build(api, workspace, config, *decisions, cover_letter="no", track=True) -> JobRunner:
    from dataclasses import replace

    return JobRunner(
        api=api,
        workspace=workspace,
        config=replace(config, cover_letter=cover_letter),
        confirmer=ScriptedConfirmer(*(decisions or (Decision(submit=True),))),
        tracker=build_tracker(workspace.root, track),
    )


def dropped(workspace: Workspace, name: str = "posting.txt", text: str = JD_TEXT):
    """A file claimed into working/, as the watcher would hand it over."""
    inbox = workspace.root.parent / "inbox"
    inbox.mkdir(exist_ok=True)
    path = inbox / name
    path.write_text(text, encoding="utf-8")
    claimed = workspace.take_in(path)
    return JDCandidate(text=text, origin=claimed, label=name)


def test_a_delivered_job_has_every_artifact(runner, workspace):
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    assert outcome.status == "delivered"
    folder = outcome.path
    assert folder.parent.parent == workspace.cv
    assert folder.name == "Acme_Corp_Head_of_IT"
    assert sorted(p.name for p in folder.iterdir()) == [
        "analysis.json", "cv_Jordan_Rivera.json", "cv_Jordan_Rivera.pdf",
        "cv_Jordan_Rivera.tex", "jd.txt", "log.log",
    ]
    assert (folder / "cv_Jordan_Rivera.pdf").read_bytes() == b"%PDF-fake"
    assert (folder / "jd.txt").read_text() == JD_TEXT


def test_the_stored_document_is_the_reply_kept_exactly_as_it_arrived(runner, api):
    """One flat object, not read here at all: a server that learns to write
    one more field must not mean a client to upgrade first."""
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    stored = json.loads((outcome.path / "cv_Jordan_Rivera.json").read_text())
    assert stored == api.rendered.document
    assert stored["name"] == "Jordan Rivera"


def test_your_candidate_data_is_sent_so_the_page_count_is_real(runner, api):
    runner.handle(JDCandidate(text=JD_TEXT))
    assert json.loads(api.seen_candidate_data)["name"] == "Jordan Rivera"


def test_each_new_status_is_reported_as_it_happens(runner, api):
    """The wait is minutes long; a silent one is indistinguishable from a
    hung one."""
    from jobstitch_contracts import CVStatus

    api.statuses = [
        CVStatus(status="generate", detail=""),
        CVStatus(status="review", detail="generation finished, tokens used=99"),
        CVStatus(status="END", detail="done"),
    ]
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    log = (outcome.path / "log.log").read_text()
    assert "generate" in log and "review" in log and "END" in log
    assert "tokens used=99" not in log, "that is what --verbose is for"


def test_verbose_adds_what_the_server_said_about_each_step(api, workspace, config):
    from dataclasses import replace

    from jobstitch_contracts import CVStatus

    api.statuses = [
        CVStatus(status="review", detail="generation finished, tokens used=99"),
        CVStatus(status="END", detail="done"),
    ]
    runner = build(api, workspace, replace(config, verbose=True))
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    assert "tokens used=99" in (outcome.path / "log.log").read_text()


def test_the_working_folder_is_left_empty(runner, workspace):
    runner.handle(JDCandidate(text=JD_TEXT))
    assert workspace.pending() == ([], [])


def test_a_dropped_file_keeps_its_own_name(api, workspace, config):
    runner = build(api, workspace, config)
    outcome = runner.handle(dropped(workspace, "ENGIE_infra_lead.txt"))
    assert (outcome.path / "ENGIE_infra_lead.txt").is_file()
    assert not (outcome.path / "jd.txt").exists()


def test_the_log_records_the_client_steps(runner):
    outcome = runner.handle(JDCandidate(text=JD_TEXT))
    log = (outcome.path / "log.log").read_text()
    assert "analyzing the posting" in log
    assert "writing the CV" in log


def test_a_quiet_run_never_asks_for_the_server_log(runner, api):
    runner.handle(JDCandidate(text=JD_TEXT))
    assert "logs" not in api.calls, "a successful run costs one round trip per step"


def test_debug_folds_in_every_server_log(api, workspace, config):
    from dataclasses import replace

    runner = build(api, workspace, replace(config, debug=True))
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    log = (outcome.path / "log.log").read_text()
    assert "--- server request test-request (1 line(s)) ---" in log
    assert "prompt=10, completion=20" in log, "the token spend is in the server's lines"
    assert api.calls.count("logs") == 3, "detect, analyze, and the CV job"


def test_a_failure_always_fetches_the_server_log(api, workspace, config):
    api.fail_on = "create_cv"
    runner = build(api, workspace, config)
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    assert outcome.status == "failed"
    log = (outcome.path / "log.log").read_text()
    assert "--- server request failed-request (1 line(s)) ---" in log
    assert api.calls.count("logs") == 1, "only the failed request, and without --debug"


def test_an_unfetchable_server_log_does_not_mask_the_failure(api, workspace, config):
    class Stubborn(FakeApi):
        def logs(self, request_id):
            raise JobstitchError("unknown_request: logs are gone", status=404)

    api = Stubborn(fail_on="create_cv")
    outcome = build(api, workspace, config).handle(JDCandidate(text=JD_TEXT))

    assert outcome.status == "failed" and "create_cv failed" in outcome.message, outcome.message
    assert "could not fetch the server log" in (outcome.path / "log.log").read_text()


def test_the_spreadsheet_gets_a_row(runner, workspace):
    import openpyxl

    runner.handle(JDCandidate(text=JD_TEXT))
    sheet = openpyxl.load_workbook(workspace.root / "applications.xlsx").active
    assert [c.value for c in sheet[2]][:2] == ["Acme Corp", "Head of IT"]


def test_tracking_can_be_turned_off(api, workspace, config):
    build(api, workspace, config, track=False).handle(JDCandidate(text=JD_TEXT))
    assert not (workspace.root / "applications.xlsx").exists()


def test_a_broken_spreadsheet_costs_a_warning_and_not_the_job(api, workspace, config):
    """The CV was paid for and is filed under cv/ before the row is written;
    bookkeeping on top of it must not turn that back into a failure — or, in
    watch mode, kill the loop that is waiting for the next posting."""

    class Broken:
        def record(self, job_dir, analysis):
            raise RuntimeError(f"{workspace.root}/applications.xlsx has no date column")

    runner = JobRunner(
        api=api, workspace=workspace, config=config,
        confirmer=ScriptedConfirmer(Decision(submit=True)), tracker=Broken(),
    )
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    assert outcome.status == "delivered"
    assert outcome.path.parent.parent == workspace.cv
    log = (outcome.path / "log.log").read_text()
    assert "could not record this job in applications.xlsx" in log
    assert "has no date column" in log, "the underlying error is what you act on"


def test_junk_never_reaches_the_server(runner, api):
    outcome = runner.handle(JDCandidate(text="too short to be a posting"))
    assert outcome.status == "rejected"
    assert api.calls == [], "the free check has to come first"


def test_a_rejected_file_lands_in_error_with_a_timestamp(api, workspace, config):
    api.is_jd = False
    runner = build(api, workspace, config)
    outcome = runner.handle(dropped(workspace, "not_a_posting.txt"))

    assert outcome.status == "rejected"
    assert outcome.path.parent == workspace.error
    assert outcome.path.name.endswith("_not_a_posting.txt")
    assert outcome.path.name[:2].isdigit(), "timestamp prefix"


def test_saying_no_files_it_under_discarded(api, workspace, config):
    runner = build(api, workspace, config, Decision(submit=False))
    outcome = runner.handle(JDCandidate(text=JD_TEXT))

    assert outcome.status == "discarded"
    assert outcome.path.parent.parent == workspace.discarded
    assert (outcome.path / "analysis.json").is_file(), "the analysis is kept, it was paid for"
    assert api.calls == ["detect", "analyze"], "nothing expensive ran"


def test_the_pasted_url_is_stored_in_the_analysis(api, workspace, config):
    runner = build(api, workspace, config, Decision(submit=True, url="https://jobs.example/42"))
    outcome = runner.handle(JDCandidate(text=JD_TEXT))
    stored = JDAnalysis.model_validate_json((outcome.path / "analysis.json").read_text())
    assert stored.posting_url == "https://jobs.example/42"


def test_a_failure_lands_in_error_with_its_log(api, workspace, config):
    api.fail_on = "create_cv"
    runner = build(api, workspace, config)
    outcome = runner.handle(dropped(workspace))

    assert outcome.status == "failed"
    assert outcome.path.parent == workspace.error
    assert "model_output" in (outcome.path / "log.log").read_text()
    assert (outcome.path / "analysis.json").is_file()


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("no", ["detect", "analyze", "create_cv", "cv_status", "cv_result"]),
        ("yes", ["detect", "analyze", "create_cv", "cv_status", "cv_result", "letter"]),
        ("letter_only", ["detect", "analyze", "letter"]),
    ],
)
def test_cover_letter_modes_decide_what_runs(api, workspace, config, mode, expected):
    outcome = build(api, workspace, config, cover_letter=mode).handle(
        JDCandidate(text=JD_TEXT)
    )
    assert api.calls == expected
    assert (outcome.path / "cover_letter.txt").is_file() is (mode != "no")


def test_your_own_prompts_and_template_are_sent(api, workspace, config, tmp_path):
    from dataclasses import replace

    prompt = tmp_path / "sys_prompt_cv.txt"
    prompt.write_text("MY PROMPT")
    template = tmp_path / "resume.tex.jinja"
    template.write_text("MY TEMPLATE")
    files = {**config.files, "sys_prompt_cv.txt": prompt, "resume.tex.jinja": template}
    build(api, workspace, replace(config, files=files)).handle(JDCandidate(text=JD_TEXT))

    assert api.seen_prompts == {"sys_prompt_cv": "MY PROMPT"}
    assert api.seen_template == "MY TEMPLATE"
    assert "candidate_signature.png" in api.seen_images


# --- recovery ---------------------------------------------------------------
def test_resuming_a_folder_skips_the_analysis(runner, workspace, api):
    runner.handle(JDCandidate(text=JD_TEXT))
    delivered = list((workspace.cv).rglob("Acme_Corp_Head_of_IT"))[0]
    import shutil

    shutil.move(str(delivered), str(workspace.working / delivered.name))
    api.calls.clear()

    outcome = runner.resume(workspace.working / delivered.name)

    assert outcome.status == "delivered"
    assert "analyze" not in api.calls


def test_resuming_reuses_the_document_and_only_re_renders(runner, workspace, api):
    job = workspace.open_job("Acme Corp", "Head of IT")
    (job / "jd.txt").write_text(JD_TEXT)
    (job / "analysis.json").write_text(
        __import__("conftest").analysis().model_dump_json()
    )
    (job / "cv_Jordan_Rivera.json").write_text(json.dumps(document()))

    outcome = runner.resume(job)

    assert outcome.status == "delivered"
    assert api.calls == ["render"], "the CV was already written and paid for"


def test_resuming_a_folder_without_an_analysis_fails_it(runner, workspace):
    job = workspace.open_job("Broken", "Folder")
    outcome = runner.resume(job)
    assert outcome.status == "failed"
    assert outcome.path.parent == workspace.error
