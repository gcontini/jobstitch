"""The two halves of a request's inputs, kept apart on purpose.

:class:`ResourceBundle` is impersonal — prompts, the LaTeX template, the
images the template includes. The server ships a default one and a request may
override any part of it.

:class:`CandidateInputs` is personal — the profile the model reads, the
preferences the JD analysis scores against, and the candidate data the
template prints. It only ever comes from the request body, is never cached and
is never written to disk. That is the whole reason this file has two classes
instead of one: the thing worth caching and the thing that must not be. Only
``profile`` and ``preferences`` are ever shown to a model; ``data`` goes to
the renderer, which cannot count pages without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .defaults import (
    SIGNATURE_FILE,
    TEMPLATE_FILE,
    read_bytes_default,
    read_text_default,
)

#: What counts as an image when a bundle is built from a folder.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")


@dataclass(frozen=True)
class ResourceBundle:
    """Prompts, template and assets for one request."""

    sys_prompt_cv: str
    sys_prompt_highlight: str
    sys_prompt_review: str
    sys_prompt_letter: str
    template_source: str
    template_name: str = TEMPLATE_FILE
    assets: Mapping[str, bytes] = field(default_factory=dict)

    def with_overrides(self, **parts: Any) -> "ResourceBundle":
        """A copy with the given parts replaced. ``None`` keeps the default.

        Handed the optional multipart parts directly, so a request that sends
        nothing gets the shipped defaults untouched. Images are merged over the
        defaults by name rather than replacing them, so sending one image does
        not take the others away.
        """
        images = parts.pop("images", None)
        changes = {k: v for k, v in parts.items() if v is not None}
        if images:
            changes["assets"] = {**self.assets, **images}
        return replace(self, **changes) if changes else self


@dataclass(frozen=True)
class CandidateInputs:
    """Everything personal in one request. Never cached, never persisted."""

    #: Everything you have done: what the model tailors the CV from.
    profile: Mapping[str, Any]
    #: What you want from a job, scored by the JD analysis.
    preferences: str = ""
    #: candidate_data.json — printed by the template, read by no model.
    data: Mapping[str, Any] = field(default_factory=dict)


@lru_cache(maxsize=1)
def default_bundle() -> ResourceBundle:
    """The shipped defaults, read once per process.

    Cached deliberately: these files cannot change under a running server
    without a restart, and re-reading them per request would be four opens on
    the hot path.
    """
    return ResourceBundle(
        sys_prompt_cv=read_text_default("sys_prompt_cv.txt"),
        sys_prompt_highlight=read_text_default("sys_prompt_highlight.txt"),
        sys_prompt_review=read_text_default("sys_prompt_review.txt"),
        sys_prompt_letter=read_text_default("sys_prompt_letter.txt"),
        template_source=read_text_default(TEMPLATE_FILE),
        assets={SIGNATURE_FILE: read_bytes_default(SIGNATURE_FILE)},
    )


def bundle_from_dir(directory: Path) -> ResourceBundle:
    """Build a bundle from a folder of files — for local runs and tests."""
    directory = Path(directory)

    def text(name: str) -> str:
        path = directory / name
        return path.read_text(encoding="utf-8") if path.is_file() else read_text_default(name)

    # The blank signature first, so a folder with no image of its own still
    # renders the stock template; anything in the folder wins by name.
    assets = {SIGNATURE_FILE: read_bytes_default(SIGNATURE_FILE)}
    assets.update({
        path.name: path.read_bytes()
        for path in sorted(directory.iterdir())
        if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file()
    })
    return ResourceBundle(
        sys_prompt_cv=text("sys_prompt_cv.txt"),
        sys_prompt_highlight=text("sys_prompt_highlight.txt"),
        sys_prompt_review=text("sys_prompt_review.txt"),
        sys_prompt_letter=text("sys_prompt_letter.txt"),
        template_source=text(TEMPLATE_FILE),
        assets=assets,
    )


__all__ = [
    "ResourceBundle",
    "CandidateInputs",
    "default_bundle",
    "bundle_from_dir",
    "IMAGE_SUFFIXES",
]
