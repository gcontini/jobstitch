"""Centralized LLM model selection.

jobstitch calls exactly three models, one per job, all declared in
``resources/models.toml``:

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

Nothing here knows about a specific provider. Each ``[models.*]`` table is
self-contained and provider quirks are declared as capability flags rather than
written as ``if name == ...`` branches. A model whose API key is not set
borrows the endpoint of one that is (see :func:`resolve_spec`), so a single
provider key runs the whole pipeline.
"""

from __future__ import annotations

import copy
import logging
import os
import time
import tomllib
from dataclasses import dataclass, field, replace
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

#: What each role is for, used in startup messages and error text.
ROLE_JOBS = {
    "summary": "JD analysis and the cover letter",
    "cv": "CV writing and the content review",
    "highlight": "keyword highlighting",
}

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

# What a model borrows from another when its own API key is not set: the
# endpoint, the model name and the provider's capabilities. Everything else
# (temperature, thinking, reasoning effort) stays the role's own.
_ENDPOINT_FIELDS = (
    "model",
    "api_key_env",
    "base_url_env",
    "base_url",
    "structured_output",
    "web_search",
)


# ---------------------------------------------------------------------------
# Declarative model table, loaded from resources/models.toml.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSpec:
    """One of the three models, as written in ``models.toml``.

    Provider differences are capabilities, not names: ``web_search``,
    ``thinking`` and ``structured_output`` are what :func:`build_model`
    branches on.
    """

    role: str
    model: str
    api_key_env: str
    base_url_env: Optional[str] = None
    base_url: Optional[str] = None
    temperature: Optional[float] = None
    reasoning_effort: Optional[str] = None
    thinking: str = "auto"                 # "auto" | "on" | "off"
    thinking_budget: Optional[int] = None  # reasoning-token cap, thinking = "on"
    web_search: bool = False
    # "json_schema_strict" | "json_schema" | "json_object" | "none"
    structured_output: str = "json_object"
    max_tokens: Optional[int] = None       # overrides [defaults].max_tokens

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
    """Parsed ``models.toml``: the three models plus the ``[defaults]`` block."""

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

    Raises ``ValueError`` when a role is missing or unknown, when a key is
    misspelled or carries a value nothing acts on, or when a model omits
    ``model`` / ``api_key_env`` — a typo in the config surfaces at startup
    instead of halfway through a job.
    """
    path = _models_path(resources_dir)
    raw = tomllib.loads(path.read_text(encoding="utf-8"))

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
        body = declared[role]
        unknown = set(body) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"{path}: [models.{role}] has unknown key(s) {sorted(unknown)}; "
                f"allowed: {sorted(_ALLOWED_KEYS)}"
            )
        for required in ("model", "api_key_env"):
            if not body.get(required):
                raise ValueError(
                    f"{path}: [models.{role}] is missing '{required}'"
                )
        spec = ModelSpec(role=role, **body)
        _validate(spec, path)
        models[role] = spec

    return ModelConfig(models=models, defaults=raw.get("defaults") or {})


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

    def completions_create(
        self,
        messages: List[Dict[str, Any]],
        response_format: Optional[Dict[str, Any]] = None,
    ):
        """Run ``self.llm.chat.completions.create`` with only the parameters
        specified in the constructor (plus the required ``model``/``messages``).

        Optional generation parameters left ``None`` at construction are
        omitted so the provider's defaults apply. ``response_format`` is
        forwarded only when provided. Prints wall time and token usage after
        each call.
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

        t0 = time.perf_counter()
        resp = self.llm.chat.completions.create(**kwargs)
        dt = time.perf_counter() - t0

        # One line per call, with what it cost. This is the whole of token
        # accounting: the log is the ledger, so there is no second copy to
        # keep in step with it.
        usage = getattr(resp, "usage", None)
        if usage is None:
            logger.info("  \u23f1 [%s] %.1fs | usage not reported", self.profile, dt)
        else:
            details = getattr(usage, "completion_tokens_details", None)
            thinking = getattr(details, "reasoning_tokens", None) if details else None
            logger.info(
                "  \u23f1 [%s] %s %.1fs | prompt=%s, completion=%s%s",
                self.profile,
                self.model,
                dt,
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
                f", thinking={thinking}" if thinking else "",
            )
        return resp

    def __repr__(self) -> str:
        return f"ModelSelector(profile={self.profile!r}, model={self.model!r})"

    def with_(self, **overrides: Any) -> "ModelSelector":
        """Return a copy of this selector with parameters overridden.

        The OpenAI client (``self.llm``) is shared with the original — no new
        client is created — so this is a cheap way to reuse one endpoint at
        different generation settings. Only attributes known to
        :class:`ModelSelector` may be overridden.

        Examples
        --------
        >>> hotter = models["cv"].with_(temperature=0.8)
        """
        known = {
            "profile",
            "model",
            "max_tokens",
            "temperature",
            "frequency_penalty",
            "presence_penalty",
            "extra_body",
            "reasoning_effort",
            "supports_web_search",
            "structured_output",
        }
        unknown = set(overrides) - known
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
def missing_keys_hint(config: ModelConfig) -> str:
    """Comma-separated list of the API-key variables the three models name."""
    return ", ".join(sorted({spec.api_key_env for spec in config.models.values()}))


def resolve_spec(role: str, config: ModelConfig) -> ModelSpec:
    """The spec to build ``role`` from, borrowing an endpoint when needed.

    Returns the role's own :class:`ModelSpec` when its API key and endpoint
    are set. Otherwise the endpoint, model name and provider capabilities of
    the first usable model are borrowed (and reported), while the role keeps
    its own temperature, thinking and reasoning settings — so one provider key
    is enough to run everything, at the size that provider was configured
    with. Raises ``RuntimeError`` when no model at all is usable.
    """
    if role not in config.models:
        raise KeyError(f"unknown model role {role!r}; expected one of {list(MODEL_ROLES)}")

    spec = config.models[role]
    if spec.is_usable():
        return spec

    donor = next(
        (other for other in config.models.values() if other.is_usable()), None
    )
    if donor is None:
        raise RuntimeError(
            f"No model is usable for {ROLE_JOBS.get(role, role)}: none of the "
            f"API keys in resources/{MODELS_FILE} are set. Set one of "
            f"{missing_keys_hint(config)} in your .env."
        )

    missing = (
        spec.api_key_env
        if not spec.resolved_api_key()
        else (spec.base_url_env or "base_url")
    )
    logger.info(
        "  ℹ %s: %s not set — borrowing the %s endpoint (%s).",
        role, missing, donor.role, donor.model,
    )
    return replace(spec, **{f: getattr(donor, f) for f in _ENDPOINT_FIELDS})


def build_model(
    role: str,
    config: Optional[ModelConfig] = None,
    *,
    resources_dir: Optional[Path] = None,
    quiet: bool = False,
) -> ModelSelector:
    """Build the :class:`ModelSelector` for one role in ``models.toml``.

    Sampling and thinking settings come from the model's own table; anything it
    leaves out is omitted from the request so the provider's default applies.
    Callers that want a one-off variation clone the result with
    :meth:`ModelSelector.with_` instead of rebuilding it.
    """
    load_dotenv()
    if config is None:
        config = load_model_config(resources_dir)

    spec = resolve_spec(role, config)
    if not quiet:
        logger.info("  🧠 %s: %s", role, spec.summary_line())

    return ModelSelector(
        profile=role,
        api_key=spec.resolved_api_key() or "",
        base_url=spec.resolved_base_url() or "",
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

    Raises ``RuntimeError`` when not one of the declared API keys is set;
    individual models missing a key borrow a configured endpoint rather than
    dropping out, so the returned dict always has all three roles.
    """
    load_dotenv()
    if config is None:
        config = load_model_config(resources_dir)
    return {
        role: build_model(role, config, quiet=quiet) for role in MODEL_ROLES
    }


__all__ = [
    "MODELS_FILE",
    "MODEL_ROLES",
    "ROLE_JOBS",
    "ModelConfig",
    "ModelSelector",
    "ModelSpec",
    "build_model",
    "build_models",
    "load_model_config",
    "missing_keys_hint",
    "resolve_spec",
]
