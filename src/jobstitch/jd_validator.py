"""Job description validation and analysis module.

- :class:`JDAnalysis` — Pydantic model describing a structured summary of a job
  description vs. the master candidate profile.
- :class:`JDValidator` — wraps the LLM pipeline that analyzes a job description:
  computes a match score and extracts structured facts.
"""

import json
from pathlib import Path
from typing import List, Literal, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from .model_selector import ModelSelector, build_model
from .paths import RESOURCES_DIR, resource_path


class JDAnalysis(BaseModel):
    """Structured summary of a job description vs. the master profile."""
    match_percentage: int = Field(description="0-100 how well the JD fits the profile")
    match_rationale: Optional[str] = Field(
        None, description="One-line reason for the match score"
    )
    job_title: str = Field(description="Job title extracted from the JD")
    work_location: Optional[str] = Field(
        None, description="Work location stated in the JD, e.g. 'Milan, Italy'"
    )
    work_mode: str = Field(
        description="full_remote | hybrid | on_site | not_specified"
    )
    expected_salary: Optional[str] = Field(
        None, description="Expected salary if stated, e.g. 'EUR 60k-80k'; null if absent"
    )
    max_salary: Optional[int] = Field(
        None,
        description=(
            "Upper bound of the salary range if a range is stated (or the single "
            "number if it is just a number); -1 if not found"
        ),
    )
    experience_level: Literal[
        "entry_level", "intermediate", "professional", "manager", "director"
    ] = Field(
        description="Guessed experience level required for this job"
    )
    hard_skills: List[str] = Field(
        description="Up to 4 hard/technical skills required by the JD"
    )
    soft_skills: List[str] = Field(
        description="Up to 4 soft skills required by the JD"
    )
    company_name: str = Field(
        description="The company that posted the job (or the headhunter agency)"
    )
    posting_type: str = Field(
        description="'direct' if posted by the hiring company, 'headhunter' if by a recruiter agency"
    )
    posting_url: Optional[str] = Field(
        None, description="URL of the job posting, if present in the JD; else null"
    )
    gaps: str = Field(
        description="Describe the gaps between the candidate and the job description"
    )
    pers_preferences: str = Field(
        description="Textual analysis of the gaps between the PERSONAL PREFERENCES and the job description"
    )
    pers_preference_score: float = Field(
        description=(
            "Numeric (real) score, e.g. 2.5 or 0.5, obtained by summing the "
            "validated PERSONAL_PREFERENCE points against the job description; "
            "0.0 if none apply"
        ),
    )

class JDValidator:
    """Analyzes a job description against the master candidate profile.

    Parameters
    ----------
    temperature:
        Optional sampling temperature override; when omitted the temperature
        declared for the model in ``models.toml`` applies.
    resources_dir:
        Optional path to the resources folder holding ``candidate_profile.json`` and
        ``pers_preferences.md``. Defaults to the repo's ``resources/`` folder.
    model_selector:
        Optional :class:`ModelSelector` to use for the LLM calls. When omitted,
        the ``summary`` model from ``resources/models.toml`` is built — the
        same mid-size model that writes the cover letter, since reading a job
        description is an extraction job rather than a writing one.
    """

    def __init__(
        self,
        temperature: Optional[float] = None,
        resources_dir: Optional[Path] = None,
        model_selector: Optional[ModelSelector] = None,
    ) -> None:
        load_dotenv()

        if model_selector is None:
            model_selector = build_model("summary", resources_dir=resources_dir)
        if temperature is not None:
            model_selector = model_selector.with_(temperature=temperature)
        self.model_selector = model_selector

        # Load the master profile and personal preferences from the resources
        # folder (same source the CV generator uses).
        if resources_dir is None:
            resources_dir = RESOURCES_DIR
        resources_dir = Path(resources_dir)
        self._master_profile = json.loads(
            resource_path("candidate_profile.json", resources_dir).read_text(
                encoding="utf-8"
            )
        )
        self._personal_preferences = resource_path(
            "pers_preferences.md", resources_dir
        ).read_text(encoding="utf-8")

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
    def analyze_jd(self, job_description: str) -> JDAnalysis:
        """Compare a job description against candidate_profile.json and extract key facts.

        Uses ``self.model_selector`` for the match score and all structured
        fields. Returns a validated :class:`JDAnalysis` object.
        """

        # Keep the full conversation across retries so the JD + profile context is
        # never lost (on failure we only APPEND a correction message).
        messages = [
            {
                "role": "system",
                "content": (
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
                ),
            },
            {
                "role": "user",
                "content": (
                    "JOB DESCRIPTION:\n"
                    f"{job_description}\n\n"
                    "--------------------------------------------\n"
                    "CANDIDATE PROFILE (candidate_profile.json):\n"
                    f"{json.dumps(self._master_profile, indent=2)}\n"
                    "--------------------------------------------\n"
                    "PERSONAL PREFERENCES:\n"
                    f"{self._personal_preferences}\n"
                ),
            },
        ]

        # Generate + validate, with one retry that feeds the validation error back.
        analysis = None
        for attempt in range(1, 3):
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
                print(
                    f"  ✗ JD analysis parse failed (attempt {attempt}): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )
                print(f"    raw model response: {preview!r}", flush=True)
                # Feed the model's actual (bad) output back so the correction is
                # targeted, mirroring the CV loop in cv_creation.py.
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
            raise RuntimeError("Could not obtain valid JDAnalysis from the model.")

        # Clamp skill lists to the requested 4 items.
        analysis.hard_skills = self._clamp_list(analysis.hard_skills, 4)
        analysis.soft_skills = self._clamp_list(analysis.soft_skills, 4)

        return analysis
