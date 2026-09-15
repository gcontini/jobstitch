"""Resource resolution: your own files win, examples are the fallback."""

import pytest

from jobstitch import paths


def test_example_name_roundtrip():
    assert paths.example_name("candidate_data.json") == "candidate_data.example.json"
    assert paths.example_name("Makefile") == "Makefile.example"
    assert paths.strip_example("candidate_data.example.json") == "candidate_data.json"
    assert paths.strip_example("candidate_data.json") == "candidate_data.json"


def test_real_file_wins_over_example(tmp_path):
    (tmp_path / "candidate_data.json").write_text("real")
    (tmp_path / "candidate_data.example.json").write_text("fictional")
    found = paths.resource_path("candidate_data.json", tmp_path)
    assert found.read_text() == "real"


def test_falls_back_to_example(tmp_path):
    (tmp_path / "candidate_data.example.json").write_text("fictional")
    found = paths.resource_path("candidate_data.json", tmp_path)
    assert found.name == "candidate_data.example.json"


def test_missing_both_raises_with_guidance(tmp_path):
    with pytest.raises(FileNotFoundError) as excinfo:
        paths.resource_path("candidate_data.json", tmp_path)
    # The message has to tell a new user what to actually do.
    assert "JOBSTITCH_RESOURCES" in str(excinfo.value)


def test_shipped_resources_are_resolvable():
    """Every file the pipeline loads must exist, as itself or as an example."""
    for name in (
        "resume3.tex.jinja",
        "models.toml",
        "sys_prompt_cv.txt",
        "sys_prompt_highlight.txt",
        "sys_review_prompt.txt",
        "sys_prompt_letter.txt",
        *paths.PERSONAL_FILES,
    ):
        assert paths.resource_path(name).is_file(), name
