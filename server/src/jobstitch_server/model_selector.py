"""Centralized LLM model selection.

jobstitch calls exactly three models, one per job, all declared in
``resources/models.toml`` and sharing one provider (one API key/endpoint,
declared once in the ``[provider]`` table):

``summary``
    JD detection, JD analysis and the cover letter — mid-size, light thinking,
    and the only model allowed to run server-side web research.
``cv``
    CV writing and the content review — the large one, with a thinking budget.
``highlight``
    The Markdown ``**bold**`` pass over already-validated CV JSON — mid-size,
    thinking off.

A :class:`ModelSelector` bundles everything needed to call one of them —
endpoint credentials, model name, generation parameters and the provider
capabilities that matter — so callers never construct an OpenAI client or pick
parameters themselves: ask :func:`build_models` for all three (or
:func:`build_model` for one), then call
:meth:`ModelSelector.completions_create`.

Nothing here knows about a specific provider — provider quirks are declared as
capability flags rather than written as ``if name == ...`` branches. The
fundamental per-role settings (``model``, ``temperature``, ``thinking``,
``structured_output``) can each be overridden by an env var named
``JOBSTITCH_<ROLE>_<FIELD>`` (e.g. ``JOBSTITCH_CV_TEMPERATURE``), so a Docker
deployment can tune a role without editing ``models.toml``.
"""

from __future__ import annotations

import copy
import logging
import os
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI

from .defaults import default_path
from .observability import LOGGER_ROOT

logger = logging.getLogger(f"{LOGGER_ROOT}.models")

MODELS_FILE = "models.toml"

#: The three jobs jobstitch has a model for, in the order a run uses them.
MODEL_ROLES = ("summary", "cv", "highlight")

# Fallback used when models.toml omits it. max_tokens has no fallback: when
# neither a model nor [defaults] declares it, the request omits max_tokens
# entirely and the provider's own default applies.
DEFAULT_TIMEOUT = 600

# Accepted values for the declarative capability keys.
THINKING_MODES = ("auto", "on", "off")
REASONING_EFFORTS = ("low", "medium", "high")
STRUCTURED_OUTPUTS = ("json_schema_strict", "json_schema", "json_object", "none")

# Minimal documented form of the server-side web-search switch. DashScope also
# accepts `search_options` ({"search_strategy": "turbo"|"max"|"agent"},
# "forced_search", ...) — that is the knob to add here if results come back thin.
WEB_SEARCH_EXTRA_BODY = {"enable_search": True}

# The thinking switch, as OpenAI-compatible endpoints that have one spell it.
THINKING_SWITCH = "enable_thinking"
THINKING_BUDGET = "thinking_budget"


# ---------------------------------------------------------------------------
# Declarative model table, loaded from resources/models.toml.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderConfig:
    """The one LLM provider every role calls, as written in ``models.toml``.

    jobstitch assumes a single provider/single API key — there is nothing to
    fall back to, so a missing key is a hard startup error naming this one
    variable rather than a per-role concern.
    """

    api_key_env: str
    base_url_env: Optional[str] = None
    base_url: Optional[str] = None

    def resolved_base_url(self) -> Optional[str]:
        """Endpoint URL: the env var when set, otherwise the declared literal."""
        if self.base_url_env:
            from_env = os.getenv(self.base_url_env)
            if from_env:
                return from_env
        return self.base_url

    def resolved_api_key(self) -> Optional[str]:
        """The API key from the environment, or ``None`` when it is not set."""
        return os.getenv(self.api_key_env) or None

    def is_usable(self) -> bool:
        """True when both the key and an endpoint URL are available."""
        return bool(self.resolved_api_key() and self.resolved_base_url())


@dataclass(frozen=True)
class ModelSpec:
    """One of the three models, as written in ``models.toml``.

    Provider differences are capabilities, not names: ``web_search``,
    ``thinking`` and ``structured_output`` are what :func:`build_model`
    branches on.
    """

    role: str
    model: str
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    thinking: str = "auto"                 # "auto" | "on" | "off"
    thinking_budget: Optional[int] = None  # reasoning-token cap, thinking = "on"
    web_search: bool = False
    # "json_schema_strict" | "json_schema" | "json_object" | "none"
    structured_output: str = "json_object"
    max_tokens: Optional[int] = None       # overrides [defaults].max_tokens

    # --- request shaping ---------------------------------------------------
    def thinking_extra_body(self) -> Optional[Dict[str, Any]]:
        """The ``extra_body`` this model's thinking setting asks for.

        ``"auto"`` sends nothing (the provider decides — also the right value
        for endpoints that reject a thinking switch outright). ``"on"`` enables
        thinking and adds ``thinking_budget`` when one is declared; ``"off"``
        forces it off, which cheap mechanical passes need: a hybrid-reasoning
        model that keeps thinking spends the whole output budget on reasoning
        tokens and returns truncated content.
        """
        if self.thinking == "off":
            return {THINKING_SWITCH: False}
        if self.thinking == "on":
            body: Dict[str, Any] = {THINKING_SWITCH: True}
            if self.thinking_budget is not None:
                body[THINKING_BUDGET] = self.thinking_budget
            return body
        return None

    def effective_reasoning_effort(self) -> Optional[str]:
        """``reasoning_effort`` for the request, dropped where it cannot go.

        Thinking forced off leaves no effort to set. Reasoning effort and a
        thinking budget are mutually exclusive — where a budget is declared,
        it alone controls thinking.
        """
        if self.thinking == "off":
            return None
        if self.thinking_budget is not None:
            return None
        return self.reasoning_effort

    def summary_line(self) -> str:
        """One-line description for startup output: model + thinking setting."""
        bits = [self.model]
        if self.thinking == "on":
            budget = f" {self.thinking_budget}" if self.thinking_budget else ""
            bits.append(f"thinking{budget}")
        elif self.thinking == "off":
            bits.append("no thinking")
        effort = self.effective_reasoning_effort()
        if effort:
            bits.append(f"effort {effort}")
        if self.web_search:
            bits.append("web search")
        return f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]


@dataclass
class ModelConfig:
    """Parsed ``models.toml``: the provider, the three models, ``[defaults]``."""

    provider: ProviderConfig
    models: Dict[str, ModelSpec] = field(default_factory=dict)
    defaults: Dict[str, Any] = field(default_factory=dict)

    @property
    def max_tokens(self) -> Optional[int]:
        value = self.defaults.get("max_tokens")
        return int(value) if value is not None else None

    @property
    def timeout(self) -> float:
        return float(self.defaults.get("timeout_seconds", DEFAULT_TIMEOUT))


_ALLOWED_KEYS = {f.name for f in ModelSpec.__dataclass_fields__.values()} - {"role"}
_PROVIDER_ALLOWED_KEYS = {f.name for f in ProviderConfig.__dataclass_fields__.values()}

#: Per-role fields a Docker deployment can override without editing
#: models.toml, via JOBSTITCH_<ROLE>_<FIELD> (e.g. JOBSTITCH_CV_TEMPERATURE).
ENV_OVERRIDABLE_FIELDS = ("model", "temperature", "thinking", "structured_output")


def _apply_env_overrides(role: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Per-role env vars win over models.toml for the fundamental settings."""
    body = dict(body)
    for field_name in ENV_OVERRIDABLE_FIELDS:
        env_var = f"JOBSTITCH_{role.upper()}_{field_name.upper()}"
        value = os.getenv(env_var)
        if value is None:
            continue
        if field_name == "temperature":
            try:
                value = float(value)
            except ValueError:
                raise ValueError(f"{env_var}={value!r} is not a number") from None
        body[field_name] = value
    return body


def _validate(spec: ModelSpec, path: Path) -> None:
    """Reject values the request builder could not act on, at load time."""
    where = f"{path}: [models.{spec.role}]"
    if spec.thinking not in THINKING_MODES:
        raise ValueError(
            f"{where}: thinking = {spec.thinking!r}; expected one of "
            f"{list(THINKING_MODES)}"
        )
    if spec.reasoning_effort is not None and spec.reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(
            f"{where}: reasoning_effort = {spec.reasoning_effort!r}; expected "
            f"one of {list(REASONING_EFFORTS)}"
        )
    if spec.structured_output not in STRUCTURED_OUTPUTS:
        raise ValueError(
            f"{where}: structured_output = {spec.structured_output!r}; expected "
            f"one of {list(STRUCTURED_OUTPUTS)}"
        )
    if spec.thinking_budget is not None:
        if spec.thinking != "on":
            raise ValueError(
                f"{where}: thinking_budget needs thinking = \"on\" "
                f"(it is {spec.thinking!r})"
            )
        if spec.thinking_budget <= 0:
            raise ValueError(
                f"{where}: thinking_budget = {spec.thinking_budget}; expected a "
                "positive number of tokens"
            )
    if spec.max_tokens is not None and spec.max_tokens <= 0:
        raise ValueError(
            f"{where}: max_tokens = {spec.max_tokens}; expected a positive "
            "number of tokens"
        )



def _models_path(resources_dir: Optional[Path] = None) -> Path:
    """Locate ``models.toml``: an explicit directory wins, else the shipped copy."""
    if resources_dir is not None:
        candidate = Path(resources_dir) / MODELS_FILE
        if not candidate.is_file():
            raise FileNotFoundError(f"{candidate} does not exist")
        return candidate
    return default_path(MODELS_FILE)


def load_model_config(resources_dir: Optional[Path] = None) -> ModelConfig:
    """Parse ``models.toml`` from the resources folder.

    Raises ``ValueError`` when ``[provider]`` or a role is missing or unknown,
    when a key is misspelled or carries a value nothing acts on, or when a
    model omits ``model`` — a typo in the config surfaces at startup instead
    of halfway through a job. ``model``, ``temperature``, ``thinking`` and
    ``structured_output`` are read after applying any
    ``JOBSTITCH_<ROLE>_<FIELD>`` env override (see
    :data:`ENV_OVERRIDABLE_FIELDS`).
    """
    path = _models_path(resources_dir)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

    provider_body = raw.get("provider")
    if not provider_body:
        raise ValueError(
            f"{path}: no [provider] table — jobstitch needs one provider's "
            "endpoint, shared by all three models"
        )
    unknown_provider_keys = set(provider_body) - _PROVIDER_ALLOWED_KEYS
    if unknown_provider_keys:
        raise ValueError(
            f"{path}: [provider] has unknown key(s) {sorted(unknown_provider_keys)}; "
            f"allowed: {sorted(_PROVIDER_ALLOWED_KEYS)}"
        )
    if not provider_body.get("api_key_env"):
        raise ValueError(f"{path}: [provider] is missing 'api_key_env'")
    if not provider_body.get("base_url_env") and not provider_body.get("base_url"):
        raise ValueError(f"{path}: [provider] needs 'base_url_env' or 'base_url'")
    provider = ProviderConfig(**provider_body)

    declared = raw.get("models") or {}
    unknown_roles = set(declared) - set(MODEL_ROLES)
    if unknown_roles:
        raise ValueError(
            f"{path}: unknown model role(s) {sorted(unknown_roles)}; jobstitch "
            f"uses exactly {list(MODEL_ROLES)}"
        )
    missing_roles = [role for role in MODEL_ROLES if role not in declared]
    if missing_roles:
        raise ValueError(
            f"{path}: no [models.{missing_roles[0]}] table — all of "
            f"{list(MODEL_ROLES)} must be declared (missing: {missing_roles})"
        )

    models: Dict[str, ModelSpec] = {}
    for role in MODEL_ROLES:
        body = _apply_env_overrides(role, declared[role])
        unknown = set(body) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"{path}: [models.{role}] has unknown key(s) {sorted(unknown)}; "
                f"allowed: {sorted(_ALLOWED_KEYS)}"
            )
        if not body.get("model"):
            raise ValueError(
                f"{path}: [models.{role}] is missing 'model'"
            )
        spec = ModelSpec(role=role, **body)
        _validate(spec, path)
        models[role] = spec

    return ModelConfig(provider=provider, models=models, defaults=raw.get("defaults") or {})


def describe_response_format(
    response_format: Optional[Dict[str, Any]],
) -> str:
    """One-token description of a ``response_format``, for the request log.

    The schema body itself is far too long to log per call; what matters when
    a reply comes back off-schema is which mode was actually asked for and
    whether the endpoint was told to enforce it.
    """
    if response_format is None:
        return "response_format=omitted"
    kind = response_format.get("type")
    if kind == "json_schema":
        block = response_format.get("json_schema") or {}
        return (
            f"response_format=json_schema(name={block.get('name')}, "
            f"strict={block.get('strict')})"
        )
    return f"response_format={kind}"


class ModelSelector:
    """Bundle one LLM endpoint + model + generation parameters.

    Parameters
    ----------
    profile:
        Short human-readable name/label for this model (the role name).
    api_key:
        API key for the endpoint.
    base_url:
        Base URL of the OpenAI-compatible endpoint.
    model:
        Model name passed to ``chat.completions.create``.
    max_tokens:
        Optional max output tokens; omitted from the request when ``None``.
    temperature:
        Optional sampling temperature; omitted when ``None``.
    frequency_penalty:
        Optional frequency penalty (-2.0..2.0); omitted when ``None``.
    presence_penalty:
        Optional presence penalty (-2.0..2.0); omitted when ``None``.
    timeout:
        Optional client-level request timeout in seconds.
    max_retries:
        Optional client-level retry count.
    extra_body:
        Optional dict forwarded as ``extra_body`` to the request (provider
        specific parameters, e.g. ``{"thinking_budget": N}``). Omitted when
        ``None``.
    reasoning_effort:
        Optional ``reasoning_effort`` passed to the request (e.g. ``"low"``,
        ``"medium"``, ``"high"``). Omitted when ``None``.
    supports_web_search:
        Whether this endpoint runs server-side web search when asked (see
        :meth:`with_web_search`), from the model's ``web_search`` flag.
    structured_output:
        ``"json_schema_strict"``, ``"json_schema"``, ``"json_object"`` or
        ``"none"`` — what the endpoint accepts as a ``response_format``; see
        :meth:`response_format`.
    """

    def __init__(
        self,
        profile: str,
        api_key: str,
        base_url: str,
        model: str,
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        frequency_penalty: Optional[float] = None,
        presence_penalty: Optional[float] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
        supports_web_search: bool = False,
        structured_output: str = "json_object",
    ) -> None:
        self.profile = profile
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.frequency_penalty = frequency_penalty
        self.presence_penalty = presence_penalty
        self.extra_body = extra_body
        self.reasoning_effort = reasoning_effort
        self.supports_web_search = supports_web_search
        self.structured_output = structured_output

        client_kwargs: Dict[str, Any] = {"api_key": api_key, "base_url": base_url}
        client_kwargs["timeout"] = DEFAULT_TIMEOUT if timeout is None else timeout
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        self.llm = OpenAI(**client_kwargs)

    # --- capability helpers -------------------------------------------------
    def response_format(
        self, name: str, schema: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Best ``response_format`` this endpoint accepts for ``schema``.

        Providers differ in how much structure they will enforce, so callers
        ask for the strongest option and get back what is actually supported:
        a ``json_schema`` block (strict only where the endpoint accepts the
        full JSON Schema vocabulary — strict mode on some providers rejects
        keywords Pydantic emits, such as ``minItems``), a plain
        ``json_object`` request, or ``None`` when the endpoint takes no
        ``response_format`` at all. Callers also restate the schema in the
        prompt and validate the parsed payload with Pydantic, so a weaker
        format costs a retry at worst, never correctness.
        """
        if self.structured_output == "none":
            return None
        if schema is not None and self.structured_output in (
            "json_schema",
            "json_schema_strict",
        ):
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "strict": self.structured_output == "json_schema_strict",
                    "schema": schema,
                },
            }
        return {"type": "json_object"}

    def sends_schema(self) -> bool:
        """True when :meth:`response_format` puts the schema in the request.

        When it is ``False`` the endpoint is limited to ``{"type":
        "json_object"}`` (or takes no ``response_format`` at all) and the
        prompt is the only place it can learn the shape it must produce — so
        callers restate the schema there, and only there.
        """
        return self.structured_output in ("json_schema", "json_schema_strict")

    def completions_create(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
    ):
        """Run ``self.llm.chat.completions.create`` with only the parameters
        specified in the constructor (plus the required ``model``/``messages``).

        Optional generation parameters left ``None`` at construction are
        omitted so the provider's defaults apply. ``response_format`` is
        forwarded only when provided.

        Two lines are logged per call: what was sent (every parameter that
        shapes the reply, so a request that came back truncated or off-schema
        can be read back from the log without reproducing it) and what came
        back (duration, token usage and ``finish_reason``). The prompt and the
        reply themselves are logged at DEBUG only — they carry the candidate's
        profile.
        """
        kwargs: Dict[str, Any] = {"model": self.model, "messages": messages}
        if response_format is not None:
            kwargs["response_format"] = response_format
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.frequency_penalty is not None:
            kwargs["frequency_penalty"] = self.frequency_penalty
        if self.presence_penalty is not None:
            kwargs["presence_penalty"] = self.presence_penalty
        if self.extra_body is not None:
            kwargs["extra_body"] = self.extra_body
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort

        logger.info(
            "  → [%s] %s | %s, max_tokens=%s, temperature=%s, "
            "reasoning_effort=%s, extra_body=%s, input=%d msgs/%d chars",
            self.profile,
            self.model,
            describe_response_format(response_format),
            kwargs.get("max_tokens", "provider default"),
            kwargs.get("temperature", "provider default"),
            kwargs.get("reasoning_effort", "provider default"),
            kwargs.get("extra_body", "-"),
            len(messages),
            sum(len(m.get("content") or "") for m in messages),
        )
        logger.debug("  → [%s] messages: %s", self.profile, messages)

        t0 = time.perf_counter()
        resp = self.llm.chat.completions.create(**kwargs)
        dt = time.perf_counter() - t0

        # One line per call, with what it cost. This is the whole of token
        # accounting: the log is the ledger, so there is no second copy to
        # keep in step with it. finish_reason rides along because "length"
        # is the difference between a model that answered badly and one that
        # was cut off mid-answer.
        choice = resp.choices[0] if resp.choices else None
        finish = getattr(choice, "finish_reason", None) if choice else None
        content = getattr(choice.message, "content", None) if choice else None
        usage = getattr(resp, "usage", None)
        if usage is None:
            logger.info(
                "  ⏱ [%s] %s %.1fs | usage not reported, finish_reason=%s, "
                "content=%d chars",
                self.profile, self.model, dt, finish, len(content or ""),
            )
        else:
            details = getattr(usage, "completion_tokens_details", None)
            thinking = getattr(details, "reasoning_tokens", None) if details else None
            logger.info(
                "  ⏱ [%s] %s %.1fs | prompt=%s, completion=%s, thinking=%s, "
                "finish_reason=%s, content=%d chars",
                self.profile,
                self.model,
                dt,
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
                thinking,
                finish,
                len(content or ""),
            )
        logger.debug("  ↩ [%s] content: %s", self.profile, content)
        return resp

    def __repr__(self) -> str:
        return f"ModelSelector(profile={self.profile!r}, model={self.model!r})"

    def with_(self, **overrides: Any) -> "ModelSelector":
        """Return a copy of this selector with parameters overridden.

        The OpenAI client (``self.llm``) is shared with the original — no new
        client is created — so this is a cheap way to reuse one endpoint at
        different generation settings. Only attributes this instance actually
        holds may be overridden; ``llm`` is excluded because replacing it would
        mean a new client, which is the thing this method exists to avoid.

        Examples
        --------
        >>> hotter = models["cv"].with_(temperature=0.8)
        """
        unknown = set(overrides) - (set(vars(self)) - {"llm"})
        if unknown:
            raise TypeError(
                f"ModelSelector.with_ got unexpected override(s): {sorted(unknown)}"
            )
        new = copy.copy(self)
        for key, value in overrides.items():
            setattr(new, key, value)
        return new

    def with_web_search(self) -> "ModelSelector":
        """Return a clone with server-side web search enabled.

        Returns ``self`` unchanged when :attr:`supports_web_search` is
        ``False`` (the model did not declare ``web_search``). Merges
        :data:`WEB_SEARCH_EXTRA_BODY` into a copy of ``extra_body`` rather than
        replacing it, so provider-specific keys already set there (e.g.
        ``enable_thinking``) are preserved.
        """
        if not self.supports_web_search:
            return self
        extra = dict(self.extra_body or {})
        extra.update(WEB_SEARCH_EXTRA_BODY)
        return self.with_(extra_body=extra, profile=f"{self.profile}+search")


# ---------------------------------------------------------------------------
# models.toml -> ModelSelector.
# ---------------------------------------------------------------------------
def build_model(
    role: str,
    config: Optional[ModelConfig] = None,
    *,
    resources_dir: Optional[Path] = None,
    quiet: bool = False,
) -> ModelSelector:
    """Build the :class:`ModelSelector` for one role in ``models.toml``.

    Sampling and thinking settings come from the model's own table (after any
    env override); anything it leaves out is omitted from the request so the
    provider's default applies. Raises ``KeyError`` for an unknown role and
    ``RuntimeError`` when the provider's API key is not set in the
    environment. Callers that want a one-off variation clone the result with
    :meth:`ModelSelector.with_` instead of rebuilding it.
    """
    load_dotenv()
    if config is None:
        config = load_model_config(resources_dir)

    if role not in config.models:
        raise KeyError(f"unknown model role {role!r}; expected one of {list(MODEL_ROLES)}")
    if not config.provider.is_usable():
        raise RuntimeError(
            f"No model is usable: {config.provider.api_key_env} is not set. "
            "Set it in your .env."
        )

    spec = config.models[role]
    if not quiet:
        logger.info("  🧠 %s: %s", role, spec.summary_line())

    return ModelSelector(
        profile=role,
        api_key=config.provider.resolved_api_key() or "",
        base_url=config.provider.resolved_base_url() or "",
        model=spec.model,
        max_tokens=spec.max_tokens if spec.max_tokens is not None else config.max_tokens,
        temperature=spec.temperature,
        timeout=config.timeout,
        max_retries=1,
        extra_body=spec.thinking_extra_body(),
        reasoning_effort=spec.effective_reasoning_effort(),
        supports_web_search=spec.web_search,
        structured_output=spec.structured_output,
    )


def build_models(
    config: Optional[ModelConfig] = None,
    *,
    resources_dir: Optional[Path] = None,
    quiet: bool = False,
) -> Dict[str, ModelSelector]:
    """Build all three models, keyed by role (see :data:`MODEL_ROLES`).

    Raises ``RuntimeError`` when the provider's API key is not set — with one
    provider there is no fallback, so all three roles fail together.
    """
    load_dotenv()
    if config is None:
        config = load_model_config(resources_dir)
    return {
        role: build_model(role, config, quiet=quiet) for role in MODEL_ROLES
    }


__all__ = [
    "ENV_OVERRIDABLE_FIELDS",
    "MODELS_FILE",
    "MODEL_ROLES",
    "ModelConfig",
    "ModelSelector",
    "ModelSpec",
    "ProviderConfig",
    "build_model",
    "build_models",
    "describe_response_format",
    "load_model_config",
]
