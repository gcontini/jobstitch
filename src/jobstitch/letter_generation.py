"""Cover letter generation module — :class:`LetterGenerator`.

:class:`LetterGenerator` — LLM-driven pipeline that produces a plain-text cover
letter for a job: builds the prompt from the JD, the JD analysis and the master
profile, optionally enables server-side web research on the employer (see
:meth:`jobstitch.model_selector.ModelSelector.with_web_search`), generates
and lightly validates the letter text, and writes it into ``job_dir``.

Much simpler than :class:`jobstitch.cv_creation.CVGenerator`: no schema, no
renderer, no highlighter, no content-review pass — just prose in, prose out.
"""

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cv_creation import _load_json_file, _load_text_file
from .model_selector import ModelSelector
from .paths import resource_path

# ---------------------------------------------------------------------------
# Output contract.
# ---------------------------------------------------------------------------
LETTER_FILENAME = "cover_letter.txt"
MIN_WORDS = 180
MAX_WORDS = 450


# ---------------------------------------------------------------------------
# LetterGenerator class — LLM-driven cover letter pipeline.
# ---------------------------------------------------------------------------
class LetterGenerator:
    """Generates a plain-text cover letter for a given job description.

    Wraps a much simpler pipeline than :class:`~jobstitch.cv_creation.
    CVGenerator`: builds the LLM prompt from the job description, the JD
    analysis and the master profile, optionally turns on server-side web
    search on the employer (only for a direct posting from a real company,
    never a headhunter/agency, and only when the endpoint supports it), and
    validates the output is plausible prose (word count, no leftover
    placeholders, ASCII) — retrying with the validation error fed back to the
    LLM on failure.

    Parameters
    ----------
    job_dir:
        The isolated per-job folder the letter runs fully inside. Fixed at
        construction time, same convention as :class:`CVGenerator`.
    summary_model:
        The ``summary`` model from ``models.toml`` — mid-size, light thinking,
        and the one role that may run web search — bundling the endpoint, model
        name and generation parameters for the letter content. Created once by
        the caller and shared across jobs (no per-instance client construction
        here). The same model reads job descriptions for
        :class:`~jobstitch.jd_validator.JDValidator`.
    resources_dir:
        Directory containing the system prompt file and the master
        ``candidate_profile.json`` (the repo's ``resources/`` folder).
    system_prompt_file:
        Name of the system prompt file inside ``resources_dir``.
    max_attempts:
        Max generate -> validate attempts (validation errors fed back to the
        LLM between attempts).
    """

    def __init__(
        self,
        job_dir: Path,
        summary_model: ModelSelector,
        *,
        resources_dir: Path,
        system_prompt_file: str = "sys_prompt_letter.txt",
        max_attempts: int = 2,
    ) -> None:
        # One job = one isolated folder: job_dir is fixed at construction time,
        # same convention as CVGenerator.
        self.job_dir = job_dir
        resources_dir = Path(resources_dir)
        self.summary_model = summary_model
        self.max_attempts = max_attempts

        # Load the master profile (candidate data used to write the letter).
        self._master_profile = _load_json_file(
            str(resource_path("candidate_profile.json", resources_dir))
        )

        # Load the system prompt enforcing structure/tone/evidence rules.
        sys_prompt_path = resource_path(system_prompt_file, resources_dir)
        self._system_message = {
            "role": "system",
            "content": _load_text_file(str(sys_prompt_path)),
        }

    def _read_job_inputs(self) -> tuple[str, Dict[str, Any]]:
        """Read ``JD.txt`` (required, non-empty) and ``analysis.json`` from
        ``job_dir``. A missing/corrupt ``analysis.json`` degrades to ``{}``
        rather than failing the whole letter — the JD text alone is still
        enough to write a letter.
        """
        jd_file = self.job_dir / "JD.txt"
        if not jd_file.is_file():
            raise FileNotFoundError(f"JD.txt missing in {self.job_dir}")
        job_description = jd_file.read_text(encoding="utf-8")
        if not job_description.strip():
            raise ValueError(f"JD.txt is empty in {self.job_dir}")

        analysis: Dict[str, Any] = {}
        analysis_file = self.job_dir / "analysis.json"
        if analysis_file.is_file():
            try:
                analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(
                    f"  ⚠ analysis.json unparsable ({e}) — writing the letter "
                    "without it",
                    flush=True,
                )

        return job_description, analysis

    def _should_research(self, analysis: Dict[str, Any]) -> bool:
        """Web research is worth doing only for a direct posting from a named,
        real employer, and only when the endpoint can actually run it. A
        headhunter/agency posting has no employer to research — searching it
        wastes tokens and risks the letter describing the agency instead of
        the company actually hiring.
        """
        if not self.summary_model.supports_web_search:
            return False
        if analysis.get("posting_type") != "direct":
            return False
        return bool(analysis.get("company_name"))

    def _build_user_message(
        self, job_description: str, analysis: Dict[str, Any], research: bool
    ) -> str:
        """Assemble the single user turn: today's date, the master profile,
        a trimmed JD-analysis subset (only the fields relevant to a letter —
        the full JDAnalysis carries scoring fields with no place in one),
        the raw JD, and — only when ``research`` is true — an explicit
        instruction to research the named company on the web.
        """
        analysis_subset = {
            k: analysis.get(k)
            for k in (
                "company_name",
                "job_title",
                "work_location",
                "hard_skills",
                "soft_skills",
                "gaps",
                "posting_url",
            )
            if analysis.get(k) is not None
        }

        content = (
            "Write my cover letter for this JOB DESCRIPTION. "
            "Output plain text only, starting at the header block.\n"
            "--------------------------------------------\n"
            f"TODAY'S DATE: {date.today().isoformat()}\n"
            "--------------------------------------------\n"
            "MASTER PROFILE:\n"
            f"{json.dumps(self._master_profile, indent=2)}\n"
            "--------------------------------------------\n"
            "JD ANALYSIS:\n"
            f"{json.dumps(analysis_subset, indent=2)}\n"
            "--------------------------------------------\n"
            "JOB DESCRIPTION:\n"
            f"{job_description}\n"
        )
        if research:
            content += (
                "--------------------------------------------\n"
                "COMPANY RESEARCH: search the web for "
                f"\"{analysis_subset.get('company_name')}\" (the employer "
                "posting this job) and use what you find for the "
                "why-this-company paragraph, per the system prompt rules.\n"
            )
        return content

    @staticmethod
    def _extract_letter(response) -> str:
        """Parse the LLM response content into the letter text.

        Strips Markdown code fences if present (the LLM is asked for plain
        text but occasionally wraps it anyway). Raises ``ValueError`` when the
        content is missing.
        """
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM returned empty content")

        text = content.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        return text

    @staticmethod
    def _validate_letter(text: str) -> None:
        """Light sanity checks on the generated letter. Raises ``ValueError``
        with an actionable message on the first violation found so it can be
        fed back to the LLM for a corrective retry.
        """
        word_count = len(text.split())
        if not (MIN_WORDS <= word_count <= MAX_WORDS):
            raise ValueError(
                f"Letter is {word_count} words; must be between {MIN_WORDS} "
                f"and {MAX_WORDS} words."
            )
        for open_tok, close_tok in (("[", "]"), ("<<", ">>")):
            if open_tok in text and close_tok in text:
                raise ValueError(
                    f"Letter contains an unfilled placeholder "
                    f"(found '{open_tok}...{close_tok}'). Remove it — rewrite "
                    "the sentence instead of leaving a gap to fill in."
                )
        try:
            text.encode("ascii")
        except UnicodeEncodeError as e:
            raise ValueError(
                f"Letter contains non-ASCII characters: {e}. Use standard "
                "ASCII only (straight quotes, '--' for a dash)."
            ) from e

    def generate_letter(self) -> str:
        """Generate the cover letter and write it into ``job_dir``.

        Reads ``JD.txt``/``analysis.json``, decides whether web research on
        the employer is worthwhile (see :meth:`_should_research`), and runs a
        validation retry loop (up to ``max_attempts``, validation errors fed
        back to the LLM as a correction turn — same self-correction pattern as
        :class:`CVGenerator`). If the first call fails while web search is
        enabled (a provider rejecting ``enable_search`` surfaces as an API
        error, not a ``ValueError``), retries once without search before
        giving up. Returns the absolute path to the written ``.txt`` file.
        """
        job_description, analysis = self._read_job_inputs()
        research = self._should_research(analysis)

        selector = self.summary_model
        if research:
            selector = selector.with_web_search()

        print(
            f"\n--- cover letter (web research: {'on' if research else 'off'}) ---",
            flush=True,
        )

        messages: List[Dict[str, Any]] = [
            self._system_message,
            {
                "role": "user",
                "content": self._build_user_message(job_description, analysis, research),
            },
        ]

        search_fallback_tried = not research
        for attempt in range(self.max_attempts):
            try:
                resp = selector.completions_create(messages)
            except Exception as e:
                if not search_fallback_tried:
                    print(
                        f"  ⚠ web search call failed ({type(e).__name__}: {e}) — "
                        "retrying without it",
                        flush=True,
                    )
                    search_fallback_tried = True
                    selector = self.summary_model
                    continue
                raise

            messages.append(
                {"role": "assistant", "content": resp.choices[0].message.content}
            )

            try:
                text = self._extract_letter(resp)
                self._validate_letter(text)
                print(f"  ✓ Letter validated (attempt {attempt + 1})", flush=True)
                letter_path = self.job_dir / LETTER_FILENAME
                letter_path.write_text(text, encoding="utf-8")
                print(f"  ✓ Cover letter: {letter_path}", flush=True)
                return str(letter_path)
            except ValueError as e:
                print(
                    f"  ✗ Validation failed (attempt {attempt + 1}): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response is not acceptable as-is.\n"
                            f"Issue: {type(e).__name__}: {e}\n\n"
                            "Fix it and output only the corrected letter text."
                        ),
                    }
                )

        raise RuntimeError(
            f"Could not produce a valid cover letter after {self.max_attempts} attempts."
        )


__all__ = ["LetterGenerator", "LETTER_FILENAME"]
