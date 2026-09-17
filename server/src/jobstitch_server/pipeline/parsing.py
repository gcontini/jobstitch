"""Getting a model's reply into a validated object.

Every generator needed the same three steps — drop the Markdown code fence a
model sometimes wraps its reply in, parse the JSON, validate it against a
pydantic model — and each one had its own copy. This is the one copy.
"""

from __future__ import annotations

import json
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


def strip_fences(text: str) -> str:
    """Drop a Markdown code fence around a reply, if the model added one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_model_json(
    content: Optional[str],
    model: Type[M],
    *,
    empty: str = "LLM returned empty content",
    unwrap_nested: bool = False,
) -> M:
    """Parse one reply into ``model``.

    Raises ``ValueError`` (including ``json.JSONDecodeError``) when the content
    is missing or is not JSON, and ``ValidationError`` when it does not
    conform. Both are what the callers feed back to the model as the next turn,
    so a weak ``response_format`` costs a retry rather than correctness.

    ``unwrap_nested`` is for the providers that wrap the real payload inside a
    single string field, e.g. ``{"description": "{\\"match_percentage\\": …}"}``.
    """
    if not content:
        raise ValueError(empty)

    data = json.loads(strip_fences(content))
    try:
        return model.model_validate(data)
    except ValidationError:
        if unwrap_nested and isinstance(data, dict) and len(data) == 1:
            wrapped = next(iter(data.values()))
            if isinstance(wrapped, str):
                return model.model_validate(json.loads(wrapped))
        raise


__all__ = ["strip_fences", "parse_model_json"]
