"""Writing the cover letter: prose in, prose out.

Much simpler than the CV pipeline — no schema, no renderer, no highlighter,
no content review. The one piece of machinery is the optional web research:
when the posting is a direct one from a named employer and the endpoint can
search, the model is told to look the company up, and a provider that rejects
the flag falls back to writing without it.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Dict, List, Mapping, Optional

from ..model_selector import ModelSelector
from ..observability import LOGGER_ROOT, stage
from .errors import ModelOutputError
from .parsing import strip_fences

logger = logging.getLogger(f"{LOGGER_ROOT}.letter")

#: The letter must read as a letter: bounds catch a stub and a runaway alike.
MIN_WORDS = 180
MAX_WORDS = 450

#: Generate -> validate rounds; the validation error is fed back between them,
#: the same self-correction the CV pipeline uses.
MAX_ATTEMPTS = 2


class LetterGenerator:
    """Generates a plain-text cover letter for one job description.

    Parameters
    ----------
    summary_model:
        The ``summary`` model — the only role allowed to run web search.
    system_prompt:
        The letter prompt for this run (default, or the request's override).
    """

    def __init__(
        self,
        summary_model: ModelSelector,
        *,
        system_prompt: str,
    ) -> None:
        self.summary_model = summary_model
        self._system_message = {"role": "system", "content": system_prompt}

    def _should_research(self, analysis: Mapping[str, Any]) -> bool:
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
        self,
        job_description: str,
        profile: Mapping[str, Any],
        analysis: Mapping[str, Any],
        research: bool,
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
            f"{json.dumps(dict(profile), indent=2)}\n"
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
        return strip_fences(content)

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

    def generate(
        self,
        job_description: str,
        *,
        profile: Mapping[str, Any],
        analysis: Optional[Mapping[str, Any]] = None,
    ) -> str:
        """Write the letter and return its text.

        ``analysis`` is optional everywhere in jobstitch: the JD alone is
        enough to write a letter, it just makes for a less targeted one.
        """
        analysis = dict(analysis or {})
        research = self._should_research(analysis)

        selector = self.summary_model
        if research:
            selector = selector.with_web_search()

        logger.info("--- cover letter (web research: %s) ---", "on" if research else "off")

        messages: List[Dict[str, Any]] = [
            self._system_message,
            {
                "role": "user",
                "content": self._build_user_message(job_description, profile, analysis, research),
            },
        ]

        search_fallback_tried = not research
        for attempt in range(MAX_ATTEMPTS):
            try:
                with stage("letter.generate"):
                    resp = selector.completions_create(messages)
            except Exception as e:
                if not search_fallback_tried:
                    logger.warning(
                        "  ⚠ web search call failed (%s: %s) — retrying without it",
                        type(e).__name__, e,
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
                logger.info("  ✓ Letter validated (attempt %d)", attempt + 1)
                return text
            except ValueError as e:
                logger.error(
                    "  ✗ Validation failed (attempt %d): %s: %s",
                    attempt + 1, type(e).__name__, e,
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

        raise ModelOutputError(
            f"Could not produce a valid cover letter after {MAX_ATTEMPTS} attempts.",
            stage="letter.generate",
        )


__all__ = ["LetterGenerator", "MIN_WORDS", "MAX_WORDS"]
