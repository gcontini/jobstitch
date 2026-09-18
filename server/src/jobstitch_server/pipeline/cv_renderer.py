"""Rendering: structured CV data in, a compiled PDF out.

Everything happens in one scratch directory handed in by the caller and
thrown away afterwards, and every input arrives in memory — the template, the
image assets, the data. The renderer reads no configuration and touches no
path of its own, which is what makes it safe to run per request on a shared
server.

``pdflatex`` is treated as hostile input territory: the template and, in the
``render`` endpoint, the whole ``.tex`` come from the client, so the compile
runs with shell escape off, file reads and writes confined to the scratch
directory, no stdin to block on, and a timeout.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from jinja2 import DictLoader, Environment, TemplateError
from pypdf import PdfReader

from ..observability import LOGGER_ROOT
from .errors import LatexCompileError, LatexTimeoutError

logger = logging.getLogger(f"{LOGGER_ROOT}.render")

#: Default template name; the source itself always comes from the caller.
TEMPLATE_NAME = "resume.tex.jinja"

#: The CV must fit this many pages; over it, the generator condenses and retries.
PAGE_LIMIT = 2

DEFAULT_LATEX_TIMEOUT = 120.0


@dataclass(frozen=True)
class RenderResult:
    """One compiled CV: the sources, the bytes, and how long it came out."""

    tex: str
    pdf: bytes
    pages: int
    advice: str  # "length OK", or what to cut — fed back to the model verbatim


class CVRenderer:
    """Render a LaTeX CV from in-memory inputs and compile it in ``work_dir``.

    Parameters
    ----------
    template_source:
        The Jinja template text. Client-supplied when the request carries one,
        otherwise the server's default.
    assets:
        Images the template includes by relative name — whatever the client
        sent, under the file names it sent them with. Written next to the
        ``.tex`` because ``\\includegraphics`` paths are resolved relative to it.
    work_dir:
        Scratch directory for this render. The caller creates and removes it.
    """

    def __init__(
        self,
        *,
        template_source: str,
        template_name: str = TEMPLATE_NAME,
        assets: Optional[Mapping[str, bytes]] = None,
        work_dir: Path,
        latex_timeout: float = DEFAULT_LATEX_TIMEOUT,
        page_limit: int = PAGE_LIMIT,
    ) -> None:
        self.template_name = template_name
        self.assets = dict(assets or {})
        self.work_dir = Path(work_dir)
        self.latex_timeout = latex_timeout
        self.page_limit = page_limit

        # A fresh environment per renderer: the template is per-request input,
        # so a shared or cached one would serve someone else's template.
        self.jinja_env = Environment(
            loader=DictLoader({template_name: template_source}),
            block_start_string=r"\BLOCK{",
            block_end_string="}",
            variable_start_string=r"\VAR{",
            variable_end_string="}",
            comment_start_string=r"\#{",
            comment_end_string="}",
            line_statement_prefix="%%",
            line_comment_prefix="%#",
            autoescape=False,
            trim_blocks=True,
        )

    # --- helpers -----------------------------------------------------------
    def _sanitize_latex(self, text: str) -> str:
        """Escapes LaTeX special characters in output strings.

        Also converts ``**bold**`` markers into LaTeX ``\\textbf{...}``.

        Intentional empty groups ``{}`` (used e.g. to guard en-dashes as
        ``{}--``) are preserved so they are not turned into literal
        backslash-brace pairs.
        """
        if not isinstance(text, str):
            return text

        chars = {
            "\\": r"\textbackslash{}",
            "{": r"\{",
            "}": r"\}",
            "&": r"\&",
            "%": r"\%",
            "$": r"\$",
            "#": r"\#",
            "_": r"\_",
            "]": r"\]",
            "~": r"\textasciitilde{}",
            "^": r"\textasciicircum{}",
        }
        # Longest keys first so multi-character tokens are matched before
        # single chars.
        regex = re.compile(
            "|".join(re.escape(k) for k in sorted(chars, key=len, reverse=True))
        )

        def _escape(part: str) -> str:
            escaped = regex.sub(lambda match: chars[match.group(0)], part)
            # Restore intentional empty groups, e.g. "{}--" en-dash guards.
            return escaped.replace(r"\{\}", "{}")

        # Split on '**...**' so bold spans are rendered as \textbf{...} while
        # the rest is escaped normally. Non-greedy / non-star inner match keeps
        # spans separate.
        parts = re.split(r"(\*\*[^*]+\*\*)", text)
        out = []
        for part in parts:
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                out.append(r"\textbf{" + _escape(part[2:-2]) + "}")
            else:
                # Within non-bold spans, render '*...*' as \textit{...} (italic).
                # Uses the same splitting pattern as bold, but for single
                # asterisks.
                italic_parts = re.split(r"(\*[^*]+\*)", part)
                for ip in italic_parts:
                    if ip.startswith("*") and ip.endswith("*") and len(ip) > 2:
                        out.append(r"\textit{" + _escape(ip[1:-1]) + "}")
                    else:
                        out.append(_escape(ip))
        return "".join(out)

    def _sanitize_obj(self, obj):
        """Recursive LaTeX character escaping over nested data."""
        if isinstance(obj, str):
            return self._sanitize_latex(obj)
        elif isinstance(obj, list):
            return [self._sanitize_obj(i) for i in obj]
        elif isinstance(obj, dict):
            return {k: self._sanitize_obj(v) for k, v in obj.items()}
        return obj

    def _write_assets(self) -> None:
        """Drop the image assets next to the ``.tex`` about to be compiled."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for name, blob in self.assets.items():
            # Names come from a multipart part; keep them inside work_dir.
            (self.work_dir / Path(name).name).write_bytes(blob)

    # --- public API ---------------------------------------------------------
    def render_tex(self, context: Mapping[str, Any]) -> str:
        """Render the template into LaTeX source. Pure: no files, no compile."""
        data = self._sanitize_obj(dict(context))
        try:
            return self.jinja_env.get_template(self.template_name).render(**data)
        except TemplateError as exc:
            raise LatexCompileError(
                f"template error: {type(exc).__name__}: {exc}", stage="render"
            ) from exc

    def render_document(
        self, document: Mapping[str, Any], *, stem: str = "cv"
    ) -> RenderResult:
        """Render one flat namespace of CV data and compile it.

        The date is added here rather than stored in the document, so a
        re-render of yesterday's file is dated today.
        """
        context = {"generation_date": date.today().strftime("%Y/%m/%d"), **document}
        return self.compile_tex(self.render_tex(context), stem=stem)

    def compile_tex(self, tex: str, *, stem: str = "cv") -> RenderResult:
        """Compile LaTeX source to a PDF and report its length."""
        self._write_assets()
        tex_path = self.work_dir / f"{stem}.tex"
        tex_path.write_text(tex, encoding="utf-8")

        pdf_path = self._run_pdflatex(tex_path)
        info = check_pdf_pages(pdf_path, limit=self.page_limit)
        return RenderResult(
            tex=tex,
            pdf=pdf_path.read_bytes(),
            pages=info["pages"],
            advice=info["description"],
        )

    # --- pdflatex -----------------------------------------------------------
    def _run_pdflatex(self, tex_path: Path) -> Path:
        """One pdflatex pass, sandboxed.

        The template has no ``\\ref``/``\\label``/``\\tableofcontents``, so a
        second pass would change nothing — do not "fix" this into latexmk.
        """
        cmd = [
            "pdflatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-no-shell-escape",
            "-file-line-error",
            tex_path.name,
        ]
        env = {
            **os.environ,
            # Keep the font cache and any dotfile inside the scratch dir, so
            # concurrent compiles cannot race each other.
            "HOME": str(self.work_dir),
            "TEXMFVAR": str(self.work_dir / "texmf-var"),
            "TEXMFHOME": str(self.work_dir / "texmf-home"),
            # Refuse \input{/etc/passwd} and writes outside the scratch dir.
            "openin_any": "p",
            "openout_any": "p",
            "shell_escape": "f",
            "max_print_line": "1000",
        }
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.work_dir),
                capture_output=True,
                text=True,
                timeout=self.latex_timeout,
                # nonstopmode still prompts "Enter file name:" for a missing
                # include; with an inherited stdin that is a request that
                # never returns.
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise LatexTimeoutError(
                f"pdflatex exceeded {self.latex_timeout:.0f}s", stage="compile"
            ) from exc
        except FileNotFoundError as exc:
            raise LatexCompileError(
                "pdflatex is not installed on this server", stage="compile"
            ) from exc

        if result.returncode != 0:
            # The tail goes to the log, not into the error body: it is
            # kilobytes of TeX chatter, wanted only when someone is actually
            # debugging. GET /logs/{request_id} is where they ask for it.
            logger.error("  pdflatex failed for %s:\n%s", tex_path.name,
                         self._scrub(result.stdout[-4000:] + result.stderr[-2000:]))
            raise LatexCompileError(
                f"pdflatex failed for {tex_path.name}",
                stage="compile",
                detail={"file": tex_path.name},
            )

        pdf_path = self.work_dir / f"{tex_path.stem}.pdf"
        if not pdf_path.is_file():
            raise LatexCompileError(
                "pdflatex reported success but produced no PDF", stage="compile"
            )
        logger.info("  [Tool Executed] Compiled PDF: %s (%d bytes)",
                    pdf_path.name, pdf_path.stat().st_size)
        return pdf_path

    def _scrub(self, text: str) -> str:
        """Keep the scratch path out of anything the client gets back."""
        return text.replace(str(self.work_dir), "<work>")


def check_pdf_pages(pdf_path: Path, limit: int = PAGE_LIMIT) -> Dict[str, Any]:
    """Page count plus, when it is too long, what to cut.

    The ``description`` is fed back to the model verbatim as the next user
    turn, so its wording is part of the prompt.
    """
    try:
        reader = PdfReader(str(pdf_path))
        pages = len(reader.pages)
    except Exception as e:
        raise LatexCompileError(f"Could not read PDF {pdf_path.name}: {e}", stage="compile")

    result: Dict[str, Any] = {"pages": pages}

    if pages <= limit:
        desc = "length OK"
    else:
        # Count non-empty text lines on each overflowing page (past the limit).
        overflow_lines: Dict[str, int] = {}
        for idx in range(limit, pages):
            text = reader.pages[idx].extract_text() or ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            overflow_lines[f"page_{idx + 1}_lines"] = len(lines)
        result.update(overflow_lines)

        total_overflow = sum(overflow_lines.values())
        desc=""
        if(total_overflow <= 2):
            desc = (
                f"PDF is rejected because it is too long. PDF is {pages} pages, the mandatory {limit} page limit was exceeded. Slight overflow detected."
                f"Excess {total_overflow} lines across all the pages. "
                f"Condense summary, REMOVE 1 bullet point. In total be sure to remove more than {(total_overflow * 90)} characters."
            )
        elif(total_overflow<10): 
            desc = (
                f"PDF is rejected because it is too long. PDF is {pages} pages, mandatory page limit exceeded."
                f"Condense summary, REMOVE {int((total_overflow+1)/2)} bullet points." 
                f"In total be sure to remove more than {(total_overflow * 90)} characters."
            )
        else:
            desc = (
                f"PDF is rejected because it is EXTREMELY long. PDF is {pages} pages, page limit exceeded. SERIOUS overflow detected. An heavy rework of the content is needed."
                f"Total {total_overflow} overflow lines across pages. "
                "Condense summary, remove one work experience completely." 
                "Aim for 4 work experiences with 18 bullet points in total over the whole CV.")

    result["description"] = desc
    logger.info("  [Tool Executed] Checked '%s': %d pages -> %s", pdf_path.name, pages, desc)
    return result


__all__ = ["CVRenderer", "RenderResult", "check_pdf_pages", "TEMPLATE_NAME", "PAGE_LIMIT"]
