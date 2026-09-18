"""Finding your files without being told where they are.

The rule is: put your files in the current folder. Anything named below that
sits in the directory you are running from (or in ``--data-dir``) is picked up
by name. An explicit path in ``jobstitch.toml`` or on the command line always
wins, and what cannot be found locally falls back to the server's default.

Images are the one thing found by shape rather than by name: every ``.png``,
``.jpg`` and ``.jpeg`` in those folders is sent along under its own file name,
because that is the name the LaTeX template includes it under.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

#: The files jobstitch knows how to use, by the name it looks for.
KNOWN_FILES = (
    "candidate_profile.json",
    "candidate_data.json",
    "candidate_preferences.md",
    "resume.tex.jinja",
    "sys_prompt_cv.txt",
    "sys_prompt_highlight.txt",
    "sys_prompt_review.txt",
    "sys_prompt_letter.txt",
)

#: Images are picked up by extension, not by name.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")

CONFIG_NAME = "jobstitch.toml"


def search_dirs(extra: Optional[Path] = None) -> List[Path]:
    """Where to look, in order: an explicit folder, then the current one."""
    candidates = [extra, Path.cwd()]
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


def discover_images(extra: Optional[Path] = None) -> Dict[str, Path]:
    r"""Every image in the search folders, by file name, first hit wins.

    The name is the whole contract: the server writes each one next to the
    ``.tex`` under exactly this name, so ``\includegraphics{photo.png}`` in
    your template is satisfied by ``photo.png`` sitting next to you.
    """
    found: Dict[str, Path] = {}
    for directory in search_dirs(extra):
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in IMAGE_SUFFIXES or path.name in found:
                continue
            if path.is_file():
                found[path.name] = path
    return found


__all__ = ["KNOWN_FILES", "IMAGE_SUFFIXES", "CONFIG_NAME", "search_dirs", "find_config",
           "discover_files", "discover_images"]
