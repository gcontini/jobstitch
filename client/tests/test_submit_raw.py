"""``jobstitch submit-raw``: a job description in, files in this folder out."""

from __future__ import annotations

import json

import pytest

from jobstitch_client.modes import submit_raw

from conftest import JD_TEXT


@pytest.fixture
def here(tmp_path, monkeypatch):
    """Run in an empty directory: the mode writes to wherever you stand."""
    monkeypatch.chdir(tmp_path)
    jd = tmp_path / "posting.txt"
    jd.write_text(JD_TEXT)
    return jd


@pytest.fixture(autouse=True)
def one_api(api, monkeypatch):
    monkeypatch.setattr(submit_raw, "HttpApi", lambda *a, **kw: api)
    return api


def test_without_an_output_it_writes_a_pdf_named_after_the_posting(here, config, api):
    assert submit_raw.run(config, here, []) == 0
    assert (here.parent / "posting.pdf").read_bytes() == b"%PDF-fake"


def test_a_second_run_counts_up_rather_than_replacing_yesterdays_cv(here, config):
    submit_raw.run(config, here, [])
    submit_raw.run(config, here, [])
    assert (here.parent / "posting_1.pdf").is_file()


def test_the_suffix_is_the_whole_instruction(here, config, tmp_path, api):
    from pathlib import Path

    submit_raw.run(config, here, [Path("cv.pdf"), Path("cv.json"), Path("cv.tex")])

    assert (tmp_path / "cv.pdf").read_bytes() == b"%PDF-fake"
    assert json.loads((tmp_path / "cv.json").read_text()) == api.rendered.document
    assert (tmp_path / "cv.tex").read_text() == api.rendered.tex


def test_a_name_you_chose_is_overwritten_without_asking(here, config, tmp_path):
    from pathlib import Path

    (tmp_path / "cv.pdf").write_bytes(b"old")
    submit_raw.run(config, here, [Path("cv.pdf")])
    assert (tmp_path / "cv.pdf").read_bytes() == b"%PDF-fake"


def test_an_unknown_suffix_is_refused_before_anything_is_spent(here, config, api):
    from pathlib import Path

    with pytest.raises(SystemExit):
        submit_raw.run(config, here, [Path("cv.docx")])
    assert api.calls == []


def test_it_only_writes_the_cv(here, config, api, tmp_path):
    """No detection, no analysis, no letter, no spreadsheet, no folders."""
    submit_raw.run(config, here, [])

    assert api.calls == ["create_cv", "cv_status", "cv_result"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["posting.pdf", "posting.txt"]


def test_a_missing_posting_stops_before_the_server(config, api, tmp_path):
    from pathlib import Path

    with pytest.raises(SystemExit):
        submit_raw.run(config, Path(tmp_path / "nope.txt"), [])
    assert api.calls == []


def test_a_failed_job_is_reported_with_its_request_id(here, config, api, capsys):
    api.fail_on = "create_cv"
    with pytest.raises(SystemExit):
        submit_raw.run(config, here, [])
    assert "failed-request" in capsys.readouterr().err
