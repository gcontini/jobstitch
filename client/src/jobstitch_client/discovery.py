"""Finding your files without being told where they are.

A downloaded executable has no install layout, so the rule is: put your files
next to it. Anything named below that sits beside the binary (or beside the
config file, or in the directory you are running from) is picked up by name.
An explicit path in ``jobstitch.toml`` or on the command line always wins, and
what cannot be found locally falls back to the server's default.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

#: The files jobstitch knows how to use, by the name it looks for.
KNOWN_FILES = (
    "candidate_profile.json",
    "candidate_data.json",
    "pers_preferences.md",
    "candidate_signature.png",
    "resume.tex.jinja",
    "sys_prompt_cv.txt",
    "sys_prompt_highlight.txt",
    "sys_review_prompt.txt",
    "sys_prompt_letter.txt",
)

CONFIG_NAME = "jobstitch.toml"


def executable_dir() -> Path:
    """The folder the binary is in — a PyInstaller bundle included.

    ``sys.executable`` is the python interpreter when running from source and
    the bundled binary when frozen, which is exactly the distinction that
    matters here.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def search_dirs(extra: Optional[Path] = None) -> List[Path]:
    """Where to look, in order: an explicit folder, the binary's, the cwd."""
    candidates = [extra, executable_dir(), Path.cwd()]
    seen: List[Path] = []
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = Path(candidate).expanduser().resolve()
        if resolved.is_dir() and resolved not in seen:
            seen.append(resolved)
    return seen


def find_config(explicit: Optional[Path] = None) -> Optional[Path]:
    """Locate ``jobstitch.toml``: the flag, then ``$JOBSTITCH_CONFIG``, then the
    search folders."""
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        return path
    from_env = os.getenv("JOBSTITCH_CONFIG")
    if from_env:
        path = Path(from_env).expanduser()
        if path.is_file():
            return path
    for directory in search_dirs():
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def discover_files(extra: Optional[Path] = None) -> Dict[str, Path]:
    """Every known file found in the search folders, first hit wins."""
    found: Dict[str, Path] = {}
    for directory in search_dirs(extra):
        for name in KNOWN_FILES:
            if name not in found and (directory / name).is_file():
                found[name] = directory / name
    return found


__all__ = ["KNOWN_FILES", "CONFIG_NAME", "executable_dir", "search_dirs",
           "find_config", "discover_files"]
