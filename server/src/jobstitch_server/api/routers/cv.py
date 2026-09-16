"""Writing and rendering CVs.

``POST /v1/cv`` is the expensive one: minutes of model calls, several LaTeX
compiles, and a JSON document at the end. It deliberately does not return the
PDF — rendering is a separate, cheap, deterministic call, so a client can
re-render an edited document without paying for the writing again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from jobstitch_contracts import CV_DOCUMENT_VERSION, CVDocument, Envelope, RenderedCV

from ...bundle import CandidateInputs
from ...pipeline.cv_generator import CVGenerator
from ...pipeline.cv_renderer import CVRenderer
from ..deps import envelope_of, execute, get_state
from ..errors import BadPart, MissingPart, UnsupportedVersion
from ..multipart import bytes_part, json_part, text_part
import time

router = APIRouter()


@router.post("/cv", response_model=Envelope[CVDocument], response_model_exclude_none=True)
async def create_cv(
    request: Request,
    jd: Optional[UploadFile] = File(None, description="The job description, as a file"),
    jd_text: Optional[str] = Form(None, description="The job description, as a field"),
    candidate_profile: Optional[UploadFile] = File(
        None, description="candidate_profile.json — what the model tailors from"
    ),
    candidate_data: Optional[UploadFile] = File(
        None, description="candidate_data.json — copied to the CV, never sent to the model"
    ),
    sys_prompt_cv: Optional[UploadFile] = File(None, description="Override the CV prompt"),
    sys_prompt_highlight: Optional[UploadFile] = File(
        None, description="Override the highlighting prompt"
    ),
    sys_review_prompt: Optional[UploadFile] = File(
        None, description="Override the review prompt"
    ),
    template: Optional[UploadFile] = File(
        None, description="Override resume3.tex.jinja (also used for the page check)"
    ),
    signature: Optional[UploadFile] = File(None, description="candidate_signature.png"),
    temperature: Optional[float] = Form(None, description="Sampling temperature override"),
    max_attempts: Optional[int] = Form(None, description="Condense rounds before giving up"),
) -> dict:
    """Tailor a CV for one job description and return it as a document."""
    state = get_state(request)
    limit = state.settings.max_part_bytes

    job_description = await text_part(jd, jd_text, name="jd", max_bytes=limit, required=True)
    profile = await json_part(
        candidate_profile, name="candidate_profile", max_bytes=limit, required=True
    )
    data = await json_part(candidate_data, name="candidate_data", max_bytes=limit, required=True)

    bundle = state.bundle.with_overrides(
        sys_prompt_cv=await text_part(sys_prompt_cv, name="sys_prompt_cv", max_bytes=limit),
        sys_prompt_highlight=await text_part(
            sys_prompt_highlight, name="sys_prompt_highlight", max_bytes=limit
        ),
        sys_review_prompt=await text_part(
            sys_review_prompt, name="sys_review_prompt", max_bytes=limit
        ),
        template_source=await text_part(template, name="template", max_bytes=limit),
        signature=await bytes_part(signature, name="signature", max_bytes=limit),
    )
    candidate = CandidateInputs(profile=profile, data=data)

    cv_model = state.models["cv"]
    if temperature is not None:
        cv_model = cv_model.with_(temperature=temperature)

    def job(work: Path) -> CVDocument:
        return CVGenerator(
            cv_model=cv_model,
            highlight_model=state.models.get("highlight"),
            bundle=bundle,
            candidate=candidate,
            work_dir=work,
            max_attempts=max_attempts or state.settings.max_attempts,
            deadline=time.monotonic() + state.settings.request_budget_seconds,
            latex_timeout=state.settings.latex_timeout,
        ).generate(job_description)

    return envelope_of(await execute(state, job, work_dir=True))


@router.post("/cv/render", response_model=Envelope[RenderedCV], response_model_exclude_none=True)
async def render_cv(
    request: Request,
    document: Optional[UploadFile] = File(None, description="A CVDocument, as JSON"),
    document_json: Optional[str] = Form(None, description="A CVDocument, as a JSON field"),
    tex: Optional[UploadFile] = File(None, description="Ready LaTeX source, compiled as-is"),
    template: Optional[UploadFile] = File(None, description="Override resume3.tex.jinja"),
    signature: Optional[UploadFile] = File(None, description="candidate_signature.png"),
) -> dict:
    """Compile a document (or a hand-edited ``.tex``) into a PDF."""
    state = get_state(request)
    limit = state.settings.max_part_bytes

    tex_source = await text_part(tex, name="tex", max_bytes=limit)
    doc_data = await json_part(document, document_json, name="document", max_bytes=limit)
    if tex_source is None and doc_data is None:
        raise MissingPart("send either 'document' (a CVDocument) or 'tex' (LaTeX source)")
    if tex_source is not None and doc_data is not None:
        raise BadPart("send 'document' or 'tex', not both")

    bundle = state.bundle.with_overrides(
        template_source=await text_part(template, name="template", max_bytes=limit),
        signature=await bytes_part(signature, name="signature", max_bytes=limit),
    )

    doc: Optional[CVDocument] = None
    if doc_data is not None:
        doc = CVDocument.model_validate(doc_data)
        if doc.version != CV_DOCUMENT_VERSION:
            raise UnsupportedVersion(
                f"document version {doc.version} is not supported "
                f"(this server speaks version {CV_DOCUMENT_VERSION})"
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
            if doc is None
            else renderer.render_document(doc)
        )
        return RenderedCV.from_bytes(
            tex=result.tex, pdf=result.pdf, pages=result.pages, advice=result.advice
        )

    return envelope_of(await execute(state, job, work_dir=True))
