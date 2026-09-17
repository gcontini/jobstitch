"""The three models are declarative config, and capabilities are flags."""

import pytest

from jobstitch_server import model_selector as ms

TOML = """
[provider]
api_key_env = "PROVIDER_KEY"
base_url_env = "PROVIDER_URL"
base_url = "https://provider.example/v1"

[defaults]
max_tokens = 8000
timeout_seconds = 30

[models.summary]
model = "summary-1"
temperature = 0.1
reasoning_effort = "low"
thinking = "on"
web_search = true

[models.cv]
model = "cv-1"
structured_output = "json_schema_strict"
thinking = "on"
thinking_budget = 6000
reasoning_effort = "high"

[models.highlight]
model = "highlight-1"
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
def provider_key(monkeypatch):
    monkeypatch.setenv("PROVIDER_KEY", "x")


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


def test_missing_model_name_is_rejected(tmp_path):
    toml = TOML.replace('model = "cv-1"', "")
    with pytest.raises(ValueError, match="missing 'model'"):
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


# --- [provider] --------------------------------------------------------------
def test_missing_provider_table_is_rejected(tmp_path):
    toml = TOML[TOML.index("[defaults]"):]
    with pytest.raises(ValueError, match=r"\[provider\]"):
        ms.load_model_config(write(tmp_path, toml))


def test_missing_provider_key_is_rejected(tmp_path):
    toml = TOML.replace('api_key_env = "PROVIDER_KEY"', "")
    with pytest.raises(ValueError, match="api_key_env"):
        ms.load_model_config(write(tmp_path, toml))


def test_provider_without_any_base_url_is_rejected(tmp_path):
    toml = TOML.replace('base_url_env = "PROVIDER_URL"', "").replace(
        'base_url = "https://provider.example/v1"', ""
    )
    with pytest.raises(ValueError, match="base_url"):
        ms.load_model_config(write(tmp_path, toml))


def test_unknown_provider_key_is_rejected(tmp_path):
    toml = TOML.replace("[provider]", "[provider]\ntyop = 1")
    with pytest.raises(ValueError, match="unknown key"):
        ms.load_model_config(write(tmp_path, toml))


def test_base_url_env_wins_over_literal(config, monkeypatch):
    provider = config.provider
    monkeypatch.setenv("PROVIDER_URL", "https://override.example/v1")
    assert provider.resolved_base_url() == "https://override.example/v1"
    monkeypatch.delenv("PROVIDER_URL")
    assert provider.resolved_base_url() == "https://provider.example/v1"


# --- env overrides -----------------------------------------------------------
def test_env_overrides_the_fundamental_settings(tmp_path, monkeypatch):
    # summary has no thinking_budget in the fixture, so overriding thinking
    # away from "on" doesn't collide with it (unlike cv).
    monkeypatch.setenv("JOBSTITCH_SUMMARY_MODEL", "summary-2")
    monkeypatch.setenv("JOBSTITCH_SUMMARY_TEMPERATURE", "0.7")
    monkeypatch.setenv("JOBSTITCH_SUMMARY_THINKING", "off")
    monkeypatch.setenv("JOBSTITCH_SUMMARY_STRUCTURED_OUTPUT", "json_schema")
    config = ms.load_model_config(write(tmp_path, TOML))
    summary = config.models["summary"]
    assert summary.model == "summary-2"
    assert summary.temperature == 0.7
    assert summary.thinking == "off"
    assert summary.structured_output == "json_schema"
    # Untouched roles keep their models.toml values.
    assert config.models["cv"].model == "cv-1"


def test_a_non_numeric_temperature_override_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSTITCH_CV_TEMPERATURE", "hot")
    with pytest.raises(ValueError, match="JOBSTITCH_CV_TEMPERATURE"):
        ms.load_model_config(write(tmp_path, TOML))


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


# --- building ---------------------------------------------------------------
def test_build_models_carries_the_declared_settings(config, provider_key):
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


def test_build_model_raises_naming_the_provider_var(config, monkeypatch):
    monkeypatch.delenv("PROVIDER_KEY", raising=False)
    with pytest.raises(RuntimeError, match="PROVIDER_KEY"):
        ms.build_model("cv", config)


def test_unknown_role_is_a_key_error(config):
    with pytest.raises(KeyError):
        ms.build_model("proofreader", config)


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
