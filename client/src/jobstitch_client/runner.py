"""One job, start to finish — the only place the order of steps is written.

    detect -> analyze -> confirm -> write CV -> letter -> deliver

Every collaborator is injected: the API, the folder layout, the question
asked before spending anything, the spreadsheet. That is what lets the four
modes share this and what lets the tests run it with no server and no
terminal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from jobstitch_contracts import JDAnalysis, static_jd_guess

from .api import JobstitchApi, JobstitchError, read_text
from .config import LETTER_PROMPT, Config
from .cvjob import write_cv
from .joblog import JobLog
from .sources import JDCandidate
from .tracking import TEMPLATE_NAME, Tracker
from .ui import Confirmer
from .workspace import (
    ANALYSIS_FILENAME,
    LETTER_FILENAME,
    LOG_FILENAME,
    Artifacts,
    Workspace,
)


@dataclass(frozen=True)
class Outcome:
    """What became of one posting."""

    status: str  # delivered | discarded | rejected | failed | quit
    message: str
    path: Optional[Path] = None

    @property
    def ok(self) -> bool:
        return self.status == "delivered"


class JobRunner:
    """Runs one posting through the pipeline and files the result."""

    def __init__(
        self,
        *,
        api: JobstitchApi,
        workspace: Workspace,
        config: Config,
        confirmer: Confirmer,
        tracker: Tracker,
        resume: Optional[str] = None,
    ) -> None:
        self.api = api
        self.workspace = workspace
        self.config = config
        self.confirmer = confirmer
        self.tracker = tracker
        #: --resume: pick an already-started CV job back up instead of
        #: submitting a new one. Only meaningful for the one job this run
        #: processes. Named apart from the resume() method below, which is
        #: the unrelated "pick up a leftover working/ folder" recovery.
        self.resume_id = resume

    # --- entry points -------------------------------------------------------
    def handle(self, candidate: JDCandidate) -> Outcome:
        """A new posting: detect, analyze, ask, produce."""
        log = JobLog()
        try:
            detection = self._detect(candidate, log)
            if not detection:
                return self._reject(candidate, log)

            analysis, job_dir = self._analyze(candidate, log)
            decision = self.confirmer.confirm(analysis)
            if decision.quit:
                log.step("⏹ stopped before submitting")
                self._finish_log(job_dir, log)
                return Outcome("quit", "stopped by the user", self.workspace.discard(job_dir))
            if not decision.submit:
                log.step("⏭ skipped")
                self._finish_log(job_dir, log)
                return Outcome("discarded", "skipped", self.workspace.discard(job_dir))

            if decision.url:
                analysis.posting_url = decision.url
                self._write_analysis(job_dir, analysis)
            return self._produce(job_dir, candidate.text, analysis, log)
        except JobstitchError as exc:
            # Nothing has a job folder yet, so the claimed file itself is what
            # gets filed under error/ — never left behind in working/.
            return self._failed(log, exc, candidate=candidate)

    def resume(self, job_dir: Path) -> Outcome:
        """A folder left in ``working/``: it was analyzed, so pick up there."""
        log = JobLog()
        log.step(f"♻ resuming {job_dir.name}")
        try:
            analysis = JDAnalysis.model_validate_json(
                (job_dir / ANALYSIS_FILENAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            log.step(f"✗ cannot read {ANALYSIS_FILENAME}: {exc}")
            self._finish_log(job_dir, log)
            return Outcome("failed", str(exc), self.workspace.to_error(job_dir))

        jd_text = self._stored_jd(job_dir)
        decision = self.confirmer.confirm(analysis)
        if not decision.submit:
            self._finish_log(job_dir, log)
            return Outcome("discarded", "skipped", self.workspace.discard(job_dir))
        try:
            return self._produce(job_dir, jd_text, analysis, log)
        except JobstitchError as exc:
            return self._failed(log, exc, job_dir=job_dir)

    # --- the one place a call is made ---------------------------------------
    def _call(self, log: JobLog, call):
        """Make one API call and, with ``--debug``, keep what the server said.

        Every step goes through here, so "fetch the log too" is one decision
        in one place rather than five.
        """
        envelope = call()
        if self.config.debug:
            self._fetch_logs(log, envelope.request_id)
        return envelope.data

    def _fetch_logs(self, log: JobLog, request_id: Optional[str]) -> None:
        """Pull one request's server-side log into this job's ``log.log``."""
        if not request_id:
            return
        try:
            log.server(request_id, self.api.logs(request_id).data.entries)
        except JobstitchError as exc:
            log.step(f"⚠ could not fetch the server log for {request_id}: {exc}")

    # --- steps --------------------------------------------------------------
    def _detect(self, candidate: JDCandidate, log: JobLog) -> bool:
        """The free check first, the server's second."""
        if not static_jd_guess(candidate.text):
            log.step(f"✗ not a job description ({len(candidate.text)} chars, nothing sent)")
            return False
        detection = self._call(log, lambda: self.api.detect(candidate.text))
        if not detection.is_job_description:
            log.step("✗ not a job description")
            return False
        log.step(f"✓ job description ({len(candidate.text)} chars)")
        return True

    def _analyze(self, candidate: JDCandidate, log: JobLog) -> tuple[JDAnalysis, Path]:
        log.step("📊 analyzing the posting...")
        analysis = self._call(log, lambda: self.api.analyze(
            candidate.text,
            profile=self.config.require("candidate_profile.json").read_bytes(),
            preferences=self.config.require("candidate_preferences.md").read_text(encoding="utf-8"),
            temperature=self.config.temperature,
        ))

        job_dir = self.workspace.open_job(analysis.company_name, analysis.job_title)
        target = job_dir / candidate.filename
        if candidate.origin is not None and candidate.origin.exists():
            candidate.origin.rename(target)  # keeps the name you gave it
        else:
            target.write_text(candidate.text, encoding="utf-8")
        self._write_analysis(job_dir, analysis)
        log.step(f"📥 {job_dir.name}")
        return analysis, job_dir

    def _produce(
        self, job_dir: Path, jd_text: str, analysis: JDAnalysis, log: JobLog
    ) -> Outcome:
        """Everything that costs money, in order."""
        try:
            if self.config.cover_letter != "letter_only":
                self._cv(job_dir, jd_text, log)
            if self.config.cover_letter in ("yes", "letter_only"):
                self._letter(job_dir, jd_text, analysis, log)
        except JobstitchError as exc:
            return self._failed(log, exc, job_dir=job_dir)

        self._finish_log(job_dir, log)
        delivered = self.workspace.deliver(job_dir)
        try:
            self.tracker.record(delivered, analysis)
        except Exception as exc:
            # The CV is delivered and was paid for; a broken or locked
            # spreadsheet is bookkeeping, and must not turn that into a
            # failed job or kill the loop that is watching the folder.
            log.step(f"⚠ could not record this job in {TEMPLATE_NAME}: {exc}")
            self._finish_log(delivered, log)  # the warning belongs in log.log too
        return Outcome("delivered", "done", delivered)

    def _cv(self, job_dir: Path, jd_text: str, log: JobLog) -> None:
        """Write the CV and save all three files — or re-render the one here.

        The server sends the document, the LaTeX and the PDF back together, so
        there is one call and one moment where the folder is complete.

        A folder that already holds its document is re-rendered instead, and
        that costs LaTeX only. That is the whole recovery story: edit the
        JSON, drop the folder back, pay nothing to a model.
        """
        artifacts = self._artifacts()
        stored = job_dir / artifacts.document
        if stored.is_file():
            log.step(f"♻ reusing {stored.name} (no model call)")
            rendered = self._call(log, lambda: self.api.render(
                document=json.loads(stored.read_text(encoding="utf-8")),
                template=read_text(self.config.path("resume.tex.jinja")),
                images=self.config.image_parts(),
            ))
        else:
            log.step("✍ writing the CV (this takes minutes)...")
            request_id, rendered = write_cv(
                self.api, self.config, jd_text, say=log.step, resume=self.resume_id
            )
            if self.config.debug:
                self._fetch_logs(log, request_id)
            stored.write_text(
                json.dumps(rendered.document, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        (job_dir / artifacts.tex).write_text(rendered.tex, encoding="utf-8")
        (job_dir / artifacts.pdf).write_bytes(rendered.pdf_bytes())
        log.step(f"✅ {artifacts.pdf}")

    def _letter(
        self, job_dir: Path, jd_text: str, analysis: JDAnalysis, log: JobLog
    ) -> None:
        log.step("✉ writing the cover letter...")
        letter = self._call(log, lambda: self.api.letter(
            jd_text,
            profile=self.config.require("candidate_profile.json").read_bytes(),
            analysis=analysis.model_dump_json(),
            prompt=read_text(self.config.path(LETTER_PROMPT)),
            temperature=self.config.temperature,
        ))
        (job_dir / LETTER_FILENAME).write_text(letter.text, encoding="utf-8")
        log.step(f"✅ {LETTER_FILENAME} ({letter.words} words)")

    # --- endings ------------------------------------------------------------
    def _reject(self, candidate: JDCandidate, log: JobLog) -> Outcome:
        return Outcome(
            "rejected", "not a job description", self._park(candidate, log)
        )

    def _failed(
        self,
        log: JobLog,
        exc: JobstitchError,
        *,
        job_dir: Optional[Path] = None,
        candidate: Optional[JDCandidate] = None,
    ) -> Outcome:
        log.step(f"✗ {exc}")
        if exc.request_id:
            log.step(f"   request id: {exc.request_id}")
        # A failure is always worth the extra round trip: this is the one
        # moment the server's account of the run is what you need.
        self._fetch_logs(log, exc.request_id)
        if job_dir is not None:
            self._finish_log(job_dir, log)
            return Outcome("failed", str(exc), self.workspace.to_error(job_dir))
        return Outcome("failed", str(exc), self._park(candidate, log))

    def _park(self, candidate: Optional[JDCandidate], log: JobLog) -> Optional[Path]:
        """File a claimed input under error/, with its log beside it.

        Returns ``None`` for a source with no file behind it (the clipboard):
        there is nothing to move and nothing to keep.
        """
        if candidate is None or candidate.origin is None or not candidate.origin.exists():
            return None
        moved = self.workspace.to_error(candidate.origin)
        log.write(moved.with_suffix(moved.suffix + ".log"))
        return moved

    # --- helpers ------------------------------------------------------------
    def _candidate_data(self) -> Dict[str, Any]:
        """Your own candidate_data.json — read here only to name the files."""
        return json.loads(
            self.config.require("candidate_data.json").read_text(encoding="utf-8")
        )

    def _artifacts(self) -> Artifacts:
        """File names come from your own candidate data, not from the server."""
        return Artifacts(str(self._candidate_data().get("name") or "candidate"))

    def _write_analysis(self, job_dir: Path, analysis: JDAnalysis) -> None:
        (job_dir / ANALYSIS_FILENAME).write_text(
            analysis.model_dump_json(indent=2), encoding="utf-8"
        )

    def _stored_jd(self, job_dir: Path) -> str:
        """The JD text a resumed folder was built from, whatever it is called."""
        from .workspace import JD_FILENAME

        candidates = [job_dir / JD_FILENAME, *sorted(job_dir.glob("*.txt"))]
        for path in candidates:
            if path.is_file() and path.name not in (LETTER_FILENAME, LOG_FILENAME):
                return path.read_text(encoding="utf-8")
        raise JobstitchError(f"no job description file left in {job_dir.name}")

    def _finish_log(self, job_dir: Path, log: JobLog) -> None:
        log.write(job_dir / LOG_FILENAME)


__all__ = ["JobRunner", "Outcome"]
