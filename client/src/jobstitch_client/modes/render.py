"""``jobstitch render`` — a document or a .tex in, a PDF beside it out.

The only mode that needs neither your profile nor a job description: it is
the one to reach for after hand-editing a ``cv_*.json`` or a ``cv_*.tex``.
"""

from __future__ import annotations

from pathlib import Path

from jobstitch_contracts import CVDocument

from ..api import HttpApi, JobstitchError, read_bytes, read_text
from ..config import Config
from ..joblog import format_entries
from ..ui import fail


def run(config: Config, source: Path, *, output: Path | None = None) -> int:
    source = Path(source).expanduser()
    if not source.is_file():
        fail(f"no such file: {source}")

    api = HttpApi(config.server_url, token=config.token, timeout=config.timeout,
                  verbose=config.verbose)
    template = read_text(config.path("resume3.tex.jinja"))
    signature = read_bytes(config.path("candidate_signature.png"))

    try:
        if source.suffix == ".tex":
            envelope = api.render(tex=source.read_text(encoding="utf-8"),
                                  signature=signature)
        elif source.suffix == ".json":
            document = CVDocument.model_validate_json(source.read_text(encoding="utf-8"))
            envelope = api.render(document=document, template=template, signature=signature)
        else:
            fail(f"render takes a .tex or a .json file, not {source.suffix or 'a directory'}")
    except JobstitchError as exc:
        fail(f"{exc}{_server_log(api, exc.request_id)}")
    except ValueError as exc:
        fail(f"{source.name} is not a valid CV document: {exc}")

    rendered = envelope.data
    target = Path(output) if output else source.with_suffix(".pdf")
    target.write_bytes(rendered.pdf_bytes())
    print(f"✅ {target} ({rendered.pages} page(s))", flush=True)
    return 0


def _server_log(api: HttpApi, request_id: str | None) -> str:
    """A failed render is nearly always a LaTeX error; the log holds it."""
    if not request_id:
        return ""
    try:
        return "\n" + format_entries(request_id, api.logs(request_id).data.entries)
    except JobstitchError:
        return f"\n(the server log for {request_id} could not be fetched)"
