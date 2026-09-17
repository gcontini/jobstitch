"""Everything the person sees and answers.

Kept behind :class:`Confirmer` so the decision to spend tokens has one
implementation for a terminal and one for ``--yes``, and so the runner can be
tested without stdin.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional, Protocol

from jobstitch_contracts import JDAnalysis


@dataclass(frozen=True)
class Decision:
    """What to do with an analyzed posting."""

    submit: bool
    url: Optional[str] = None
    quit: bool = False


class Confirmer(Protocol):
    """Asked once per posting, before anything is spent on it."""

    def confirm(self, analysis: JDAnalysis) -> Decision: ...


def print_analysis(analysis: JDAnalysis) -> None:
    """The analysis table — the thing you actually read before deciding."""
    print("\n" + "=" * 56)
    print("📊 JD ANALYSIS")
    print("=" * 56)
    print(f"Job title         : {analysis.job_title}")
    print(f"Match %           : {analysis.match_percentage}%  "
          f"({analysis.match_rationale or 'no rationale'})")
    print(f"Work location     : {analysis.work_location or 'not specified'}")
    print(f"Work mode         : {analysis.work_mode}")
    print(f"Expected salary   : {analysis.expected_salary or 'not specified'}")
    print(f"Max salary        : "
          f"{analysis.max_salary if analysis.max_salary not in (None, -1) else 'not found'}")
    print(f"Experience level  : {analysis.experience_level}")
    print(f"Hard skills       : {', '.join(analysis.hard_skills) or 'n/a'}")
    print(f"Soft skills       : {', '.join(analysis.soft_skills) or 'n/a'}")
    print(f"Posting type      : {analysis.posting_type}")
    print(f"Company           : {analysis.company_name}")
    print(f"Posting URL       : {analysis.posting_url or 'not detected'}")
    print(f"Gaps              : {analysis.gaps}")
    print(f"Personal pref     : {analysis.pers_preference_score} — {analysis.pers_preferences}")
    print("=" * 56)


class PromptConfirmer:
    """Asks on the terminal, and takes the posting URL while it is there."""

    def confirm(self, analysis: JDAnalysis) -> Decision:
        print_analysis(analysis)
        print("Paste the posting URL to submit, y = submit without link, "
              "s = skip, q = quit", flush=True)
        while True:
            try:
                answer = input("> ").strip()
            except EOFError:
                return Decision(submit=False, quit=True)
            if not answer:
                continue
            low = answer.lower()
            if low in ("q", "quit"):
                return Decision(submit=False, quit=True)
            if low in ("s", "skip"):
                return Decision(submit=False)
            if low in ("y", "yes"):
                return Decision(submit=True)
            return Decision(submit=True, url=answer)


class AutoConfirmer:
    """Says yes to everything — ``--yes``, for an unattended run."""

    def confirm(self, analysis: JDAnalysis) -> Decision:
        print_analysis(analysis)
        print("→ submitting automatically (--yes)", flush=True)
        return Decision(submit=True, url=analysis.posting_url)


def ask_recovery(files: int, folders: int) -> str:
    """``working/`` is not empty at startup: clean it or resume it."""
    print(f"\n⚠ working/ still holds {files} file(s) and {folders} job folder(s) "
          "from a previous run.", flush=True)
    print("  r = resume them, c = clean them out, q = quit", flush=True)
    while True:
        try:
            answer = (input("> ").strip() or "").lower()
        except EOFError:
            return "quit"
        if answer in ("r", "resume"):
            return "resume"
        if answer in ("c", "clean"):
            return "clean"
        if answer in ("q", "quit"):
            return "quit"


def fail(message: str, code: int = 1) -> None:
    """Report a fatal problem the way a CLI should: stderr, non-zero exit."""
    print(f"✗ {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def warn(message: str) -> None:
    """A non-fatal heads-up: stderr, execution continues."""
    print(f"⚠ {message}", file=sys.stderr, flush=True)


__all__ = ["Decision", "Confirmer", "PromptConfirmer", "AutoConfirmer",
           "print_analysis", "ask_recovery", "fail", "warn"]
