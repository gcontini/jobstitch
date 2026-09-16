"""The CV side of the wire format.

Two structures, deliberately separate:

- :class:`TailoredCVData` — what the model writes. Every ``Field`` description
  below is restated to the model inside three prompts (generation, review,
  highlighting) via ``model_json_schema()``, so editing one of them changes
  what the model produces. Treat this class as a prompt, not just a type.
- :class:`CVDocument` — what a renderer needs: the model's half plus the
  candidate data that is copied straight through and never sent to an LLM.
  That half is a plain dictionary on purpose: it is your data going to your
  template, and neither jobstitch nor a model has an opinion about its shape.

They are nested rather than merged so the trust boundary stays visible: a
field the model invents can never overwrite the real name, email or location.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

#: Bumped when a stored ``CVDocument`` stops being readable by this code.
CV_DOCUMENT_VERSION = 1


class WorkExperienceItem(BaseModel):
    title: str = Field(description="Job title in this specific job experience")
    company: str = Field(description="Company name")
    location: str = Field(description="Location: City, Country")
    dates: str = Field(
        description="Employment date range, e.g., February 2021 -- Present"
    )
    project_name: Optional[str] = Field(
        None,
        description="Project Description eg. 'Milano Winter Olympics -- Organizing Committee'",
    )
    bullet_points: List[str] = Field(
        description="Tailored achievements/responsibilities matching the master profile."
    )


class TailoredCVData(BaseModel):
    company_name: Optional[str] = Field("Unknown",description="The company that posted the Job Description")
    job_title: str = Field(description="Target job title extracted from job description")
    summary: str = Field(
        description="Professional summary tailored specifically to the job role"
    )
    skills: List[str] = Field(
        min_length = 6, max_length=8,
        description="Key technical and soft skills prioritized for this job, keep them grounded in candidate's profile."
    )
    experiences: List[WorkExperienceItem] = Field(
        min_length=3, max_length=5,
        description="Tailored work experience list"
    )


class CVDocument(BaseModel):
    """Everything needed to render a CV, and the unit clients store.

    ``cv`` is the model's output, ``candidate`` is copied through untouched.
    """

    version: int = Field(
        CV_DOCUMENT_VERSION, description="Format version of this document"
    )
    cv: TailoredCVData = Field(description="The tailored content the model wrote")
    candidate: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Whatever candidate_data.json holds — name, email, languages, and "
            "anything else your template prints. Passed through untouched and "
            "never sent to a model, so it is deliberately not a fixed schema: "
            "adding a field to your data and to your template is enough."
        ),
    )
    generated_at: Optional[datetime] = Field(
        None, description="When the content was generated"
    )
    job_description: Optional[str] = Field(
        None, description="The job description the CV was tailored for"
    )

    def render_context(self) -> Dict[str, Any]:
        """Flatten into the single namespace the LaTeX template renders from.

        The two halves must not collide: a key in both would mean the model
        silently overwriting a real contact detail. That is a bug in the
        schema, not something to resolve at render time, so it raises.
        """
        cv_fields = self.cv.model_dump()
        candidate_fields = dict(self.candidate)
        clash = sorted(set(cv_fields) & set(candidate_fields))
        if clash:
            raise ValueError(
                f"CVDocument.cv and .candidate both define {clash} — the "
                "template cannot know which one to print"
            )
        stamp = self.generated_at.date() if self.generated_at else date.today()
        return {
            **candidate_fields,
            "generation_date": stamp.strftime("%Y/%m/%d"),
            **cv_fields,
        }


__all__ = [
    "CV_DOCUMENT_VERSION",
    "WorkExperienceItem",
    "TailoredCVData",
    "CVDocument",
]
