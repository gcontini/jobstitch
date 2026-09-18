"""Where settings come from, and in which order."""

from __future__ import annotations

import pytest

from jobstitch_client.config import ConfigError, load_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A folder that stands in for the current folder."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBSTITCH_API_URL", raising=False)
    monkeypatch.delenv("JOBSTITCH_API_TOKEN", raising=False)
    monkeypatch.delenv("JOBSTITCH_CONFIG", raising=False)
    return tmp_path


def test_files_in_the_current_folder_are_found_by_name(home):
    (home / "candidate_profile.json").write_text("{}")
    (home / "sys_prompt_cv.txt").write_text("prompt")

    config = load_config()

    assert config.path("candidate_profile.json") == home / "candidate_profile.json"
    assert config.path("sys_prompt_cv.txt") == home / "sys_prompt_cv.txt"
    assert config.path("resume.tex.jinja") is None, "absent means 'the server's default'"


def test_the_config_file_is_read_from_the_same_place(home):
    (home / "jobstitch.toml").write_text('server_url = "https://cv.example"\ntoken = "abc"\n')
    config = load_config()
    assert (config.server_url, config.token) == ("https://cv.example", "abc")


def test_the_environment_beats_the_config_file(home, monkeypatch):
    (home / "jobstitch.toml").write_text('server_url = "https://from-file"\n')
    monkeypatch.setenv("JOBSTITCH_API_URL", "https://from-env")
    assert load_config().server_url == "https://from-env"


def test_the_command_line_beats_everything(home, monkeypatch):
    (home / "jobstitch.toml").write_text('server_url = "https://from-file"\n')
    monkeypatch.setenv("JOBSTITCH_API_URL", "https://from-env")
    config = load_config(overrides={"server_url": "https://from-flag"})
    assert config.server_url == "https://from-flag"


def test_unset_flags_do_not_erase_the_config_file(home):
    (home / "jobstitch.toml").write_text('cover_letter = "yes"\n')
    assert load_config(overrides={"cover_letter": None}).cover_letter == "yes"


def test_an_explicit_path_overrides_discovery(home):
    (home / "candidate_profile.json").write_text("{}")
    elsewhere = home / "real"
    elsewhere.mkdir()
    (elsewhere / "mine.json").write_text("{}")
    (home / "jobstitch.toml").write_text(
        '[files]\nprofile = "real/mine.json"\n'
    )
    assert load_config().path("candidate_profile.json") == elsewhere / "mine.json"


def test_a_path_that_does_not_exist_is_refused(home):
    (home / "jobstitch.toml").write_text('[files]\nprofile = "nope.json"\n')
    with pytest.raises(ConfigError, match="does not exist"):
        load_config()


def test_an_unknown_file_entry_is_refused(home):
    (home / "jobstitch.toml").write_text('[files]\nwhatever = "x"\n')
    with pytest.raises(ConfigError, match="unknown entry"):
        load_config()


def test_a_bad_cover_letter_mode_is_refused(home):
    with pytest.raises(ConfigError, match="cover_letter"):
        load_config(overrides={"cover_letter": "maybe"})


def test_a_required_file_that_is_missing_says_where_to_put_it(home):
    with pytest.raises(ConfigError, match="in the current folder"):
        load_config().require("candidate_profile.json")


def test_every_image_in_the_folder_is_picked_up_by_its_own_name(home):
    (home / "candidate_signature.png").write_bytes(b"sig")
    (home / "photo.JPEG").write_bytes(b"me")
    (home / "logo.jpg").write_bytes(b"logo")
    (home / "JD.txt").write_text("not an image")

    images = load_config().image_parts()

    assert images == {"candidate_signature.png": b"sig", "photo.JPEG": b"me",
                      "logo.jpg": b"logo"}


def test_an_image_in_the_data_dir_beats_the_one_in_the_current_folder(home):
    (home / "logo.png").write_bytes(b"from cwd")
    elsewhere = home / "data"
    elsewhere.mkdir()
    (elsewhere / "logo.png").write_bytes(b"from data-dir")

    assert load_config(data_dir=elsewhere).image_parts() == {"logo.png": b"from data-dir"}
