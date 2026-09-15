"""Filesystem layout for jobstitch.

Two roots, both overridable from the environment (or ``.env``):

``JOBSTITCH_RESOURCES``
    Where the templates, system prompts, model config and your own CV data
    live. Defaults to ``resources/`` next to the checkout.

``JOBSTITCH_HOME``
    The workspace the watcher operates on — ``incoming/``, ``working/``,
    ``error/``, ``resume/`` and ``applications.xlsx``. Defaults to
    ``~/jobstitch``. Nothing here is ever written inside the repository, so a
    clone stays clean.

Personal files (``candidate_profile.json``, ``candidate_data.json``,
``pers_preferences.md``, ``candidate_signature.png``) are git-ignored. Each
ships a fictional ``*.example.*`` sibling, and :func:`resource_path` falls back
to it when you have not created your own copy yet — so a fresh clone runs
end-to-end before you put any real data in it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# src/jobstitch/paths.py -> src/jobstitch -> src -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_dir(var: str, default: Path) -> Path:
    """Read a directory path from the environment, else use ``default``."""
    raw = os.getenv(var)
    if not raw:
        return default
    return Path(raw).expanduser().resolve()


#: Templates, system prompts, ``models.toml`` and the candidate data files.
RESOURCES_DIR = _env_dir("JOBSTITCH_RESOURCES", _REPO_ROOT / "resources")

#: Workspace root the watcher polls and writes into.
JOBSTITCH_HOME = _env_dir("JOBSTITCH_HOME", Path.home() / "jobstitch")

INCOMING_DIR = JOBSTITCH_HOME / "incoming"
WORKING_DIR = JOBSTITCH_HOME / "working"
ERROR_DIR = JOBSTITCH_HOME / "error"
RESUME_DIR = JOBSTITCH_HOME / "resume"  # aka the OK dir; resume = French for CV
XLSX_PATH = JOBSTITCH_HOME / "applications.xlsx"

# Files that carry personal data: git-ignored, with a shipped example sibling.
PERSONAL_FILES = (
    "candidate_profile.json",
    "candidate_data.json",
    "pers_preferences.md",
    "candidate_signature.png",
)

_warned_examples: set[str] = set()


def example_name(name: str) -> str:
    """``candidate_data.json`` -> ``candidate_data.example.json``."""
    stem, dot, suffix = name.rpartition(".")
    return f"{stem}.example.{suffix}" if dot else f"{name}.example"


def strip_example(name: str) -> str:
    """Inverse of :func:`example_name`; returns ``name`` unchanged if not one."""
    return name.replace(".example.", ".", 1) if ".example." in name else name


def resource_path(name: str, resources_dir: Optional[Path] = None) -> Path:
    """Locate ``name`` in the resources folder, falling back to its example.

    Your own file always wins. When it is absent the shipped
    ``*.example.*`` sibling is used instead and a one-time warning is printed,
    so a fresh clone works while making it obvious the CV is being built from
    fictional data. Raises ``FileNotFoundError`` when neither exists.
    """
    base = Path(resources_dir) if resources_dir is not None else RESOURCES_DIR
    real = base / name
    if real.is_file():
        return real

    fallback = base / example_name(name)
    if fallback.is_file():
        if name not in _warned_examples:
            _warned_examples.add(name)
            print(
                f"  ⚠ {name} not found in {base} — using the fictional "
                f"{fallback.name}. Copy it to {name} and put your own data in "
                "it.",
                flush=True,
            )
        return fallback

    raise FileNotFoundError(
        f"Neither {real} nor {fallback} exists. Copy the example files in "
        f"{base} to their real names (see the README) or point "
        "JOBSTITCH_RESOURCES at a folder that has them."
    )


__all__ = [
    "RESOURCES_DIR",
    "JOBSTITCH_HOME",
    "INCOMING_DIR",
    "WORKING_DIR",
    "ERROR_DIR",
    "RESUME_DIR",
    "XLSX_PATH",
    "PERSONAL_FILES",
    "example_name",
    "strip_example",
    "resource_path",
]
