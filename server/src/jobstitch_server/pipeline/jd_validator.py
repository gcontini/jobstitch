"""Reading a job description: detection, then structured analysis.

Two jobs, both the ``summary`` model's:

- :meth:`JDValidator.detect` — is this text a job posting at all? The free
  structural checks run first (see
  :func:`jobstitch_contracts.static_jd_guess`); the model is only asked when
  they pass, so a clipboard full of code costs nothing.
- :meth:`JDValidator.analyze` — score the posting against the candidate
  profile and extract the facts a CV and a cover letter need.

The profile and the preferences arrive with the call, never from disk: the
same validator instance serves every request.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from jobstitch_contracts import (
    MAX_JD_CHARS,
    MIN_JD_CHARS,
    JDAnalysis,
    JDDetection,
    static_jd_guess,
)
from pydantic import ValidationError

from ..bundle import CandidateInputs
from ..defaults import read_text_default
from ..model_selector import ModelSelector
from ..observability import LOGGER_ROOT, stage
from .errors import ModelOutputError
from .parsing import parse_model_json

logger = logging.getLogger(f"{LOGGER_ROOT}.jd")

#: Generate -> validate rounds. The validation error is fed back between them,
#: the same self-correction the CV pipeline uses.
MAX_ATTEMPTS = 2

#: The contract with the model for :meth:`JDValidator.analyze`, shipped as
#: ``resources/sys_prompt_analysis.txt`` alongside the CV/letter prompts.
JD_SYSTEM_PROMPT = read_text_default("sys_prompt_analysis.txt")

#: The detection prompt, shipped as ``resources/sys_prompt_jd_detect.txt``.
DETECT_PROMPT = read_text_default("sys_prompt_jd_detect.txt")


class JDValidator:
    """Analyzes job descriptions with one shared model.

    Parameters
    ----------
    model_selector:
        The ``summary`` model — reading a posting is extraction, not writing,
        so it does not need the large model.
    min_chars / max_chars:
        The length band :meth:`detect` accepts before it will ask the model.
    """

    def __init__(
        self,
        model_selector: ModelSelector,
        *,
        min_chars: int = MIN_JD_CHARS,
        max_chars: int = MAX_JD_CHARS,
    ) -> None:
        self.model_selector = model_selector
        self.min_chars = min_chars
        self.max_chars = max_chars

    # --- private helpers ---------------------------------------------------
    @staticmethod
    def _parse_jd_analysis(text: str) -> JDAnalysis:
        """Strip markdown code fences (if any) and validate the LLM JSON payload.

        ``unwrap_nested`` is on here and nowhere else: the flash models this
        role runs on are the ones that wrap the payload in a single string
        field.
        """
        return parse_model_json(text, JDAnalysis, unwrap_nested=True)

    @staticmethod
    def _clamp_list(items, limit: int) -> List[str]:
        """Coerce to strings and keep at most ``limit`` items."""
        if not items:
            return []
        return [str(i) for i in items][:limit]

    # --- public API ---------------------------------------------------------
    def detect(self, text: str) -> JDDetection:
        """Is ``text`` a job description? Static checks first, model second.

        The caller gets a yes or a no; why is in this request's log, which
        also records whether the model was asked at all (it is not, when the
        structural checks already say no — that is the point of having them).
        """
        if not static_jd_guess(text, min_chars=self.min_chars, max_chars=self.max_chars):
            logger.info("  ✗ not a job description (%d chars, no model call)", len(text))
            return JDDetection(is_job_description=False)

        with stage("jd.detect"):
            response = self.model_selector.completions_create(
                [{"role": "user", "content": DETECT_PROMPT + text}]
            )
        verdict = (response.choices[0].message.content or "").strip()
        logger.info("  🔎 job description check: %s", verdict)
        return JDDetection(is_job_description=verdict.upper().startswith("YES"))

    def analyze(self, job_description: str, candidate: CandidateInputs) -> JDAnalysis:
        """Compare a job description against the profile and extract key facts.

        Returns a validated :class:`JDAnalysis`. One corrective retry: the
        model's own bad output is fed back so the fix is targeted.
        """
        # Keep the full conversation across retries so the JD + profile context is
        # never lost (on failure we only APPEND a correction message).
        messages = [
            {"role": "system", "content": JD_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "JOB DESCRIPTION:\n"
                    f"{job_description}\n\n"
                    "--------------------------------------------\n"
                    "CANDIDATE PROFILE (candidate_profile.json):\n"
                    f"{json.dumps(dict(candidate.profile), indent=2)}\n"
                    "--------------------------------------------\n"
                    "PERSONAL PREFERENCES:\n"
                    f"{candidate.preferences}\n"
                ),
            },
        ]

        # Generate + validate, with one retry that feeds the validation error back.
        analysis = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            with stage("jd.analysis"):
                response = self.model_selector.completions_create(
                    messages,
                    response_format=self.model_selector.response_format(
                        "jd_analysis", JDAnalysis.model_json_schema()
                    ),
                )
            try:
                analysis = self._parse_jd_analysis(response.choices[0].message.content)
                break
            except (ValueError, ValidationError) as e:
                raw = response.choices[0].message.content or ""
                preview = raw if len(raw) <= 1500 else raw[:1500] + f"… [truncated {len(raw)} chars]"
                logger.error(
                    "  ✗ JD analysis parse failed (attempt %d): %s: %s",
                    attempt, type(e).__name__, e,
                )
                logger.error("    raw model response: %r", preview)
                # Feed the model's actual (bad) output back so the correction is
                # targeted, mirroring the CV loop in cv_generator.py.
                messages.append(
                    {"role": "assistant", "content": response.choices[0].message.content or ""}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON data against the required "
                            "structure.\n"
                            f"Error: {type(e).__name__}: {e}\n\n"
                            "Fix the response above: produce the ACTUAL analysis JSON object — use "
                            "the field list and example from the system message. Do NOT echo the "
                            "schema/template; output data only, with real values."
                        ),
                    }
                )
        if analysis is None:
            raise ModelOutputError(
                "Could not obtain valid JDAnalysis from the model.", stage="jd.analysis"
            )

        # Clamp skill lists to the requested 4 items.
        analysis.hard_skills = self._clamp_list(analysis.hard_skills, 4)
        analysis.soft_skills = self._clamp_list(analysis.soft_skills, 4)

        return analysis


__all__ = ["JDValidator", "MAX_ATTEMPTS"]
