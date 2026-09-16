"""Writing the CV: job description in, :class:`CVDocument` out.

The loop is generate -> review -> render -> measure -> condense, and the
render in the middle is the point: the two-page limit is checked against a
real compiled PDF, so the instruction fed back to the model ("remove 1 bullet
point") is grounded in what actually overflowed rather than a guess. The PDF
produced along the way is thrown out with the scratch directory; the caller
renders the returned document when it wants the file.

Every input arrives in memory — prompts and template in a
:class:`~jobstitch_server.bundle.ResourceBundle`, profile and candidate data
in :class:`~jobstitch_server.bundle.CandidateInputs`. Nothing is read from
disk and nothing is written outside ``work_dir``.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from jobstitch_contracts import CVDocument, TailoredCVData, WorkExperienceItem
from pydantic import BaseModel, Field, ValidationError

from ..bundle import CandidateInputs, ResourceBundle
from ..model_selector import ModelSelector
from ..observability import LOGGER_ROOT, stage
from .cv_renderer import PAGE_LIMIT, CVRenderer, RenderResult
from .errors import BudgetExceededError, ModelOutputError

logger = logging.getLogger(f"{LOGGER_ROOT}.cv")


class ReviewResult(BaseModel):
    status: Literal["OK", "REVIEW"] = Field(
        description="'OK' when the CV content is acceptable, 'REVIEW' when the CV must be regenerated fixing the issues in 'violations'."
    )
    violations: List[str] = Field(
        max_length= 10,
        default_factory=list,
        description="Specific, actionable issues the CV generator must fix when status=='REVIEW'. Empty when status=='OK'.",
    )


class CVGenerator:
    """Generates tailored CV content for one job description.

    Parameters
    ----------
    cv_model:
        The ``cv`` model: writes the CV and reviews it. Built once by the
        caller and shared across requests — it holds an HTTP client, not
        per-job state.
    highlight_model:
        The ``highlight`` model, which adds ``**bold**``/``*italics*`` markers
        to the finished content. ``None`` skips that pass.
    bundle:
        Prompts and the LaTeX template for this run (defaults, or whatever the
        request overrode).
    candidate:
        The profile the model tailors from and the candidate data the render
        prints. Neither is cached; both die with the request.
    work_dir:
        Scratch directory for the page-check renders. Created and removed by
        the caller.
    max_attempts:
        Generate -> review -> render -> page-check rounds before giving up.
    max_validation_attempts:
        Schema-validation retries within one round.
    deadline:
        Optional :func:`time.monotonic` value. Checked between rounds so a run
        that cannot finish in time fails with a clear cause instead of being
        cut off by a proxy.
    """

    def __init__(
        self,
        *,
        cv_model: ModelSelector,
        bundle: ResourceBundle,
        candidate: CandidateInputs,
        work_dir: Path,
        highlight_model: Optional[ModelSelector] = None,
        max_attempts: int = 4,
        max_validation_attempts: int = 3,
        deadline: Optional[float] = None,
        latex_timeout: Optional[float] = None,
    ) -> None:
        self.cv_model = cv_model
        self.highlight_model = highlight_model
        self.bundle = bundle
        self.candidate = candidate
        self.work_dir = Path(work_dir)
        self.max_attempts = max_attempts
        self.max_validation_attempts = max_validation_attempts
        self.deadline = deadline

        renderer_kwargs: Dict[str, Any] = {}
        if latex_timeout is not None:
            renderer_kwargs["latex_timeout"] = latex_timeout
        self.renderer = CVRenderer(
            template_source=bundle.template_source,
            template_name=bundle.template_name,
            assets=bundle.assets,
            work_dir=self.work_dir,
            **renderer_kwargs,
        )

        self._master_profile = dict(candidate.profile)
        self._candidate_data = dict(candidate.data)
        self._system_message = {"role": "system", "content": bundle.sys_prompt_cv}
        self._highlight_message = {"role": "system", "content": bundle.sys_prompt_highlight}
        self._review_message = {"role": "system", "content": bundle.sys_review_prompt}
        self._cached_schema = TailoredCVData.model_json_schema()

    # --- helpers ------------------------------------------------------------
    def _check_deadline(self, attempt: int) -> None:
        """Stop before starting a round that cannot finish in the budget."""
        if self.deadline is None or time.monotonic() < self.deadline:
            return
        raise BudgetExceededError(
            f"time budget exhausted after {attempt} attempt(s)",
            stage="cv.generate",
            detail={"attempts": attempt},
        )

    def _document(self, cv_data: TailoredCVData, job_description: str) -> CVDocument:
        """Wrap generated content together with the copied-through data."""
        return CVDocument(
            cv=cv_data,
            candidate=self._candidate_data,
            generated_at=datetime.now(timezone.utc),
            job_description=job_description,
        )

    def _render(self, cv_data: TailoredCVData, attempt: int) -> RenderResult:
        """Render for the page check only; the bytes are discarded."""
        return self.renderer.render_document(
            self._document(cv_data, ""), stem=f"attempt_{attempt + 1}"
        )

    @staticmethod
    def _extract_cv_data(response) -> TailoredCVData:
        """Parse the LLM response content into a validated ``TailoredCVData``.

        Strips Markdown code fences if present, parses the JSON payload and
        validates it against the :class:`TailoredCVData` Pydantic model. Raises
        ``ValueError`` (incl. ``json.JSONDecodeError``) or ``ValidationError``
        when the content is missing or does not conform to the schema.
        """
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM returned empty content")

        text = content.strip()
        # Strip Markdown code fences around the JSON payload, if present.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        data = json.loads(text)                     # -> JSONDecodeError on bad JSON
        return TailoredCVData.model_validate(data)  # -> ValidationError on bad schema

    @staticmethod
    def _extract_review_result(response) -> ReviewResult:
        """Parse the reviewer's response content into a :class:`ReviewResult`.

        Strips Markdown code fences if present, parses the JSON payload and
        validates it against :class:`ReviewResult`. Raises ``ValueError``
        (incl. ``json.JSONDecodeError``) or ``ValidationError`` when the
        content is missing or does not conform to the schema.
        """
        content = response.choices[0].message.content
        if not content:
            raise ValueError("Reviewer returned empty content")

        text = content.strip()
        # Strip Markdown code fences around the JSON payload, if present.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        data = json.loads(text)                     # -> JSONDecodeError on bad JSON
        return ReviewResult.model_validate(data)    # -> ValidationError on bad schema

    def _highlight_keywords(
        self, cv_data: TailoredCVData, job_description: str
    ) -> TailoredCVData:
        """Add ``**bold**``/``*italics*`` keyword markers to validated CV data.

        Uses the separate ``highlight_model`` (when configured) on the
        already schema-validated ``cv_data``. The highlighter only inserts
        Markdown markers inside existing string values — content, structure
        and order are preserved — following the rules in the highlight prompt.
        The ``response_format`` is whatever the highlighter's endpoint
        declares it supports (see
        :meth:`~jobstitch.model_selector.ModelSelector.response_format`); the
        ``TailoredCVData`` JSON schema is also restated in the prompt, which
        substitutes for the structural guarantee strict ``json_schema`` mode
        would otherwise give on endpoints that lack it.
        Up to 2 attempts: on any failure (empty/invalid output) a warning
        (including ``finish_reason`` and any reasoning-token usage, for
        diagnosing truncation) is printed and, after the last attempt, the
        un-highlighted ``cv_data`` is returned so the job still succeeds.
        Returns the highlighted data re-validated as :class:`TailoredCVData`.
        """
        if self.highlight_model is None:
            return cv_data

        messages = [
            self._highlight_message,
            {
                "role": "user",
                "content": (
                    "Highlight the keywords in the CV_DATA below.\n"
                    "Output the SAME JSON object (CV_DATA), unchanged except for the "
                    "added **bold**/*italics* markers inside string values. The "
                    "output MUST be a single JSON object valid against this JSON "
                    "Schema:\n"
                    "--------------------------------------------\n"
                    "JSON_SCHEMA:\n"
                    f"{json.dumps(self._cached_schema)}\n"
                    "--------------------------------------------\n"
                    "CV_DATA:\n"
                    f"{cv_data.model_dump_json(indent=2)}\n"
                    "--------------------------------------------\n"
                    "JOB_DESCRIPTION:\n"
                    f"{job_description}\n"
                    "--------------------------------------------\n"
                ),
            },
        ]

        max_attempts = 2
        for attempt in range(max_attempts):
            resp = self.highlight_model.completions_create(
                messages,
                response_format=self.highlight_model.response_format(
                    "cv_data", self._cached_schema
                ),
            )
            try:
                return self._extract_cv_data(resp)
            except (ValueError, ValidationError) as e:
                choice = resp.choices[0]
                reasoning = getattr(choice.message, "reasoning_content", None)
                diag = f"finish_reason={choice.finish_reason}"
                if reasoning:
                    diag += f", reasoning_content_len={len(reasoning)}"
                if attempt + 1 < max_attempts:
                    logger.warning(
                        "  ↻ highlighting attempt %d failed (%s: %s; %s) — retrying",
                        attempt + 1, type(e).__name__, e, diag,
                    )
                    continue
                logger.warning(
                    "  ⚠ highlighting failed (%s: %s; %s) — continuing with the "
                    "un-highlighted CV", type(e).__name__, e, diag,
                )
                return cv_data

        return cv_data  # unreachable: loop always returns on its last iteration

    def _generate_valid_cv_data(self, messages: List[Dict[str, Any]]) -> TailoredCVData:
        """Generate and schema-validate :class:`TailoredCVData`.

        Runs up to ``max_validation_attempts`` calls against
        ``cv_model`` (json_schema ``response_format``), feeding the
        validation errors back to the LLM until the output is valid. Raises
        ``RuntimeError`` when no valid payload is produced. ``messages`` is
        mutated in place (assistant + error-feedback turns are appended).
        """
        for val_attempt in range(self.max_validation_attempts):
            cv = self.cv_model.completions_create(
                messages,
                response_format=self.cv_model.response_format(
                    "cv_data", self._cached_schema
                ),
            )

            messages.append(
                {"role": "assistant", "content": cv.choices[0].message.content}
            )

            try:
                cv_data = self._extract_cv_data(cv)   # parse content + validate
                logger.info(
                    "  ✓ Output validated against TailoredCVData "
                    "(validation attempt %d)", val_attempt + 1,
                )
                return cv_data
            except (ValueError, ValidationError) as e:
                logger.error(
                    "  ✗ Validation failed (attempt %d): %s: %s",
                    val_attempt + 1, type(e).__name__, e,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was NOT valid against the "
                            "TailoredCVData schema.\n"
                            f"Validation error: {type(e).__name__}: {e}\n\n"
                            "Fix the errors above and output a single valid JSON "
                            "object strictly matching the TailoredCVData schema."
                        ),
                    }
                )

        raise ModelOutputError(
            "Could not obtain valid TailoredCVData after "
            f"{self.max_validation_attempts} validation attempts.",
            stage="cv.generate",
        )

    def _review_cv_data(self, cv_data: TailoredCVData) -> ReviewResult:
        """Review generated CV content against the master profile.

        Uses the same model as CV generation (``cv_model``) with the
        review system prompt; the master profile and the generated CV are the
        only inputs.
        On an unparsable/unschema-valid response the review is retried once
        (the error is fed back to the model); if it still fails after 2
        attempts a warning is printed and an ``OK`` (no violations) result is
        returned so the unvalidated CV proceeds through the pipeline.
        """
        review_request = (
            "Review the GENERATED CV below against the candidate MASTER "
            "PROFILE.\n"
        )
        review_request += (
            "--------------------------------------------\n"
            "MASTER PROFILE:\n"
            f"{json.dumps(self._master_profile, indent=2)}\n"
            "--------------------------------------------\n"
            "GENERATED CV:\n"
            f"{cv_data.model_dump_json(indent=2)}\n"
            "--------------------------------------------\n"
            "Output a single JSON object with 'status' ('OK' or "
            "'REVIEW') and 'violations' (list of specific issues to fix)."
        )

        messages = [
            self._review_message,
            {"role": "user", "content": review_request},
        ]

        for retry in range(2):
            resp = self.cv_model.completions_create(
                messages,
                response_format=self.cv_model.response_format(
                    "review_output", ReviewResult.model_json_schema()
                ),
            )

            messages.append(
                {"role": "assistant", "content": resp.choices[0].message.content}
            )

            try:
                return self._extract_review_result(resp)
            except (ValueError, ValidationError) as e:
                logger.error(
                    "  ✗ Review response invalid (retry %d): %s: %s",
                    retry + 1, type(e).__name__, e,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was NOT valid against the "
                            "review schema.\n"
                            f"Validation error: {type(e).__name__}: {e}\n\n"
                            "Output a single valid JSON object with 'status' "
                            "('OK' or 'REVIEW') and 'violations' (list of strings)."
                        ),
                    }
                )

        # After 2 failed attempts do not block the job: warn and let the
        # (unvalidated) CV proceed as if the review passed.
        logger.warning(
            "  ⚠ Content reviewer failed to return a valid review after 2 "
            "attempts — continuing with the unvalidated CV"
        )
        return ReviewResult(status="OK", violations=[])

    def generate(self, job_description: str) -> CVDocument:
        """Generate the tailored CV content for one job description.

        Builds the prompt from the job description and the master profile,
        generates and schema-validates the :class:`TailoredCVData` (feeding
        validation errors back to the LLM), content-reviews it against the
        master profile (REVIEW violations trigger a regeneration), renders it
        to a PDF in the scratch directory and condenses it if it exceeds the
        page limit (up to ``max_attempts``). The render is what makes the page
        limit real: the model is told to cut based on an actual page count,
        not an estimate. The PDF itself is discarded — the caller renders the
        returned document when it wants one.

        Keyword highlighting runs once after the loop. Returns the
        :class:`CVDocument`; raises :class:`ModelOutputError` if no attempt
        produced a usable CV.
        """
        messages = [
            self._system_message,
            {
                "role": "user",
                "content": (
                    "Please tailor my CV for this JOB DESCRIPTION. "
                    "Output strictly json format.\n"
                    "--------------------------------------------\n"
                    # Restated here as well as in response_format: endpoints
                    # that only support {"type": "json_object"} never see the
                    # schema otherwise.
                    "JSON_SCHEMA (TailoredCVData) the output must satisfy:\n"
                    f"{json.dumps(self._cached_schema)}\n"
                    "--------------------------------------------\n"
                    "MASTER PROFILE:\n"
                    f"{json.dumps(self._master_profile, indent=2)}\n"
                    "--------------------------------------------\n"
                    "JOB DESCRIPTION:\n"
                    f"{job_description}\n\n"
                ),
            },
        ]

        final_cv_data = None

        for attempt in range(self.max_attempts):
            self._check_deadline(attempt)
            logger.info("--- attempt %d ---", attempt + 1)

            # Generate -> validate against TailoredCVData, feeding the
            # validation errors back to the LLM until the output is
            # schema-valid.
            with stage("cv.generate", attempt=attempt + 1):
                cv_data = self._generate_valid_cv_data(messages)

            # Content review (same model as generation): REVIEW rejects the CV
            # and its violations are fed back for regeneration on the next
            # attempt; OK lets the CV proceed to rendering.
            logger.info("--- content review ---")
            with stage("cv.review", attempt=attempt + 1):
                review = self._review_cv_data(cv_data)
            if review.status == "REVIEW" and not review.violations:
                # The reviewer flagged REVIEW without any specifics — this
                # violates its own prompt and should be rare. There is
                # nothing actionable to feed back to the generator, so treat
                # it as a pass rather than regenerating against a made-up
                # instruction.
                logger.warning(
                    "  ⚠ Reviewer returned REVIEW with no violations — treating as OK"
                )
                review.status = "OK"

            if review.status == "REVIEW":
                # Print the violations the reviewer requested so they are
                # visible in the log (they are also fed back to the generator).
                logger.info(
                    "  ✗ Review rejected CV (%d violation(s)) — regenerating",
                    len(review.violations),
                )
                for violation in review.violations:
                    logger.info("      - %s", violation)
                violations_text = "\n".join(f"- {v}" for v in review.violations)
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous CV was rejected by the content "
                            "reviewer. Fix ALL of the following issues and "
                            "output a single valid JSON object strictly "
                            "matching the TailoredCVData schema:\n"
                            f"{violations_text}"
                        ),
                    }
                )
                continue
            logger.info("  ✓ Review OK")

            logger.info("--- render %d ---", attempt + 1)
            with stage("render", attempt=attempt + 1):
                result = self._render(cv_data, attempt)
            final_cv_data = cv_data
            logger.info("  Page check: %d pages -> %s", result.pages, result.advice)

            if result.pages <= PAGE_LIMIT:
                logger.info("  ✓ Length OK (%d pages)", result.pages)
                break

            logger.info("PDF too long — condensing and retrying...")
            messages.append(
                {
                    "role": "user",
                    "content": result.advice
                }
            )

        if final_cv_data is None:
            raise ModelOutputError(
                f"No CV passed review in {self.max_attempts} attempts.",
                stage="cv.generate",
                detail={"attempts": self.max_attempts},
            )

        # Keyword highlighting with the separate fast model — run ONCE, after
        # the loop, on the final (within page-limit) CV data (no retry loop —
        # the data is already schema-valid).
        logger.info("--- keyword highlighting ---")
        with stage("cv.highlight"):
            final_cv_data = self._highlight_keywords(final_cv_data, job_description)

        return self._document(final_cv_data, job_description)


__all__ = ["CVGenerator", "ReviewResult"]
