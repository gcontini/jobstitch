"""The incoming/ folder contract, and what survives a successful job."""

import json

from jobstitch.job_watcher import clean_success_folder, valid_job_folder


def make_job(tmp_path, jd="A job description.", name="Globex_Staff_Engineer"):
    folder = tmp_path / name
    folder.mkdir(parents=True)
    if jd is not None:
        (folder / "JD.txt").write_text(jd)
    return folder


def test_a_jd_is_the_whole_contract(tmp_path):
    assert valid_job_folder(make_job(tmp_path)) == (True, "")


def test_rejects_missing_or_empty_jd(tmp_path):
    ok, reason = valid_job_folder(make_job(tmp_path, jd=None))
    assert not ok and "JD.txt" in reason
    ok, reason = valid_job_folder(make_job(tmp_path / "b", jd="   "))
    assert not ok and "JD.txt" in reason


def test_the_folder_name_is_never_inspected(tmp_path):
    """Company_JobTitle is a convention for the reader, not a parsed format."""
    assert valid_job_folder(make_job(tmp_path, name="whatever I like")) == (True, "")


def test_analysis_json_is_optional_and_unread(tmp_path):
    """A JD source may leave one; the watcher neither needs it nor looks at
    it, so even an unparsable one is none of its business."""
    folder = make_job(tmp_path)
    (folder / "analysis.json").write_text("{not json")
    assert valid_job_folder(folder) == (True, "")


def test_clean_success_folder_drops_only_the_leftovers(tmp_path):
    folder = make_job(tmp_path)
    (folder / "analysis.json").write_text(json.dumps({"posting_url": "https://x"}))
    kept = ("cv.tex", "cv.pdf", "candidate_signature.png", "cover_letter.txt",
            "cv_data.json", "notes_from_my_own_jd_source.md")
    for name in kept:
        (folder / name).write_text("x")
    for name in ("cv.aux", "cv.log", "job.log", "cv.out"):
        (folder / name).write_text("x")
    (folder / "drafts").mkdir()
    (folder / "drafts" / "proposer0.md").write_text("x")

    clean_success_folder(folder)

    assert {p.name for p in folder.iterdir()} == {"JD.txt", "analysis.json", *kept}
