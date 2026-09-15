"""Folder-based CV job watcher.

A single coordinator thread watches ``incoming/`` for job folders produced by
a JD source such as ``jd_sources.clipboard_import``. The whole contract is a
non-empty ``JD.txt`` in the folder — the only file this watcher reads, and the
only one it requires. Anything else a source leaves in there is carried along
untouched.
Valid folders are moved to ``working/`` and handed to a pool of daemon worker
threads; every job runs ``CVGenerator.generate_cv`` fully isolated in its own
directory. Outcomes are handled back in the coordinator thread:

- success -> the folder is cleaned (only the LaTeX leftovers go; whatever the
  JD source put there travels with it) and moved under ``resume/<today>/``
  (daily subfolder), then :class:`~jobstitch.workbook.ApplicationsWorkbook`
  appends a row to ``applications.xlsx`` — off with ``--no-xlsx`` or
  ``JOBSTITCH_XLSX=off``, and a failure there costs a log line, not the CV.
- failure -> a ``job.log`` with the traceback is added and the folder is
  moved to ``error/`` (invalid incoming folders are also moved to ``error/``).

Resubmit a job by moving its folder from ``error/`` back to ``incoming/``.
Partial recovery: an existing ``.tex`` is recompiled without an LLM call
(handled inside ``CVGenerator.generate_cv``); an existing ``cv_data.json`` is
re-rendered without an LLM call; otherwise the full LLM pipeline runs.

Rules:
- All folder moves happen ONLY in this coordinator thread.
- Nothing is inferred from a job folder's name. It is carried from
  ``incoming/`` through to ``resume/<today>/`` unchanged and only ever
  printed, so a source may name a folder whatever it likes;
  ``Company_JobTitle`` is a convention for your own convenience, not a format
  this watcher parses. The date lives in the ``resume/<today>/`` daily
  subfolder, never in a name prefix.
- Ctrl+C exits immediately (workers are daemon threads); in-flight folders
  stay in ``working/`` and are re-queued to ``incoming/`` at the next start.
- Dotfiles/lockfiles/stray files and leftover ``.tmp_*`` folders in
  ``incoming/`` are deleted ONLY at startup. During the watch loop, ``.tmp_*``
  folders are always left alone (a JD source may still be writing them).

The three models the pipeline calls (``cv``, ``summary``, ``highlight``) come
from ``resources/models.toml``; the CLI knobs here only tune sampling for CV
writing.

Launch with (console script installed by ``uv sync``, or ``uv run``):
    job-watcher --temperature=0.4 --frequency-penalty=0.1 --system-prompt-file my_prompt.txt
"""

from __future__ import annotations

import queue
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from .cv_creation import CVGenerator
from .letter_generation import LetterGenerator
from .model_selector import ModelConfig, ModelSelector, build_models, load_model_config
from .paths import (
    ERROR_DIR,
    INCOMING_DIR,
    JOBSTITCH_HOME,
    RESOURCES_DIR,
    RESUME_DIR,
    WORKING_DIR,
)
from .workbook import ApplicationsWorkbook, tracking_enabled

# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------
# The watched folders come from jobstitch.paths (JOBSTITCH_HOME), and every
# model name comes from resources/models.toml — nothing about a provider or a
# machine is hardcoded here.
MAX_WORKERS = 10            # max jobs in flight (queued + running)
POLL_INTERVAL = 2.0         # seconds between intake scans

SYSTEM_PROMPT_FILE = "sys_prompt_cv.txt"
LETTER_PROMPT_FILE = "sys_prompt_letter.txt"

# Auxiliary files the LaTeX run and the watcher itself leave behind; they are
# the only thing a successful job folder loses. Everything else is kept — the
# deliverables and, whatever they are, the files the JD source delivered.
DROP_SUFFIXES = {".aux", ".log", ".out", ".fls", ".fdb_latexmk", ".toc"}


# ---------------------------------------------------------------------------
# Shared configuration + outcome types.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JobConfig:
    """Shared, read-only configuration handed to every worker.

    The three model roles from ``resources/models.toml``, already built and
    shared across jobs: ``cv_model`` writes and reviews the CV,
    ``highlight_model`` adds the Markdown keyword markers, ``summary_model``
    writes the cover letter.
    """
    resources_dir: Path
    cv_model: ModelSelector
    highlight_model: Optional[ModelSelector] = None
    summary_model: Optional[ModelSelector] = None
    system_prompt_file: Optional[str] = None
    letter_prompt_file: str = LETTER_PROMPT_FILE


@dataclass
class JobOutcome:
    """Result of one worker job, handled by the coordinator thread."""
    folder_name: str
    ok: bool
    pdf_path: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Folder helpers (all moves happen in the coordinator thread).
# ---------------------------------------------------------------------------
def append_log(folder: Path, text: str) -> None:
    """Append a timestamped line to job.log inside the job folder."""
    try:
        with (folder / "job.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        print(f"⚠ could not write job.log in {folder}", flush=True)


def move_overwrite(src: Path, dst_dir: Path) -> Path:
    """Move src into dst_dir, overwriting any existing entry (no renaming)."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    delete_entry(dst)
    shutil.move(str(src), str(dst))
    return dst


def delete_entry(entry: Path) -> None:
    """Delete a file or directory tree, ignoring errors."""
    try:
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink()
    except OSError:
        pass


def valid_job_folder(folder: Path) -> tuple[bool, str]:
    """A job folder is valid when it has a non-empty JD.txt.

    That is the whole contract: the JD text is the only thing the pipeline
    needs. A JD source is free to drop anything else alongside it — an
    ``analysis.json`` being the usual extra — and the watcher neither requires
    nor reads it.
    """
    jd_file = folder / "JD.txt"
    if not jd_file.is_file() or not jd_file.read_text(encoding="utf-8").strip():
        return False, "JD.txt missing or empty"
    return True, ""


def clean_success_folder(folder: Path) -> None:
    """Drop the run's auxiliary leftovers; keep everything else.

    Subfolders (the pipeline's scratch dirs) and the aux/log files a LaTeX run
    leaves behind go; the CV, the letter, and every file the JD source put in
    the folder travel on to ``resume/`` untouched.
    """
    for item in folder.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        elif item.suffix.lower() in DROP_SUFFIXES:
            try:
                item.unlink()
            except OSError:
                pass


def requeue_orphans() -> None:
    """Move folders left in working/ (from a crashed run) back to incoming/."""
    for entry in sorted(WORKING_DIR.iterdir()):
        print(f"♻ re-queuing orphan from working/: {entry.name}", flush=True)
        move_overwrite(entry, INCOMING_DIR)


def cleanup_stray_incoming() -> None:
    """Delete stray files, lockfiles and leftover temp folders in incoming/.

    Runs only at watcher startup. Any leftover ``.tmp_*`` folder here is from a
    crashed run of a JD source and is safe to delete.
    """
    for entry in sorted(INCOMING_DIR.iterdir()):
        if entry.is_file() or entry.name.startswith("."):
            delete_entry(entry)
            print(f"🧹 deleted stray entry in incoming/: {entry.name}", flush=True)


# ---------------------------------------------------------------------------
# CV generation (runs in worker threads, fully isolated per job directory).
# ---------------------------------------------------------------------------
def generate_with_recovery(job_dir: Path, cfg: JobConfig) -> str:
    """Run the CV pipeline for one job folder (recovery handled inside).

    :class:`CVGenerator` reuses an existing ``.tex`` or ``cv_data.json`` in
    ``job_dir`` instead of calling the LLM again, so a resubmitted job costs
    only what it has to.
    """
    return CVGenerator(
        job_dir,
        cv_model=cfg.cv_model,
        highlight_model=cfg.highlight_model,
        resources_dir=cfg.resources_dir,
        system_prompt_file=cfg.system_prompt_file,
    ).generate_cv()


def generate_letter_safely(job_dir: Path, cfg: JobConfig) -> None:
    """Generate the cover letter; never raises.

    A letter failure must not sink an already-generated CV, so this degrades
    to a warning (same non-fatal-degradation policy as the highlight/review
    stages in CVGenerator) rather than sending the job to error/.
    """
    if cfg.summary_model is None:
        return
    try:
        LetterGenerator(
            job_dir,
            cfg.summary_model,
            resources_dir=cfg.resources_dir,
            system_prompt_file=cfg.letter_prompt_file,
        ).generate_letter()
    except Exception as exc:
        print(
            f"  ⚠ cover letter skipped ({job_dir.name}): "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def run_job(job_dir: Path, cfg: JobConfig) -> JobOutcome:
    """Worker entry point: never raises — errors become an Outcome."""
    try:
        pdf_path = generate_with_recovery(job_dir, cfg)
    except Exception:
        return JobOutcome(job_dir.name, ok=False, error=traceback.format_exc())
    generate_letter_safely(job_dir, cfg)
    return JobOutcome(job_dir.name, ok=True, pdf_path=pdf_path)


def worker_loop(job_queue: "queue.Queue", results_queue: "queue.Queue") -> None:
    """Consume jobs until the process exits (daemon thread)."""
    while True:
        job_dir, cfg = job_queue.get()
        results_queue.put(run_job(job_dir, cfg))


# ---------------------------------------------------------------------------
# Outcome handling + intake (coordinator thread).
# ---------------------------------------------------------------------------
def handle_outcome(
    outcome: JobOutcome, workbook: Optional[ApplicationsWorkbook] = None
) -> None:
    """Coordinator-side handling: success -> resume/ (+ xlsx), failure -> error/."""
    src = WORKING_DIR / outcome.folder_name
    if not src.exists():
        print(f"⚠ job folder vanished from working/: {outcome.folder_name}", flush=True)
        return

    if outcome.ok and outcome.pdf_path:
        dst: Optional[Path] = None
        try:
            clean_success_folder(src)
            dst_dir = RESUME_DIR / date.today().strftime("%y-%m-%d")
            dst = move_overwrite(src, dst_dir)
            pdf_in_resume = dst / Path(outcome.pdf_path).name
            if not pdf_in_resume.exists():
                raise FileNotFoundError(f"expected PDF not found: {pdf_in_resume}")
        except Exception:
            target = dst if dst is not None else src
            append_log(target, f"post-processing failed:\n{traceback.format_exc()}")
            move_overwrite(target, ERROR_DIR)
            print(f"⚠ {outcome.folder_name} -> error/ (post-processing failed)", flush=True)
            return

        print(f"✅ {outcome.folder_name} -> resume/{dst.parent.name}/{dst.name}",
              flush=True)
        # The CV is delivered; the spreadsheet is bookkeeping on top of it, so
        # a failure here is reported and nothing more (same non-fatal policy as
        # the cover letter).
        if workbook is not None:
            try:
                workbook.record(dst)
            except Exception as exc:
                print(
                    f"  ⚠ applications.xlsx not updated ({dst.name}): "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
        return

    append_log(src, f"CV generation failed:\n{outcome.error}")
    move_overwrite(src, ERROR_DIR)
    print(f"❌ {outcome.folder_name} -> error/", flush=True)


def try_intake_one(cfg: JobConfig, job_queue: "queue.Queue") -> bool:
    """Scan incoming/ once and submit at most one job. Returns True if a job
    was submitted."""
    for entry in sorted(INCOMING_DIR.iterdir()):
        # Delete stray files and lockfiles on sight (job folders only).
        if entry.is_file():
            delete_entry(entry)
            print(f"🧹 deleted stray entry in incoming/: {entry.name}", flush=True)
            continue

        # .tmp_* folder: a JD source is still writing it — always
        # leave alone (temp cleanup happens only at startup, never during the
        # loop).
        if entry.name.startswith("."):
            continue

        ok, reason = valid_job_folder(entry)
        if not ok:
            append_log(entry, f"invalid job folder: {reason}")
            move_overwrite(entry, ERROR_DIR)
            print(f"⚠ {entry.name} -> error/ ({reason})", flush=True)
            continue

        working_dir = move_overwrite(entry, WORKING_DIR)
        job_queue.put((working_dir, cfg))
        print(f"🚀 submitted {entry.name} to working/ + worker queue", flush=True)
        return True
    return False


# ---------------------------------------------------------------------------
# Coordinator main loop.
# ---------------------------------------------------------------------------
def build_job_config(
    *,
    temperature: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    system_prompt_file: str = SYSTEM_PROMPT_FILE,
    config: Optional[ModelConfig] = None,
) -> JobConfig:
    """Build the three models declared in ``models.toml`` into a :class:`JobConfig`.

    The sampling knobs apply to the ``cv`` model only — it writes the prose
    they exist to tune, while the ``summary`` and ``highlight`` models keep the
    settings their own tables declare. A model whose API key is not set borrows
    a configured endpoint (see :func:`jobstitch.model_selector.resolve_spec`),
    so either all three roles resolve or nothing is configured at all and
    :func:`build_models` raises.
    """
    if config is None:
        config = load_model_config()

    models = build_models(config)

    overrides = {
        key: value
        for key, value in (
            ("temperature", temperature),
            ("frequency_penalty", frequency_penalty),
            ("presence_penalty", presence_penalty),
        )
        if value is not None
    }
    cv_model = models["cv"].with_(**overrides) if overrides else models["cv"]

    return JobConfig(
        resources_dir=RESOURCES_DIR,
        cv_model=cv_model,
        highlight_model=models["highlight"],
        summary_model=models["summary"],
        system_prompt_file=system_prompt_file,
    )


def main(
    *,
    temperature: Optional[float] = None,
    frequency_penalty: Optional[float] = None,
    presence_penalty: Optional[float] = None,
    system_prompt_file: str = SYSTEM_PROMPT_FILE,
    track_applications: bool = True,
) -> None:
    for d in (INCOMING_DIR, WORKING_DIR, ERROR_DIR, RESUME_DIR):
        d.mkdir(parents=True, exist_ok=True)

    workbook = ApplicationsWorkbook() if track_applications else None

    cleanup_stray_incoming()
    requeue_orphans()

    cfg = build_job_config(
        temperature=temperature,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
        system_prompt_file=system_prompt_file,
    )

    job_queue: queue.Queue = queue.Queue()
    results_queue: queue.Queue = queue.Queue()

    workers = [
        threading.Thread(
            target=worker_loop,
            args=(job_queue, results_queue),
            name=f"cv-worker-{i}",
            daemon=True,
        )
        for i in range(MAX_WORKERS)
    ]
    for worker in workers:
        worker.start()

    print(f"👀 Workspace: {JOBSTITCH_HOME}  (set JOBSTITCH_HOME to move it)",
          flush=True)
    print(f"👀 Watching {INCOMING_DIR} (max {MAX_WORKERS} jobs in flight). "
          "Ctrl+C to stop.", flush=True)
    if workbook is None:
        print("📊 Application tracking off — no applications.xlsx row is written.",
              flush=True)

    in_flight = 0
    try:
        while True:
            # 1. Harvest finished jobs (each one frees a slot).
            while True:
                try:
                    outcome = results_queue.get_nowait()
                except queue.Empty:
                    break
                in_flight -= 1
                handle_outcome(outcome, workbook)

            # 2. Intake only when a slot is free (pool full => stop polling).
            if in_flight < MAX_WORKERS:
                if try_intake_one(cfg, job_queue):
                    in_flight += 1

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n⏹ Watcher stopped (Ctrl+C). In-flight jobs were left in "
              "working/ and will be re-queued at the next start.", flush=True)

def cli() -> None:
    """Console-script entry point (``job-watcher``, see pyproject.toml)."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Watch incoming/ for job folders and generate tailored CVs.",
        epilog="Which models run is declared in resources/models.toml "
               "(cv, summary, highlight); the options here tune sampling for "
               "CV writing.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        help="Sampling temperature for the CV model.",
    )
    parser.add_argument(
        "--frequency-penalty",
        type=float,
        help="Penalize tokens by how often they already appeared (CV model).",
    )
    parser.add_argument(
        "--presence-penalty",
        type=float,
        help="Penalize tokens that already appeared at least once (CV model).",
    )
    parser.add_argument(
        "--system-prompt-file",
        default=SYSTEM_PROMPT_FILE,
        help=f"Name of the system prompt file in the resources folder "
             f"(default: {SYSTEM_PROMPT_FILE}).",
    )
    parser.add_argument(
        "--no-xlsx",
        action="store_true",
        help="Do not track generated CVs in applications.xlsx "
             "(JOBSTITCH_XLSX=off does the same).",
    )
    args = parser.parse_args()

    main(
        temperature=args.temperature,
        frequency_penalty=args.frequency_penalty,
        presence_penalty=args.presence_penalty,
        system_prompt_file=args.system_prompt_file,
        track_applications=tracking_enabled() and not args.no_xlsx,
    )


if __name__ == "__main__":
    cli()
