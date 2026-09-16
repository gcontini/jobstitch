"""The output tree: naming, moves, and what recovery finds."""

from __future__ import annotations

import re

import pytest

from jobstitch_client.workspace import (
    Artifacts,
    Workspace,
    day,
    sanitize_name,
    strip_timestamp,
)


@pytest.fixture
def ws(tmp_path) -> Workspace:
    return Workspace(tmp_path / "out").ensure()


def drop(ws, name="posting.txt", text="a posting"):
    path = ws.root.parent / name
    path.write_text(text)
    return path


def test_ensure_builds_the_four_folders(ws):
    assert sorted(p.name for p in ws.root.iterdir()) == ["cv", "discarded", "error", "working"]


def test_intake_claims_the_file_immediately(ws):
    source = drop(ws)
    claimed = ws.take_in(source)

    assert not source.exists(), "the watched folder must not keep it"
    assert claimed.parent == ws.working
    assert re.match(r"^\d{2}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}_posting\.txt$", claimed.name)


def test_a_named_file_is_copied_not_consumed(ws):
    """`jobstitch submit posting.txt` must leave posting.txt where it is."""
    source = drop(ws)
    claimed = ws.take_in(source, move=False)

    assert source.exists(), "an argument is not an inbox"
    assert claimed.read_text() == source.read_text()
    assert claimed.parent == ws.working


def test_delivery_goes_into_a_daily_folder(ws):
    job = ws.open_job("Acme Corp", "Head of IT")
    delivered = ws.deliver(job)
    assert delivered.parent.name == day()
    assert delivered.parent.parent == ws.cv
    assert delivered.name == "Acme_Corp_Head_of_IT"


def test_a_second_run_of_the_same_job_does_not_overwrite_the_first(ws):
    first = ws.deliver(ws.open_job("Acme", "Head of IT"))
    second = ws.deliver(ws.open_job("Acme", "Head of IT"))
    assert first.exists() and second.exists()
    assert first != second


def test_error_keeps_an_existing_timestamp(ws):
    claimed = ws.take_in(drop(ws))
    failed = ws.to_error(claimed)
    assert failed.name == claimed.name, "the arrival time is the useful one"


def test_error_adds_a_timestamp_when_there_is_none(ws):
    job = ws.open_job("Acme", "Head of IT")
    failed = ws.to_error(job)
    assert failed.name.endswith("_Acme_Head_of_IT")
    assert strip_timestamp(failed.name) == "Acme_Head_of_IT"


def test_pending_separates_loose_files_from_job_folders(ws):
    ws.take_in(drop(ws, "one.txt"))
    ws.open_job("Acme", "Head of IT")
    files, folders = ws.pending()
    assert [f.name for f in folders] == ["Acme_Head_of_IT"]
    assert len(files) == 1


def test_clean_empties_the_working_folder(ws):
    ws.take_in(drop(ws, "one.txt"))
    ws.open_job("Acme", "Head of IT")
    assert ws.clean() == 2
    assert ws.pending() == ([], [])


@pytest.mark.parametrize(
    "raw, expected",
    [("Acme Corp.", "Acme_Corp"), ("  ", "unknown"), ("Head of IT / Ops", "Head_of_IT_Ops")],
)
def test_names_are_made_filesystem_safe(raw, expected):
    assert sanitize_name(raw) == expected


def test_artifacts_are_named_after_the_candidate():
    artifacts = Artifacts("Jordan Rivera")
    assert (artifacts.pdf, artifacts.tex, artifacts.document) == (
        "cv_Jordan_Rivera.pdf", "cv_Jordan_Rivera.tex", "cv_Jordan_Rivera.json")
