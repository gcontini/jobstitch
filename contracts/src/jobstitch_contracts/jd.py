"""The job-description side of the wire format.

:class:`JDAnalysis` is what ``POST /v1/jd/analysis`` returns and what clients
store as ``analysis.json``. Like every model-facing schema, its field
descriptions are part of the prompt.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


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


class JDDetection(BaseModel):
    """Answer to "is this text a job description?" — the cheap gate.

    Yes or no. The reasoning is in the server's log for that request, not in
    the answer: a caller only ever branches on the verdict.
    """

    is_job_description: bool = Field(description="Whether to treat this text as a posting")


__all__ = ["JDAnalysis", "JDDetection"]
