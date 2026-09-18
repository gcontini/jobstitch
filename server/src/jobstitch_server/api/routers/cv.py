"""Writing and rendering CVs.

``POST /v1/cv`` is the expensive one — minutes of model calls and several
LaTeX compiles — so it does not wait: it accepts the job, answers with a
request id and writes the CV in the background. ``GET /v1/cv/{id}/status``
says where it has got to, and ``GET /v1/cv/{id}`` hands back the finished
document, its LaTeX and its PDF together.

It takes ``candidate_data`` as well as ``candidate_profile``. No model is
shown it — only the template is — but the loop renders the CV to count its
pages, and a page count taken without the contact details is not the page
count of the CV you will send.

``POST /v1/cv/render`` is the cheap, deterministic half: a document you have
edited, or a ``.tex`` you have edited, compiled again for nothing.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, File, Form, Request, UploadFile
from jobstitch_contracts import CVStatus, Envelope, RenderedCV

from ...bundle import CandidateInputs
from ...jobstore import RESULT, STATUS, JobDir
from ...observability import current_run
from ...pipeline.cv_generator import CVGenerator
from ...pipeline.cv_renderer import CVRenderer
from .. import jobs
from ..deps import claim_slot, envelope_for, envelope_of, execute, get_state, save_log
from ..errors import BadPart, JobNotReady, MissingPart, UnknownRequest, failure_response
from ..multipart import images_part, json_part, text_part

router = APIRouter()


@router.post("/cv", status_code=202, response_model=Envelope[None],
             response_model_exclude_none=True)
async def create_cv(
    request: Request,
    jd: Optional[UploadFile] = File(None, description="The job description, as a file"),
    jd_text: Optional[str] = Form(None, description="The job description, as a field"),
    candidate_profile: Optional[UploadFile] = File(
        None, description="candidate_profile.json — what the model tailors from"
    ),
    candidate_data: Optional[UploadFile] = File(
        None, description="candidate_data.json — what the template prints"
    ),
    sys_prompt_cv: Optional[UploadFile] = File(None, description="Override the CV prompt"),
    sys_prompt_highlight: Optional[UploadFile] = File(
        None, description="Override the highlighting prompt"
    ),
    sys_prompt_review: Optional[UploadFile] = File(
        None, description="Override the review prompt"
    ),
    template: Optional[UploadFile] = File(
        None, description="Override resume.tex.jinja (also used for the page check)"
    ),
    images: List[UploadFile] = File(
        [], description="Images the template includes, each by its own file name"
    ),
    temperature: Optional[float] = Form(None, description="Sampling temperature override"),
    pages: Optional[int] = Form(None, description="Page limit the CV must fit (default: 2)"),
) -> dict:
    """Accept one CV job. The id in the envelope is the handle for the rest."""
    state = get_state(request)
    limit = state.settings.max_part_bytes
    if pages is not None and pages < 1:
        raise BadPart("pages must be at least 1")

    job_description = await text_part(jd, jd_text, name="jd", max_bytes=limit, required=True)
    profile = await json_part(
        candidate_profile, name="candidate_profile", max_bytes=limit, required=True
    )
    data = await json_part(
        candidate_data, name="candidate_data", max_bytes=limit, required=True
    )

    bundle = state.bundle.with_overrides(
        sys_prompt_cv=await text_part(sys_prompt_cv, name="sys_prompt_cv", max_bytes=limit),
        sys_prompt_highlight=await text_part(
            sys_prompt_highlight, name="sys_prompt_highlight", max_bytes=limit
        ),
        sys_prompt_review=await text_part(
            sys_prompt_review, name="sys_prompt_review", max_bytes=limit
        ),
        template_source=await text_part(template, name="template", max_bytes=limit),
        images=await images_part(images, max_bytes=limit),
    )
    candidate = CandidateInputs(profile=profile, data=data)

    cv_model = state.models["cv"]
    if temperature is not None:
        cv_model = cv_model.with_(temperature=temperature)

    def build(work: Path, progress) -> Tuple[Dict[str, Any], str, bytes, str]:
        return CVGenerator(
            cv_model=cv_model,
            highlight_model=state.models.get("highlight"),
            bundle=bundle,
            candidate=candidate,
            work_dir=work,
            max_attempts=state.settings.max_attempts,
            deadline=time.monotonic() + state.settings.request_budget_seconds,
            latex_timeout=state.settings.latex_timeout,
            page_limit=pages,
            progress=progress,
        ).generate(job_description)

    run = current_run()
    # Order matters: the slot, then the directory with a status already in it,
    # then the thread. A poll must never arrive before the first status.
    claim_slot(state)
    try:
        job = state.jobs.create(run.request_id)
    except FileExistsError:
        state.job_slots.release()
        raise BadPart(f"request id {run.request_id} is already in use")
    job.write(STATUS, {"state": "running", "status": "generate", "detail": ""})
    save_log(job, run)
    jobs.start(state, job, run, build)
    return {"request_id": run.request_id, "ok": True}


@router.get("/cv/{request_id}/status", response_model=Envelope[CVStatus],
            response_model_exclude_none=True)
async def cv_status(request: Request, request_id: str) -> Any:
    """Where the job is now. Poll it; there is no history to miss."""
    job, record = _record(request, request_id)
    if record["state"] == "failed":
        return failure_response(job.request_id, record["http_status"], record["error"])
    return envelope_for(job.request_id, CVStatus(status=record["status"],
                                                 detail=record["detail"]))


@router.get("/cv/{request_id}", response_model=Envelope[RenderedCV],
            response_model_exclude_none=True)
async def cv_result(request: Request, request_id: str) -> Any:
    """The finished CV: the merged document, its LaTeX and its PDF."""
    job, record = _record(request, request_id)
    if record["state"] == "failed":
        return failure_response(job.request_id, record["http_status"], record["error"])
    result = job.read(RESULT)
    if record["state"] != "done" or result is None:
        # Answered rather than raised, so the id in the body is the job's and
        # not this poll's — a client following one must not be sent its own.
        return failure_response(
            job.request_id, JobNotReady.status,
            f"{JobNotReady.kind}: job {job.request_id} is still running "
            f"({record['status']}); poll /status until it says END",
        )
    return envelope_for(job.request_id, result)


def _record(request: Request, request_id: str) -> Tuple[JobDir, Dict[str, Any]]:
    job = get_state(request).jobs.open(request_id)
    record = job.read(STATUS) if job is not None else None
    if job is None or record is None:
        raise UnknownRequest(
            f"no CV job {request_id} — it is too old, it never existed, or it "
            "was served by another instance"
        )
    return job, record


@router.post("/cv/render", response_model=Envelope[RenderedCV],
             response_model_exclude_none=True)
async def render_cv(
    request: Request,
    document: Optional[UploadFile] = File(None, description="A CV document, as JSON"),
    tex: Optional[UploadFile] = File(None, description="Ready LaTeX source, compiled as-is"),
    template: Optional[UploadFile] = File(None, description="Override resume.tex.jinja"),
    images: List[UploadFile] = File(
        [], description="Images the template includes, each by its own file name"
    ),
) -> dict:
    """Compile a document (or a hand-edited ``.tex``) into a PDF."""
    state = get_state(request)
    limit = state.settings.max_part_bytes

    tex_source = await text_part(tex, name="tex", max_bytes=limit)
    doc_data = await json_part(document, name="document", max_bytes=limit)
    if tex_source is None and doc_data is None:
        raise MissingPart("send either 'document' (CV data as JSON) or 'tex' (LaTeX source)")
    if tex_source is not None and doc_data is not None:
        raise BadPart("send 'document' or 'tex', not both")

    bundle = state.bundle.with_overrides(
        template_source=await text_part(template, name="template", max_bytes=limit),
        images=await images_part(images, max_bytes=limit),
    )

    def job(work: Path) -> RenderedCV:
        renderer = CVRenderer(
            template_source=bundle.template_source,
            template_name=bundle.template_name,
            assets=bundle.assets,
            work_dir=work,
            latex_timeout=state.settings.latex_timeout,
        )
        result = (
            renderer.compile_tex(tex_source)
            if doc_data is None
            else renderer.render_document(doc_data)
        )
        # No document echoed back: whoever asked for this render already has it.
        return RenderedCV.from_bytes(tex=result.tex, pdf=result.pdf)

    return envelope_of(await execute(state, job, work_dir=True))
