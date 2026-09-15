"""The three models are declarative config, and capabilities are flags."""

import pytest

from jobstitch import model_selector as ms

TOML = """
[defaults]
max_tokens = 8000
timeout_seconds = 30

[models.summary]
model = "summary-1"
api_key_env = "SUMMARY_KEY"
base_url_env = "SUMMARY_URL"
base_url = "https://summary.example/v1"
temperature = 0.1
reasoning_effort = "low"
thinking = "on"
web_search = true

[models.cv]
model = "cv-1"
api_key_env = "CV_KEY"
base_url = "https://cv.example/v1"
structured_output = "json_schema_strict"
thinking = "on"
thinking_budget = 6000
reasoning_effort = "high"

[models.highlight]
model = "highlight-1"
api_key_env = "HIGHLIGHT_KEY"
base_url = "https://highlight.example/v1"
thinking = "off"
reasoning_effort = "high"
"""


def write(tmp_path, toml):
    (tmp_path / "models.toml").write_text(toml)
    return tmp_path


@pytest.fixture
def config(tmp_path):
    return ms.load_model_config(write(tmp_path, TOML))


@pytest.fixture
def all_keys(monkeypatch):
    for var in ("SUMMARY_KEY", "CV_KEY", "HIGHLIGHT_KEY"):
        monkeypatch.setenv(var, "x")


# --- parsing + validation ---------------------------------------------------
def test_parses_the_three_roles(config):
    assert sorted(config.models) == ["cv", "highlight", "summary"]
    assert config.models["cv"].thinking_budget == 6000
    assert config.models["summary"].web_search is True
    assert config.models["cv"].web_search is False  # defaulted
    assert config.max_tokens == 8000
    assert config.timeout == 30


def test_a_missing_role_is_rejected(tmp_path):
    toml = TOML.replace('[models.highlight]', '[models.unused]')
    with pytest.raises(ValueError, match=r"unknown model role"):
        ms.load_model_config(write(tmp_path, toml))


def test_declaring_only_two_roles_is_rejected(tmp_path):
    toml = TOML[: TOML.index("[models.highlight]")]
    with pytest.raises(ValueError, match=r"models\.highlight"):
        ms.load_model_config(write(tmp_path, toml))


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown key"):
        ms.load_model_config(write(tmp_path, TOML + '\ntyop = 1\n'))


def test_missing_required_key_is_rejected(tmp_path):
    toml = TOML.replace('api_key_env = "CV_KEY"', "")
    with pytest.raises(ValueError, match="api_key_env"):
        ms.load_model_config(write(tmp_path, toml))


def test_a_thinking_mode_that_does_not_exist_is_rejected(tmp_path):
    toml = TOML.replace('thinking = "off"', 'thinking = "maybe"')
    with pytest.raises(ValueError, match="expected one of"):
        ms.load_model_config(write(tmp_path, toml))


def test_a_budget_without_thinking_is_rejected(tmp_path):
    # A budget caps reasoning tokens; with thinking off there are none to cap.
    toml = TOML.replace('thinking = "off"', 'thinking = "off"\nthinking_budget = 6000')
    with pytest.raises(ValueError, match="thinking_budget needs"):
        ms.load_model_config(write(tmp_path, toml))


@pytest.mark.parametrize(
    "bad",
    ['reasoning_effort = "extreme"', 'structured_output = "yaml"'],
)
def test_capability_values_are_checked_against_the_allowed_set(tmp_path, bad):
    toml = TOML.replace('thinking = "off"\nreasoning_effort = "high"', bad)
    with pytest.raises(ValueError, match="expected one of"):
        ms.load_model_config(write(tmp_path, toml))


# --- request shaping --------------------------------------------------------
def test_thinking_on_sends_the_switch_and_the_budget(config):
    assert config.models["cv"].thinking_extra_body() == {
        "enable_thinking": True,
        "thinking_budget": 6000,
    }
    assert config.models["summary"].thinking_extra_body() == {"enable_thinking": True}


def test_thinking_off_forces_reasoning_off(config):
    highlight = config.models["highlight"]
    assert highlight.thinking_extra_body() == {"enable_thinking": False}
    # Nothing to spend effort on once thinking is off, so it is dropped.
    assert highlight.effective_reasoning_effort() is None


def test_thinking_auto_sends_nothing(tmp_path):
    toml = TOML.replace('thinking = "off"', 'thinking = "auto"')
    config = ms.load_model_config(write(tmp_path, toml))
    assert config.models["highlight"].thinking_extra_body() is None
    assert config.models["highlight"].effective_reasoning_effort() == "high"


def test_thinking_budget_drops_conflicting_reasoning_effort(config):
    # A budget and reasoning_effort are mutually exclusive; summary has no
    # budget at all, so its effort survives.
    assert config.models["cv"].effective_reasoning_effort() is None
    assert config.models["summary"].effective_reasoning_effort() == "low"


def test_base_url_env_wins_over_literal(config, monkeypatch):
    spec = config.models["summary"]
    monkeypatch.setenv("SUMMARY_URL", "https://override.example/v1")
    assert spec.resolved_base_url() == "https://override.example/v1"
    monkeypatch.delenv("SUMMARY_URL")
    assert spec.resolved_base_url() == "https://summary.example/v1"


# --- building ---------------------------------------------------------------
def test_build_models_carries_the_declared_settings(config, all_keys):
    models = ms.build_models(config, quiet=True)
    assert sorted(models) == ["cv", "highlight", "summary"]
    summary = models["summary"]
    assert summary.model == "summary-1"
    assert summary.temperature == 0.1
    assert summary.reasoning_effort == "low"
    assert summary.supports_web_search is True
    assert summary.max_tokens == 8000
    assert models["cv"].extra_body == {"enable_thinking": True, "thinking_budget": 6000}
    assert models["highlight"].temperature is None  # not declared -> provider default


def test_a_model_without_a_key_borrows_a_configured_endpoint(config, monkeypatch):
    monkeypatch.setenv("CV_KEY", "x")
    monkeypatch.delenv("HIGHLIGHT_KEY", raising=False)
    monkeypatch.delenv("SUMMARY_KEY", raising=False)

    highlight = ms.resolve_spec("highlight", config)
    # Endpoint and model name come from cv...
    assert highlight.model == "cv-1"
    assert highlight.base_url == "https://cv.example/v1"
    assert highlight.structured_output == "json_schema_strict"
    # ...but the role keeps what makes it the highlighter.
    assert highlight.role == "highlight"
    assert highlight.thinking_extra_body() == {"enable_thinking": False}


def test_a_configured_model_is_used_as_declared(config, all_keys):
    assert ms.resolve_spec("cv", config) is config.models["cv"]


def test_no_keys_at_all_raises_with_an_env_hint(config, monkeypatch):
    for var in ("SUMMARY_KEY", "CV_KEY", "HIGHLIGHT_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="CV_KEY, HIGHLIGHT_KEY, SUMMARY_KEY"):
        ms.resolve_spec("cv", config)


def test_unknown_role_is_a_key_error(config):
    with pytest.raises(KeyError):
        ms.resolve_spec("proofreader", config)


# --- ModelSelector capabilities --------------------------------------------
def test_response_format_follows_declared_capability():
    schema = {"type": "object"}
    strict = ms.ModelSelector("p", "k", "https://x/v1", "m",
                              structured_output="json_schema_strict")
    hint = ms.ModelSelector("p", "k", "https://x/v1", "m",
                            structured_output="json_schema")
    plain = ms.ModelSelector("p", "k", "https://x/v1", "m",
                             structured_output="json_object")
    none = ms.ModelSelector("p", "k", "https://x/v1", "m",
                            structured_output="none")

    assert strict.response_format("n", schema)["json_schema"]["strict"] is True
    assert hint.response_format("n", schema)["json_schema"]["strict"] is False
    assert plain.response_format("n", schema) == {"type": "json_object"}
    assert none.response_format("n", schema) is None
    # No schema to send: fall back to plain JSON mode.
    assert strict.response_format("n") == {"type": "json_object"}


def test_with_web_search_is_a_noop_without_the_capability():
    plain = ms.ModelSelector("p", "k", "https://x/v1", "m", supports_web_search=False)
    assert plain.with_web_search() is plain

    searchy = ms.ModelSelector("p", "k", "https://x/v1", "m",
                               supports_web_search=True,
                               extra_body={"enable_thinking": False})
    clone = searchy.with_web_search()
    # Merges rather than replacing, so provider keys already set survive.
    assert clone.extra_body == {"enable_thinking": False, "enable_search": True}
    assert searchy.extra_body == {"enable_thinking": False}


def test_with_rejects_unknown_overrides():
    sel = ms.ModelSelector("p", "k", "https://x/v1", "m")
    with pytest.raises(TypeError):
        sel.with_(temprature=0.5)
