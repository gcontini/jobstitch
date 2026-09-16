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
from ..model_selector import ModelSelector
from ..observability import LOGGER_ROOT, stage
from .errors import ModelOutputError

logger = logging.getLogger(f"{LOGGER_ROOT}.jd")

#: Hoisted out of :meth:`JDValidator.analyze` unchanged: it is the contract
#: with the model, and it reads better as a constant than as an f-string
#: buried in a call.
JD_SYSTEM_PROMPT = (
    "You are a job-matching analyst. Given a JOB DESCRIPTION a CANDIDATE "
    "PROFILE and PERSONAL_PREFERENCE, compute how well the candidate fits "
    "the role and extract the requested structured facts. "
    "Scoring guidance: weigh hard-skill overlap, domain/industry relevance, "
    "seniority, and language/location fit. Output an integer 0-100 for "
    "match_percentage plus a one-line match_rationale. "
    "Rules:\n"
    "- work_mode must be one of: full_remote, hybrid, on_site, not_specified.\n"
    "- expected_salary: the salary as stated in the JD, else null.\n"
    "- max_salary: if the JD states a salary range, the UPPER bound as an "
    "integer (e.g. 'EUR 60k-80k' -> 80000); if a single number is stated, "
    "that number; if no salary is stated, -1.\n"
    "- experience_level: guess the seniority required by the JD, one of: "
    "entry_level, intermediate, professional, manager, director.\n"
    "- hard_skills: at most 4 hard/technical skills required by the JD.\n"
    "- soft_skills: at most 4 soft skills required by the JD.\n"
    "- posting_type: 'direct' if the post is from the hiring company itself, "
    "'headhunter' if it is posted by a recruiter/headhunter agency "
    "(e.g. 'listed on behalf of a partner company', agency branding).\n"
    "- company_name: the hiring company if known, else the posting agency.\n"
    "- posting_url: the URL of the job posting if present in the JD, else null. If the url is from linkedin.com just report the url, not the query parameters.\n"
    "- gaps: Describe the gaps between the candidate and the job description."
    "- pers_preferences: Describe the gaps between the JD and the PERSONAL_PREFERENCE as text only — no score in this field.\n"
    "- pers_preference_score: The numeric (real) score obtained by summing "
    "the validated PERSONAL_PREFERENCE points against the JD "
    "(e.g. 2.5, 0.5); 0.0 if none apply. Output it as a JSON number, not a string.\n"
    "OUTPUT FORMAT (critical):\n"
    "- Reply with ONLY a single JSON object (data). No markdown, no prose "
    "before or after, no code fences.\n"
    "- The JSON must contain the fields below with REAL values extracted from "
    "the JD. Do NOT echo the schema/template itself: your top-level keys must "
    "be exactly these field names — never 'properties', 'title', 'type', "
    "'$schema', 'description'.\n"
    "- Field names and types:\n"
    "  match_percentage: int\n"
    "  match_rationale: string or null\n"
    "  job_title: string\n"
    "  work_location: string or null\n"
    "  work_mode: one of full_remote, hybrid, on_site, not_specified\n"
    "  expected_salary: string or null\n"
    "  max_salary: int or null\n"
    "  experience_level: one of entry_level, intermediate, professional, manager, director\n"
    "  hard_skills: array of strings (max 4)\n"
    "  soft_skills: array of strings (max 4)\n"
    "  company_name: string\n"
    "  posting_type: 'direct' or 'headhunter'\n"
    "  posting_url: string or null\n"
    "  gaps: string\n"
    "  pers_preferences: string\n"
    "  pers_preference_score: number\n"
    "Example of a valid response:\n"
    '{"match_percentage": 82, "match_rationale": "Strong overlap with the profile.", '
    '"job_title": "Solution Architect", "work_location": "Milan, Italy", '
    '"work_mode": "hybrid", "expected_salary": "EUR 60k-80k", '
    '"max_salary": 80000, "experience_level": "professional", '
    '"hard_skills": ["AWS", "Kubernetes"], "soft_skills": ["Communication"], '
    '"company_name": "Acme Corp", "posting_type": "direct", '
    '"posting_url": "https://example.com/job", '
    '"gaps": "No previous management experience.", '
    '"pers_preferences": "Hybrid work is acceptable.", '
    '"pers_preference_score": 1.5}\n'
)

#: The detection prompt, from the clipboard importer it used to live in.
DETECT_PROMPT = (
    "You are a job description validator. The text below was "
    "copied to a clipboard. Reply with exactly 'YES' if it is "
    "a job description (a job posting mentioning role, "
    "company, responsibilities and/or requirements), or "
    "exactly 'NO' if it is not.\n\nTEXT:\n"
)


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
        """Strip markdown code fences (if any) and validate the LLM JSON payload."""
        content = text.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        data = json.loads(content)                     # -> JSONDecodeError on bad JSON
        try:
            return JDAnalysis.model_validate(data)     # -> ValidationError on bad schema
        except ValidationError:
            # NESTED-PAYLOAD FALLBACK (isolated; roll back this whole try/except
            # to revert): some flash models wrap the real payload inside a single
            # string field, e.g. {"description": "{\"match_percentage\": ..., ...}"}.
            # Unwrap and re-validate.
            if isinstance(data, dict) and len(data) == 1:
                wrapped = next(iter(data.values()))
                if isinstance(wrapped, str):
                    return JDAnalysis.model_validate(json.loads(wrapped))
            raise

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
        for attempt in range(1, 3):
            with stage("jd.analysis", attempt=attempt):
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


__all__ = ["JDValidator", "JD_SYSTEM_PROMPT", "DETECT_PROMPT"]
