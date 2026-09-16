"""Where the server's impersonal defaults come from.

Two sources, in order: the directory named by ``$JOBSTITCH_RESOURCES`` (so a
deployment can mount edited prompts or a different template without a
rebuild), then the copy shipped inside the package. A file present in neither
is a packaging bug, so it raises.

Nothing personal is ever read from here — a profile, candidate data,
preferences or a signature only ever arrive in a request.
"""

from __future__ import annotations

import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Optional

#: Files the server ships and will look for.
PROMPT_FILES = (
    "sys_prompt_cv.txt",
    "sys_prompt_highlight.txt",
    "sys_review_prompt.txt",
    "sys_prompt_letter.txt",
)
TEMPLATE_FILE = "resume3.tex.jinja"
MODELS_FILE = "models.toml"
PLACEHOLDER_SIGNATURE = "placeholder_signature.png"


@lru_cache(maxsize=1)
def packaged_dir() -> Path:
    """The ``resources/`` folder inside the installed package."""
    return Path(str(resources.files("jobstitch_server.resources")))


def override_dir() -> Optional[Path]:
    """``$JOBSTITCH_RESOURCES``, when set to an existing directory."""
    raw = os.getenv("JOBSTITCH_RESOURCES")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def default_path(name: str) -> Path:
    """Locate one default file: the override directory wins over the package."""
    override = override_dir()
    if override is not None and (override / name).is_file():
        return override / name
    packaged = packaged_dir() / name
    if packaged.is_file():
        return packaged
    raise FileNotFoundError(
        f"{name} is missing from the server resources ({packaged_dir()}"
        + (f" and {override}" if override else "")
        + ")"
    )


def read_text_default(name: str) -> str:
    return default_path(name).read_text(encoding="utf-8")


def read_bytes_default(name: str) -> bytes:
    return default_path(name).read_bytes()


__all__ = [
    "PROMPT_FILES",
    "TEMPLATE_FILE",
    "MODELS_FILE",
    "PLACEHOLDER_SIGNATURE",
    "packaged_dir",
    "override_dir",
    "default_path",
    "read_text_default",
    "read_bytes_default",
]
