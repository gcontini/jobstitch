"""CV creation module — :class:`CVGenerator`.

:class:`CVGenerator` — LLM-driven pipeline that produces a tailored CV PDF
for a job description: builds the prompt from the JD and the master profile,
generates/validates the structured CV data (the :class:`TailoredCVData` /
:class:`WorkExperienceItem` models, defined in
:mod:`jobstitch.cv_renderer`) with the LLM, content-reviews it against the
master profile, renders it through :class:`CVRenderer` (same module) and
condenses it if it exceeds the page limit (:meth:`CVGenerator.generate_cv`).

The review/output models :class:`ReviewResult` and :class:`ResultData` used by
the generation pipeline are defined here.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from .cv_renderer import CVRenderer, TailoredCVData, WorkExperienceItem
from .model_selector import ModelSelector
from .paths import resource_path

# ---------------------------------------------------------------------------
# Pydantic models for the CV pipeline outputs -> must match expectations.
# ---------------------------------------------------------------------------
class ResultData(BaseModel):
    status: str = Field(description="Result of operation: OK=Success, VALIDATION_ERROR=Validation error")
    pdf_path: Optional[str] = Field(None, description="Path of the generated PDF if status==OK, None otherwise")
    errors: List[str] = Field(
        description="if status==OK empty, if status==VALIDATION_ERROR List of validation errors."
    )


class ReviewResult(BaseModel):
    status: Literal["OK", "REVIEW"] = Field(
        description="'OK' when the CV content is acceptable, 'REVIEW' when the CV must be regenerated fixing the issues in 'violations'."
    )
    violations: List[str] = Field(
        max_length= 10,
        default_factory=list,
        description="Specific, actionable issues the CV generator must fix when status=='REVIEW'. Empty when status=='OK'.",
    )


# ---------------------------------------------------------------------------
# Cached loaders for static inputs (read once per process).
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _load_json_file(path: str) -> Dict[str, Any]:
    """Read and parse a JSON file; the result is cached per process."""
    return json.loads(Path(path).read_text())


@lru_cache(maxsize=8)
def _load_text_file(path: str) -> str:
    """Read a text file; the content is cached per process."""
    return Path(path).read_text()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CVGenerator class — LLM-driven CV tailoring pipeline.
# ---------------------------------------------------------------------------
class CVGenerator:
    """Generates a tailored CV PDF for a given job description using an LLM.

    Wraps the full pipeline: builds the LLM prompt from the job description
    and the master profile, generates and schema-validates the
    :class:`TailoredCVData` (retrying with the validation errors fed back to
    the LLM), renders/compiles the CV through :class:`CVRenderer` and condenses
    it if it exceeds the 2-page limit.

    Parameters
    ----------
    job_dir:
        The isolated per-job folder the CV runs fully inside. Fixed at
        construction time and kept for the whole lifetime of the generator
        (all rendering/compiling happens under this directory).
    cv_model:
        The ``cv`` model from ``models.toml`` — a :class:`ModelSelector`
        bundling the endpoint, model name and generation parameters for the CV
        content and the content review. Created once by the caller and shared
        across jobs (no per-instance client construction here).
    highlight_model:
        The ``highlight`` model (mid-size, thinking off), used by
        :meth:`_highlight_keywords` to add Markdown ``**bold**``/``*italics*``
        markers after validation. Pass ``None`` to skip highlighting.
    resources_dir:
        Directory containing the Jinja ``.tex`` templates, the system prompt
        files and the master ``candidate_profile.json`` (the repo's
        ``resources/`` folder).
    system_prompt_file:
        Name of the system prompt file inside ``resources_dir``.
    highlight_prompt_file:
        Name of the keyword-highlighting prompt file inside ``resources_dir``.
    review_prompt_file:
        Name of the content-review prompt file inside ``resources_dir``. The
        reviewer reuses ``cv_model`` (the same model as generation).
    resume_file:
        Name of the Jinja template file to render.
    max_attempts:
        Max generate -> render -> page-check attempts (condensing between).
    max_validation_attempts:
        Max schema-validation retries per attempt.
    """

    def __init__(
        self,
        job_dir: Path,
        cv_model: ModelSelector,
        *,
        highlight_model: Optional[ModelSelector] = None,
        resources_dir: Path,
        system_prompt_file: str = "sys_prompt_cv.txt",
        highlight_prompt_file: str = "sys_prompt_highlight.txt",
        review_prompt_file: str = "sys_review_prompt.txt",
        resume_file: str = "resume3.tex.jinja",
        max_attempts: int = 4,
        max_validation_attempts: int = 3,
    ) -> None:
        # One job = one isolated folder: job_dir is fixed at construction time
        # and kept for the whole lifetime of this generator.
        self.job_dir = job_dir
        resources_dir = Path(resources_dir)
        self.renderer = CVRenderer(
            resources_dir=resources_dir,
            output_dir=self.job_dir,
            resume_file=resume_file,
        )
        self.cv_model = cv_model
        self.highlight_model = highlight_model
        self.max_attempts = max_attempts
        self.max_validation_attempts = max_validation_attempts

        # Load the master profile (candidate data used to tailor the CV).
        self._master_profile = _load_json_file(
            str(resource_path("candidate_profile.json", resources_dir))
        )

        # Load the system prompt enforcing content/length tailoring rules.
        sys_prompt_path = resource_path(system_prompt_file, resources_dir)
        self._system_message = {
            "role": "system",
            "content": _load_text_file(str(sys_prompt_path)),
        }

        # Load the keyword-highlighting prompt (separate fast model).
        self._highlight_message = {
            "role": "system",
            "content": _load_text_file(
                str(resource_path(highlight_prompt_file, resources_dir))
            ),
        }

        # Load the content-review prompt (reviewer reuses the generation model).
        self._review_message = {
            "role": "system",
            "content": _load_text_file(
                str(resource_path(review_prompt_file, resources_dir))
            ),
        }

        self._cached_schema = TailoredCVData.model_json_schema()

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

    def recompile_tex(self, tex_filename: Optional[str] = None) -> str:
        """Recompile a ``.tex`` file (default: latest in ``output_dir``) to PDF.

        Convenience wrapper around :meth:`CVRenderer.recompile_tex` for
        re-rendering after manual edits to a generated ``.tex`` file. Returns
        the absolute path to the compiled PDF.
        """
        return self.renderer.recompile_tex(tex_filename)

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
                    print(
                        f"  ↻ highlighting attempt {attempt + 1} failed "
                        f"({type(e).__name__}: {e}; {diag}) — retrying",
                        flush=True,
                    )
                    continue
                print(
                    f"  ⚠ highlighting failed ({type(e).__name__}: {e}; {diag}) — "
                    "continuing with the un-highlighted CV",
                    flush=True,
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
                print(
                    f"  ✓ Output validated against TailoredCVData "
                    f"(validation attempt {val_attempt + 1})",
                    flush=True,
                )
                return cv_data
            except (ValueError, ValidationError) as e:
                print(
                    f"  ✗ Validation failed (attempt {val_attempt + 1}): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
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

        raise RuntimeError(
            f"Could not obtain valid TailoredCVData after "
            f"{self.max_validation_attempts} validation attempts."
        )

    def _review_cv_data(self, cv_data: TailoredCVData, attempt: int) -> ReviewResult:
        """Review generated CV content against the master profile.

        Uses the same model as CV generation (``cv_model``) with the
        review system prompt; the master profile and the generated CV are the
        only inputs. ``attempt`` is the 0-based generation attempt this review
        belongs to: from the 2nd attempt onward (``attempt >= 1``) an extra
        instruction tells the reviewer to flag ONLY large issues (the previous
        review's violations are assumed to have been fixed), so small nits no
        longer keep rejecting the CV.
        On an unparsable/unschema-valid response the review is retried once
        (the error is fed back to the model); if it still fails after 2
        attempts a warning is printed and an ``OK`` (no violations) result is
        returned so the unvalidated CV proceeds through the pipeline.
        """
        review_request = (
            "Review the GENERATED CV below against the candidate MASTER "
            "PROFILE.\n"
        )
        """"
        if attempt >= 1:
            review_request += (
                "NOTE: this is a REVISED CV (review attempt "
                f"{attempt + 1}) — the issues flagged in the previous review "
                "were already corrected. ONLY flag LARGE issues that make the "
                "CV inaccurate or unusable (unsupported major roles, "
                "responsibilities or technologies, factual contradictions, "
                "garbled content). Do NOT reject the CV for minor wording, "
                "style or small improvements.\n"
            )
        """
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
                print(
                    f"  ✗ Review response invalid (retry {retry + 1}): "
                    f"{type(e).__name__}: {e}",
                    flush=True,
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
        print(
            "  ⚠ Content reviewer failed to return a valid review after 2 "
            "attempts — continuing with the unvalidated CV",
            flush=True,
        )
        return ReviewResult(status="OK", violations=[])

    def generate_cv(self) -> str:
        """Generate a tailored CV PDF for the job in ``self.job_dir``.

        Runs fully inside ``self.job_dir`` (one job = one isolated folder). The
        job description is read from ``JD.txt`` in the folder. First tries the
        cheap path: if the folder already contains a ``.tex``, it is recompiled
        and page-checked; a within-limit PDF is returned without any LLM call.
        Otherwise the full LLM pipeline runs (see
        :meth:`_generate_cv_with_retry`). Returns the absolute path to the
        generated PDF.
        """
        job_dir = self.job_dir
        job_dir.mkdir(parents=True, exist_ok=True)

        jd_file = job_dir / "JD.txt"
        if not jd_file.is_file():
            raise FileNotFoundError(f"JD.txt missing in {job_dir}")
        job_description = jd_file.read_text(encoding="utf-8")
        if not job_description.strip():
            raise ValueError(f"JD.txt is empty in {job_dir}")

 
        try:
            print("  ♻ recompiling existing tex", flush=True)
            pdf_path = self.renderer.recompile_tex()  # picks the first .tex
            if self.renderer.check_pdf_pages(pdf_path)["pages"] <= 2:
                print(f"  ✓ Reused existing tex. Final PDF: {pdf_path}", flush=True)
                return pdf_path
            print("  ⚠ recompiled PDF exceeds 2 pages — regenerating from scratch")
        except Exception as exc:
            print(f"  ⚠ tex reuse failed ({type(exc).__name__}: {exc}) — "
                    "regenerating from scratch", flush=True)
            for stale in job_dir.glob("*.tex"):
                stale.unlink(missing_ok=True)

        return self._generate_cv_with_retry(job_description)

    def _generate_cv_with_retry(self, job_description: str) -> str:
        """Full LLM-driven pipeline with condensing retries.

        Builds the prompt from the job description and the master profile,
        generates and schema-validates the :class:`TailoredCVData` (feeding
        validation errors back to the LLM), content-reviews it against the
        master profile (REVIEW violations trigger a regeneration), renders/compiles
        the CV through :class:`CVRenderer` and condenses it if it exceeds the
        2-page limit (up to ``max_attempts``). Keyword highlighting and the
        final highlighted render run once after the loop. Returns the absolute
        path to the generated PDF.
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

        final_pdf_path = None
        final_cv_data = None

        for attempt in range(self.max_attempts):
            print(f"\n--- attempt {attempt + 1} ---", flush=True)

            # Generate -> validate against TailoredCVData, feeding the
            # validation errors back to the LLM until the output is
            # schema-valid.
            cv_data = self._generate_valid_cv_data(messages)

            # Content review (same model as generation): REVIEW rejects the CV
            # and its violations are fed back for regeneration on the next
            # attempt; OK lets the CV proceed to rendering.
            print("\n--- content review ---", flush=True)
            review = self._review_cv_data(cv_data, attempt)
            if review.status == "REVIEW" and not review.violations:
                # The reviewer flagged REVIEW without any specifics — this
                # violates its own prompt and should be rare. There is
                # nothing actionable to feed back to the generator, so treat
                # it as a pass rather than regenerating against a made-up
                # instruction.
                print(
                    "  ⚠ Reviewer returned REVIEW with no violations — "
                    "treating as OK",
                    flush=True,
                )
                review.status = "OK"

            if review.status == "REVIEW":
                # Print the violations the reviewer requested so they are
                # visible in the log (they are also fed back to the generator).
                print(
                    f"  ✗ Review rejected CV ({len(review.violations)} "
                    "violation(s)) — regenerating",
                    flush=True,
                )
                for violation in review.violations:
                    print(f"      - {violation}", flush=True)
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
            print("  ✓ Review OK", flush=True)

            print(f"\n--- render {attempt + 1} ---", flush=True)
            pdf_path = self.renderer.generate_cv_pdf(cv_data)  # compile the PDF
            final_pdf_path = pdf_path
            final_cv_data = cv_data

            info = self.renderer.check_pdf_pages(pdf_path)     # real page count
            print(f"  Page check: {info['pages']} pages -> {info['description']}")

            if info["pages"] <= 2:
                print(f"\n✓ Length OK: {pdf_path}")
                break

            print("PDF too long — condensing and retrying...")
            messages.append(
                {
                    "role": "user",
                    "content": info["description"]
                }
            )

        if final_cv_data is None:
            raise RuntimeError("No CV was generated (max_attempts must be >= 1).")

        # Keyword highlighting with the separate fast model — run ONCE, after
        # the loop, on the final (within page-limit) CV data (no retry loop —
        # the data is already schema-valid).
        print("\n--- keyword highlighting ---", flush=True)
        final_cv_data = self._highlight_keywords(final_cv_data, job_description)

        (self.renderer.output_dir / "cv_data.json").write_text(
            final_cv_data.model_dump_json(indent=2)
        )

        # Re-render from the highlighted data to produce the final PDF.
        print("\n--- final render (highlighted) ---", flush=True)
        final_pdf_path = self.renderer.generate_cv_pdf(final_cv_data)
        print(f"\n✓ Final PDF: {final_pdf_path}")

        jd_save_path = Path(str(Path(final_pdf_path).with_suffix("")) + "_jd.txt")
        jd_save_path.write_text(job_description)
        return final_pdf_path
