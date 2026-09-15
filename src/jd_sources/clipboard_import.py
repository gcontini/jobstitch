"""Clipboard job-description intake — one JD source.

Watches the clipboard for something that looks like a job description, runs it
through :class:`~jobstitch.jd_validator.JDValidator`, prints the analysis, and
— on confirmation — publishes a job folder into ``incoming/`` for
:mod:`jobstitch.job_watcher` to pick up.

It lives outside the ``jobstitch`` package (see :mod:`jd_sources`) because the
published folder, not an import, is the contract between jobstitch and
whatever supplies job descriptions to it::

    incoming/<Company>_<JobTitle>/
        JD.txt          the raw job description text — the whole contract
        analysis.json   a serialized JDAnalysis (optional; this source writes
                        one, the watcher never reads it)

Anything that writes a non-empty ``JD.txt`` is a valid JD source, so this
module is one implementation rather than the only way in (see "Bring your own
JD source" in the README). The folder name here is for your own eyes: nothing
downstream parses it.

Launch with (console script installed by ``uv sync``, or ``uv run``)::

    clipboard-import
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Optional

import pyperclip

from jobstitch.jd_validator import JDAnalysis, JDValidator
from jobstitch.model_selector import ModelSelector, build_model
from jobstitch.paths import INCOMING_DIR

# A clipboard shorter than this is never a job posting; skip the LLM call.
MIN_JD_CHARS = 500
CLIPBOARD_POLL_SECONDS = 2.0


# ---------------------------------------------------------------------------
# 1. Clipboard preflight — is there a clipboard here at all?
# ---------------------------------------------------------------------------
# pyperclip.paste() returns "" both for an empty clipboard and for a helper
# that never reached the display (it discards xclip's stderr), so the loop
# below cannot tell "nothing copied yet" from "there is no clipboard here":
# it waits forever for a change that can never arrive. Ask the helper itself,
# once, at startup. These are how the helpers say the display is unreachable;
# an *empty* clipboard fails differently ("target STRING not available",
# "No selection") and is not a problem.
UNREACHABLE_DISPLAY = ("open display", "wayland")

NO_CLIPBOARD_HINT = """  In a container over SSH with X11 forwarding, DISPLAY (localhost:10.0 and
  the like) is a TCP port on the *host's* loopback and the cookie is on the
  host too, so the container needs both, in .env:
      JOBSTITCH_NETWORK=host
      JOBSTITCH_XAUTHORITY=$HOME/.Xauthority   (an absolute path, no ~)
  On a machine with no clipboard, run the watcher alone with
  JOBSTITCH_CLIPBOARD=off and feed it job folders from your own JD source."""


def clipboard_error() -> Optional[str]:
    """Return why the clipboard cannot be read, or None when it can be.

    The helper and the order are pyperclip's own (``determine_clipboard``),
    so what is probed here is what ``pyperclip.paste()`` actually runs.
    """
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        probe = ["wl-paste", "--no-newline"]
    elif os.environ.get("DISPLAY") and shutil.which("xclip"):
        probe = ["xclip", "-selection", "c", "-o"]
    elif os.environ.get("DISPLAY") and shutil.which("xsel"):
        probe = ["xsel", "-b", "-o"]
    else:
        return (
            "no clipboard here: neither DISPLAY nor WAYLAND_DISPLAY names a "
            "display with a helper installed (xclip, xsel, wl-clipboard)"
        )

    try:
        done = subprocess.run(probe, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{probe[0]} could not be run: {exc}"

    stderr = (done.stderr or "").strip()
    if done.returncode != 0 and any(
        phrase in stderr.lower() for phrase in UNREACHABLE_DISPLAY
    ):
        return f"{probe[0]} cannot reach the display: {stderr}"
    return None


# ---------------------------------------------------------------------------
# 2. Clipboard watching + JD detection.
# ---------------------------------------------------------------------------
def evaluate_clipboard(text: str, model: ModelSelector) -> bool:
    """Return True if ``text`` looks like a job description, else False."""
    if len(text) < MIN_JD_CHARS:
        return False

    response = model.completions_create(
        [
            {
                "role": "user",
                "content": (
                    "You are a job description validator. The text below was "
                    "copied to a clipboard. Reply with exactly 'YES' if it is "
                    "a job description (a job posting mentioning role, "
                    "company, responsibilities and/or requirements), or "
                    "exactly 'NO' if it is not.\n\nTEXT:\n" + text
                ),
            }
        ]
    )
    verdict = (response.choices[0].message.content or "").strip()
    print(f"🔎 clipboard check: {verdict}", flush=True)
    return verdict.upper().startswith("YES")


def wait_for_clipboard_jd(model: ModelSelector, seen: str = "") -> str:
    """Block until the clipboard holds a valid job description to process.

    When ``seen`` is non-empty (the JD just processed) it first waits for the
    clipboard to change away from it, so the same JD is not submitted twice.
    Returns the captured JD text.
    """
    while True:
        cur_clip = pyperclip.paste().strip()
        print(f"📋 Current clipboard: {len(cur_clip)} chars")

        if cur_clip == seen:
            print("✗ Clipboard unchanged — copy a new JD...", flush=True)
        elif evaluate_clipboard(cur_clip, model):
            print(f"✓ Job Description captured ({len(cur_clip)} chars)")
            return cur_clip
        else:
            print("✗ Not a valid job description — waiting for clipboard to "
                  "change...", flush=True)

        # Wait for the clipboard to change, then loop and re-check.
        prev_clip = cur_clip
        while True:
            time.sleep(CLIPBOARD_POLL_SECONDS)
            new_clip = pyperclip.paste().strip()
            if new_clip and new_clip != prev_clip:
                break


# ---------------------------------------------------------------------------
# 3. JD analysis — match score + structured extraction.
# ---------------------------------------------------------------------------
def analyze_and_print(job_description: str, validator: JDValidator) -> JDAnalysis:
    """Analyze ``job_description`` and print the result."""
    analysis = validator.analyze_jd(job_description)

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
    print(f"Personal pref     : {analysis.pers_preference_score} — "
          f"{analysis.pers_preferences}")
    print("=" * 56)
    return analysis


# ---------------------------------------------------------------------------
# 4. Confirm the source URL + publish the job folder to incoming/.
# ---------------------------------------------------------------------------
def prompt_submission() -> tuple[str, Optional[str]]:
    """Ask the user how to proceed with the detected JD.

    Returns ``(action, url)``:
      - ("submit", link)  -> submit with the pasted posting URL
      - ("submit", None)  -> submit without a link (answer 'y')
      - ("skip",  None)   -> skip this JD (answer 's')
      - ("quit",  None)   -> exit the intake loop (answer 'q')
    """
    print("Paste the posting URL to submit, y = submit without link, "
          "s = skip, q = quit", flush=True)
    while True:
        ans = input("> ").strip()
        if not ans:
            continue                    # empty answer -> re-prompt
        low = ans.lower()
        if low in ("q", "quit"):
            return "quit", None
        if low in ("s", "skip"):
            return "skip", None
        if low in ("y", "yes"):
            return "submit", None
        return "submit", ans            # anything else is treated as the URL


def sanitize_name(text: str, max_len: int = 40) -> str:
    """Turn a company/job title into a filesystem-safe folder fragment."""
    clean = re.sub(r"\W+", "_", text.strip()).strip("_")
    return clean[:max_len].strip("_") or "unknown"


def publish_job(
    jd_analysis: JDAnalysis,
    job_description: str,
    posting_url: Optional[str],
    incoming_dir=INCOMING_DIR,
) -> None:
    """Publish the analyzed JD into ``incoming/`` for the watcher.

    Any existing folder of the same name is overwritten.
    """
    jd_analysis.posting_url = posting_url or None

    # No date prefix: the watcher organizes resume/ CVs under a daily folder,
    # so incoming/working/error folder names are just Company_JobTitle.
    folder_name = (
        f"{sanitize_name(jd_analysis.company_name)}_"
        f"{sanitize_name(jd_analysis.job_title)}"
    )

    # Write into a hidden temp folder, then rename: the watcher only ever sees
    # complete job folders.
    incoming_dir.mkdir(parents=True, exist_ok=True)
    final_dir = incoming_dir / folder_name
    tmp_dir = incoming_dir / f".tmp_{final_dir.name}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=False)
    (tmp_dir / "JD.txt").write_text(job_description, encoding="utf-8")
    (tmp_dir / "analysis.json").write_text(
        jd_analysis.model_dump_json(indent=2), encoding="utf-8"
    )
    if final_dir.exists():
        shutil.rmtree(final_dir, ignore_errors=True)
    tmp_dir.rename(final_dir)

    print(f"📥 Published job folder for the watcher: {final_dir}")


# ---------------------------------------------------------------------------
# 5. Main intake loop — detect, analyze, ask, submit/skip, repeat.
# ---------------------------------------------------------------------------
def run_intake_loop() -> None:
    """Run until the user answers 'q'.

    After every submit or skip it goes back to waiting for the next valid JD
    on the clipboard.
    """
    problem = clipboard_error()
    if problem:
        print(f"✗ {problem}", flush=True)
        print(NO_CLIPBOARD_HINT, flush=True)
        raise SystemExit(1)

    print(f"📥 Job intake folder: {INCOMING_DIR}")
    # Both steps here — the "is this a job description?" check and the full
    # analysis — are the `summary` model's job, so build it once and share it.
    model = build_model("summary")
    validator = JDValidator(model_selector=model)

    seen = ""
    while True:
        job_description = wait_for_clipboard_jd(model, seen)
        analysis = analyze_and_print(job_description, validator)
        action, url = prompt_submission()
        if action == "quit":
            print("👋 Exiting intake loop.")
            break
        if action == "skip":
            print("⏭ Skipped — waiting for the next JD...", flush=True)
            seen = job_description
            continue
        publish_job(analysis, job_description, url)
        seen = job_description
        print("⏳ Submitted — waiting for the next JD...", flush=True)


if __name__ == "__main__":
    run_intake_loop()
