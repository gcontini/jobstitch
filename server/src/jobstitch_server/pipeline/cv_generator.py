"""Writing the CV: job description in, :class:`TailoredCVData` out.

The loop is generate -> review -> render -> measure -> condense, and the
render in the middle is the point: the two-page limit is checked against a
real compiled PDF, so the instruction fed back to the model ("remove 1 bullet
point") is grounded in what actually overflowed rather than a guess. The PDF
produced along the way is thrown out with the scratch directory; the caller
merges in its own candidate data and renders the result when it wants the
file.

The page checks render your ``candidate_data`` too. They have to: a page
count taken with the contact details missing is not the page count of the CV
you will send. No model is shown that data — it goes to the template and
nowhere else — but the renderer needs it, and so the endpoint takes it.

Every input arrives in memory — prompts and template in a
:class:`~jobstitch_server.bundle.ResourceBundle`, the profile and the
candidate data in :class:`~jobstitch_server.bundle.CandidateInputs`. Nothing
is read from disk and nothing is written outside ``work_dir``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, ValidationError

from ..bundle import CandidateInputs, ResourceBundle
from ..model_selector import ModelSelector
from ..observability import LOGGER_ROOT, stage
from .cv_renderer import CVRenderer, RenderResult
from .cv_schema import TailoredCVData, prompt_schema
from .errors import BudgetExceededError, ModelOutputError
from .parsing import parse_model_json

logger = logging.getLogger(f"{LOGGER_ROOT}.cv")

# How much of a rejected payload to log: enough head to see the shape the
# model chose and enough tail to see where a truncated reply stopped. Both
# together stay under the per-line cap the run log store applies.
RAW_HEAD_CHARS = 1200
RAW_TAIL_CHARS = 400


def _raw_excerpt(content: Optional[str]) -> str:
    """Head and tail of a payload, with the middle elided."""
    if not content:
        return repr(content)
    elided = len(content) - RAW_HEAD_CHARS - RAW_TAIL_CHARS
    if elided <= 0:
        return content
    return (
        f"{content[:RAW_HEAD_CHARS]}"
        f"\n    ... [{elided} chars elided] ...\n"
        f"{content[-RAW_TAIL_CHARS:]}"
    )


def _retry_prompt(cv_data: TailoredCVData, why: str, instruction: str) -> str:
    """The CV to improve on, and what to do to it, for the next round.

    A rejected CV comes back as material in a fresh prompt rather than as the
    model's own previous turn: told to fix its own reply, the model copy-edits
    it — carrying over whatever was wrong with it — instead of writing a CV.
    """
    return (
        "\n--------------------------------------------\n"
        f"YOUR PREVIOUS ATTEMPT, {why}:\n"
        f"{cv_data.model_dump_json(indent=2)}\n"
        "--------------------------------------------\n"
        f"{instruction}"
    )


def _reply_diagnostics(response) -> str:
    """Why a reply could not be used, beyond the validation error itself.

    ``finish_reason="length"`` means the model was cut off rather than wrong,
    and reasoning counts tell a thinking budget that ate the output budget
    apart from one that was never applied — neither is visible in a Pydantic
    error.
    """
    choice = response.choices[0]
    content = choice.message.content
    bits = [
        f"finish_reason={choice.finish_reason}",
        f"content_chars={len(content or '')}",
    ]
    reasoning = getattr(choice.message, "reasoning_content", None)
    if reasoning:
        bits.append(f"reasoning_chars={len(reasoning)}")
    usage = getattr(response, "usage", None)
    if usage is not None:
        details = getattr(usage, "completion_tokens_details", None)
        bits.append(f"completion_tokens={getattr(usage, 'completion_tokens', None)}")
        bits.append(
            f"reasoning_tokens={getattr(details, 'reasoning_tokens', None) if details else None}"
        )
    return ", ".join(bits)


def _usage_of(response) -> Tuple[int, int, int]:
    """Prompt, completion and thinking tokens for one call; zeros if unreported.

    The per-call line in the log is still the ledger. This is the same numbers
    added up, so a step can say what it cost while it is still running.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0
    details = getattr(usage, "completion_tokens_details", None)
    return (
        getattr(usage, "prompt_tokens", 0) or 0,
        getattr(usage, "completion_tokens", 0) or 0,
        (getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
    )


#: Called as the run moves on: the step starting now, and one line about the
#: one that just finished. Both go straight to a client, unread by anything.
ProgressFn = Callable[[str, str], None]


class ReviewResult(BaseModel):
    status: Literal["OK", "REVIEW"] = Field(
        description="'OK' when the CV content is acceptable, 'REVIEW' when the CV must be regenerated fixing the issues in 'violations'."
    )
    violations: List[str] = Field(
        max_length= 10,
        default_factory=list,
        description="Specific, actionable issues the CV generator must fix when status=='REVIEW'. Empty list when status=='OK'.",
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
        The profile the model tailors from. Not cached; it dies with the
        request.
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
    progress:
        Called at each step boundary. ``None`` runs the whole thing silently,
        which is what every test does.
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
        page_limit: Optional[int] = None,
        progress: Optional[ProgressFn] = None,
    ) -> None:
        self.cv_model = cv_model
        self.highlight_model = highlight_model
        self.bundle = bundle
        self.candidate = candidate
        self.work_dir = Path(work_dir)
        self.max_attempts = max_attempts
        self.max_validation_attempts = max_validation_attempts
        self.deadline = deadline
        self.progress = progress
        self._calls = 0
        self._tokens = 0
        self._thinking = 0

        renderer_kwargs: Dict[str, Any] = {}
        if latex_timeout is not None:
            renderer_kwargs["latex_timeout"] = latex_timeout
        if page_limit is not None:
            renderer_kwargs["page_limit"] = page_limit
        self.renderer = CVRenderer(
            template_source=bundle.template_source,
            template_name=bundle.template_name,
            assets=bundle.assets,
            work_dir=self.work_dir,
            **renderer_kwargs,
        )

        self._master_profile = dict(candidate.profile)
        self._system_message = {"role": "system", "content": bundle.sys_prompt_cv}
        self._highlight_message = {"role": "system", "content": bundle.sys_prompt_highlight}
        self._review_message = {"role": "system", "content": bundle.sys_prompt_review}
        self._cached_schema = prompt_schema()

    # --- helpers ------------------------------------------------------------
    def _call(self, model: ModelSelector, messages, response_format):
        """Every model call goes through here, so a step can be costed."""
        response = model.completions_create(messages, response_format=response_format)
        prompt, completion, thinking = _usage_of(response)
        self._calls += 1
        self._tokens += prompt + completion
        self._thinking += thinking
        return response

    def _mark(self) -> Tuple[int, int, float]:
        """Where the counters stand now; a step's cost is the difference."""
        return self._tokens, self._thinking, time.monotonic()

    def _cost(self, mark: Tuple[int, int, float], what: str) -> str:
        tokens, thinking, started = mark
        return (
            f"{what}, tokens used={self._tokens - tokens}, "
            f"thinking={self._thinking - thinking}, "
            f"elapsed={time.monotonic() - started:.1f}s"
        )

    def _report(self, status: str, detail: str = "") -> None:
        if self.progress is not None:
            self.progress(status, detail)

    def _document(self, cv_data: TailoredCVData) -> Dict[str, Any]:
        """One flat namespace for the template.

        Your own data goes over the top, so a field the model invents can
        never replace a real name, email or address.
        """
        return {**cv_data.model_dump(), **dict(self.candidate.data)}

    def _check_deadline(self, attempt: int) -> None:
        """Stop before starting a round that cannot finish in the budget."""
        if self.deadline is None or time.monotonic() < self.deadline:
            return
        raise BudgetExceededError(
            f"time budget exhausted after {attempt} attempt(s)",
            stage="cv.generate",
            detail={"attempts": attempt},
        )

    def _render(self, cv_data: TailoredCVData, stem: str) -> RenderResult:
        """Render the CV. The page checks throw the bytes away; the last one
        does not — it is the PDF the caller asked for."""
        return self.renderer.render_document(self._document(cv_data), stem=stem)

    @staticmethod
    def _extract_cv_data(response) -> TailoredCVData:
        """Parse the LLM response content into a validated ``TailoredCVData``.

        Raises ``ValueError`` (incl. ``json.JSONDecodeError``) or
        ``ValidationError`` when the content is missing or does not conform to
        the schema; both are fed back to the model for a corrective retry.
        """
        return parse_model_json(response.choices[0].message.content, TailoredCVData)

    @staticmethod
    def _extract_review_result(response) -> ReviewResult:
        """Parse the reviewer's response content into a :class:`ReviewResult`.

        Raises ``ValueError`` (incl. ``json.JSONDecodeError``) or
        ``ValidationError`` when the content is missing or does not conform to
        the schema.
        patch the model return in case.
        """

        review_result = parse_model_json(
            response.choices[0].message.content,
            ReviewResult,
            empty="Reviewer returned empty content",
        )
        #remove empty and too short violations
        review_result.violations[:] = [x for x in review_result.violations if len(x)>15] 
        review_result.status = "OK" if len(review_result.violations) == 0 else "REVIEW"
        return review_result

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
            resp = self._call(
                self.highlight_model,
                messages,
                self.highlight_model.response_format("cv_data", self._cached_schema),
            )
            try:
                return self._extract_cv_data(resp)
            except (ValueError, ValidationError) as e:
                diag = _reply_diagnostics(resp)
                if attempt + 1 < max_attempts:
                    logger.warning(
                        "  ↻ highlighting attempt %d failed (%s: %s; %s) — retrying",
                        attempt + 1, type(e).__name__, e, diag,
                    )
                    continue
                logger.warning(
                    "  ⚠ highlighting failed (%s: %s; %s) — continuing with the "
                    "un-highlighted CV\n    payload: %s",
                    type(e).__name__, e, diag,
                    _raw_excerpt(resp.choices[0].message.content),
                )
                return cv_data

        return cv_data  # unreachable: loop always returns on its last iteration

    def _generate_valid_cv_data(self, prompt: str) -> TailoredCVData:
        """Generate and schema-validate :class:`TailoredCVData` for one prompt.

        Runs up to ``max_validation_attempts`` calls against ``cv_model``
        (json_schema ``response_format``), feeding the validation errors back
        to the LLM until the output is valid. Raises ``ModelOutputError`` when
        no valid payload is produced.

        The conversation is built here and dropped on return, so only the
        schema-correction turns ever accumulate — one round's CV never becomes
        context for the next.
        """
        messages: List[Dict[str, Any]] = [
            self._system_message,
            {"role": "user", "content": prompt},
        ]
        for val_attempt in range(self.max_validation_attempts):
            cv = self._call(
                self.cv_model,
                messages,
                self.cv_model.response_format("cv_data", self._cached_schema),
            )

            messages.append(
                {"role": "assistant", "content": cv.choices[0].message.content}
            )

            try:
                cv_data = self._extract_cv_data(cv)   # parse content + validate
                # The payload is logged on the way through, not only when it
                # is rejected: a CV can be schema-valid and still garbled, and
                # then this is the only record of what the model actually wrote.
                logger.info(
                    "  ✓ Output validated against TailoredCVData "
                    "(validation attempt %d)\n    payload: %s",
                    val_attempt + 1, _raw_excerpt(cv.choices[0].message.content),
                )
                return cv_data
            except (ValueError, ValidationError) as e:
                logger.error(
                    "  ✗ Validation failed (attempt %d): %s: %s\n"
                    "    reply: %s\n"
                    "    payload: %s",
                    val_attempt + 1, type(e).__name__, e,
                    _reply_diagnostics(cv),
                    _raw_excerpt(cv.choices[0].message.content),
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

    def _review_cv_data(self, cv_data: TailoredCVData, attempt:int) -> ReviewResult:
        """Review generated CV content against the master profile.

        Uses the same model as CV generation (``cv_model``) with the
        review system prompt; the master profile and the generated CV are the
        only inputs.
        On an unparsable/unschema-valid response the review is retried once
        (the error is fed back to the model); if it still fails after 2
        attempts a warning is printed and an ``OK`` (no violations) result is
        returned so the unvalidated CV proceeds through the pipeline.
        """
        
        review_request = ("Review the GENERATED CV below against the candidate MASTER "
            "PROFILE.\n") 
        if attempt > 0:
             review_request += ("The GENERATED CV has already been reviewed before."
                                "Flag only Syntax and Logic issues.")
        review_request += (
            "--------------------------------------------\n"
            "MASTER PROFILE:\n"
            f"{json.dumps(self._master_profile, indent=2)}\n"
            "--------------------------------------------\n"
            "GENERATED CV:\n"
            f"{cv_data.model_dump_json(indent=2)}\n"
            "--------------------------------------------\n"
            "Output a single JSON object with 'status' 'OK' or "
            "['REVIEW' and 'violations' (list of specific issues to fix)]."
        )

        messages = [
            self._review_message,
            {"role": "user", "content": review_request},
        ]

        for retry in range(2):
            resp = self._call(
                self.cv_model,
                messages,
                self.cv_model.response_format(
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
                    "  ✗ Review response invalid (retry %d): %s: %s\n"
                    "    reply: %s\n"
                    "    payload: %s",
                    retry + 1, type(e).__name__, e,
                    _reply_diagnostics(resp),
                    _raw_excerpt(resp.choices[0].message.content),
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

    def generate(
        self, job_description: str
    ) -> Tuple[Dict[str, Any], str, bytes, str]:
        """Write, review, condense, highlight and render one CV.

        The loop is generate -> review -> render -> measure -> condense. The
        render in the middle is what makes the page limit real: the model is
        told to cut based on an actual page count, not an estimate. The review
        runs until it passes once; condensing attempts after that only measure
        length. After the loop the keywords are highlighted once and the result
        is rendered for good.

        Returns the merged document, its LaTeX, its PDF and one line saying
        what the run cost. Raises :class:`ModelOutputError` if no attempt
        produced a usable CV.
        """
        started = time.monotonic()
        user_prompt = (
            "Please tailor my CV for this JOB DESCRIPTION. "
            "Output strictly json format.\n"
            "--------------------------------------------\n"
            "MASTER PROFILE:\n"
            f"{json.dumps(self._master_profile, indent=2)}\n"
            "--------------------------------------------\n"
            "JOB DESCRIPTION:\n"
            f"{job_description}\n\n"
        )
        if not self.cv_model.sends_schema():
            user_prompt += (
                "--------------------------------------------\n"
                "JSON_SCHEMA (TailoredCVData) the output must satisfy:\n"
                f"{json.dumps(self._cached_schema)}\n"
            )

        final_cv_data = None
        content_reviewed = False
        # What the last round got wrong, appended to the task prompt for the
        # next one. Every attempt is its own conversation: the attempts used
        # to accumulate, and a model shown three of its own near-identical
        # replies stops writing and starts copying them.
        retry = ""
        # Each report names the step starting now and what the one before it
        # cost, so one call is one complete answer to "where is my CV".
        self._report("generate")

        for attempt in range(self.max_attempts):
            self._check_deadline(attempt)
            logger.info("--- attempt %d ---", attempt + 1)

            # Generate -> validate against TailoredCVData, feeding the
            # validation errors back to the LLM until the output is
            # schema-valid.
            mark = self._mark()
            with stage("cv.generate"):
                cv_data = self._generate_valid_cv_data(user_prompt + retry)
            self._report(
                "review" if not content_reviewed else "page_check",
                self._cost(mark, "generation finished"),
            )

            # Content review (same model as generation): REVIEW rejects the CV
            # and its violations are fed back for regeneration on the next
            # attempt; OK lets the CV proceed to rendering. Once it passes, the
            # only thing later attempts change is length, so it is not re-run.
            if not content_reviewed:
                logger.info("--- content review ---")
                mark = self._mark()
                with stage("cv.review"):
                    review = self._review_cv_data(cv_data, attempt)

                if review.status == "REVIEW":
                    # Print the violations the reviewer requested so they are
                    # visible in the log (they are also fed back to the
                    # generator).
                    logger.info(
                        "  ✗ Review rejected CV (%d violation(s)) — regenerating",
                        len(review.violations),
                    )
                    for violation in review.violations:
                        logger.info("      - %s", violation)
                    violations_text = "\n".join(f"- {v}" for v in review.violations)
                    self._report(
                        "re-generate",
                        self._cost(mark, "review rejected the CV")
                        + "\n"
                        + violations_text,
                    )
                    retry = _retry_prompt(
                        cv_data,
                        "rejected by the content reviewer",
                        "Rewrite it as a single valid JSON object strictly "
                        "matching the TailoredCVData schema, fixing ALL of the "
                        "following issues:\n"
                        f"{violations_text}",
                    )
                    continue

                logger.info("  ✓ Review OK")
                content_reviewed = True
                self._report("page_check", self._cost(mark, "review passed"))

            logger.info("--- render %d ---", attempt + 1)
            with stage("render"):
                result = self._render(cv_data, f"attempt_{attempt + 1}")
            final_cv_data = cv_data
            logger.info("  Page check: %d pages -> %s", result.pages, result.advice)

            if result.pages <= self.renderer.page_limit:
                logger.info("  ✓ Length OK (%d pages)", result.pages)
                self._report("highlight", f"page check passed: {result.pages} page(s)")
                break

            logger.info("PDF too long — condensing and retrying...")
            # result.advice is the instruction, verbatim: the client sees the
            # same words the model is about to be given.
            self._report(
                "re-generate",
                f"page check failed: {result.pages} page(s)\n{result.advice}",
            )
            retry = _retry_prompt(cv_data, "too long", result.advice)

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

        # The deliverable: the same render the page check did, on the
        # highlighted content, kept this time.
        with stage("render"):
            result = self._render(final_cv_data, "cv")
        document = self._document(final_cv_data)
        summary = (
            f"done, {self._calls} model calls, tokens used={self._tokens}, "
            f"thinking={self._thinking}, "
            f"elapsed={time.monotonic() - started:.1f}s, {result.pages} page(s)"
        )
        return document, result.tex, result.pdf, summary


__all__ = ["CVGenerator", "ReviewResult"]
