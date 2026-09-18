"""``jobstitch submit-raw`` — a job description in, files in this folder out.

Nothing but the CV: no detection, no analysis, no question before spending,
no cover letter, no spreadsheet and no folder structure. What you get is what
you asked for with ``-o``, written where you are standing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

from jobstitch_contracts import RenderedCV

from ..api import HttpApi, JobstitchError
from ..config import Config
from ..cvjob import write_cv
from ..ui import fail

#: What ``-o`` understands. The suffix is the whole instruction.
SUFFIXES = (".pdf", ".json", ".tex")


def run(config: Config, jd_file: Path, outputs: List[Path], *,
        resume: Optional[str] = None) -> int:
    jd_file = Path(jd_file).expanduser()
    if not jd_file.is_file():
        fail(f"no such file: {jd_file}")
    for target in outputs:
        if target.suffix not in SUFFIXES:
            fail(f"-o takes a {', '.join(SUFFIXES)} file, not {target.name}")

    api = HttpApi(config.server_url, token=config.token, verbose=config.verbose)
    try:
        _, rendered = write_cv(
            api, config, jd_file.read_text(encoding="utf-8"), say=_say, resume=resume
        )
    except JobstitchError as exc:
        where = f" (request {exc.request_id})" if exc.request_id else ""
        fail(f"{exc}{where}")

    # A name you chose is a name you meant, so it is overwritten without
    # asking. The default name is not, so it counts up instead.
    for target in outputs or [_free_pdf(jd_file.stem)]:
        _write(target, rendered)
        print(f"✅ {target}", flush=True)
    return 0


def _write(target: Path, rendered: RenderedCV) -> None:
    if target.suffix == ".pdf":
        target.write_bytes(rendered.pdf_bytes())
    elif target.suffix == ".tex":
        target.write_text(rendered.tex, encoding="utf-8")
    else:
        target.write_text(
            json.dumps(rendered.document, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _free_pdf(stem: str) -> Path:
    """``posting.pdf``, then ``posting_1.pdf``, ``posting_2.pdf``..."""
    target = Path(f"{stem}.pdf")
    n = 0
    while target.exists():
        n += 1
        target = Path(f"{stem}_{n}.pdf")
    return target


def _say(message: str) -> None:
    print(message, flush=True)
