"""CV renderer module — :class:`CVRenderer` and the structured CV data models.

Renders a tailored LaTeX CV from structured data (:class:`TailoredCVData`) and
compiles it to PDF (:meth:`CVRenderer.generate_cv_pdf`), recompiles an existing
``.tex`` file to PDF (:meth:`CVRenderer.recompile_tex`) and verifies the
compiled PDF page count (:meth:`CVRenderer.check_pdf_pages`).

The Pydantic models :class:`WorkExperienceItem` and :class:`TailoredCVData`
describe the structured data the CV is rendered from. They are defined here,
next to the :class:`CVRenderer` that consumes them, so the module is
self-contained — :mod:`jobstitch.cv_creation` imports them from this
module and no circular dependency arises.
"""

import json
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .paths import RESOURCES_DIR, resource_path, strip_example

# ---------------------------------------------------------------------------
# Pydantic models for the CV -> must match the template.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# CVRenderer class.
# ---------------------------------------------------------------------------
class CVRenderer:
    """Renders a tailored LaTeX CV from structured data and compiles it to PDF.

    Parameters
    ----------
    resources_dir:
        Directory containing the Jinja ``.tex`` templates and PNG assets.
        Defaults to the repo's ``resources/`` folder.
    output_dir:
        Directory where rendered ``.tex`` files and compiled PDFs are written.
        Defaults to the current working directory.
    resume_file:
        Name of the Jinja template file to render (default ``resume3.tex.jinja``).
    filename_prefix:
        Leading fragment of the generated ``.tex``/``.pdf`` filenames; the job
        title is appended to it.
    """

    def __init__(
        self,
        resources_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        resume_file: str = "resume3.tex.jinja",
        filename_prefix: str = "cv",
    ) -> None:
        self.resources_dir = (
            Path(resources_dir) if resources_dir is not None else RESOURCES_DIR
        )
        self.output_dir = Path(output_dir) if output_dir is not None else Path(".")
        self.resume_file = resume_file
        self.filename_prefix = filename_prefix

        self.jinja_env = Environment(
            loader=FileSystemLoader(str(self.resources_dir)),
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

    def _copy_image_assets(self) -> None:
        """Copy PNG assets (e.g. the signature image) from ``resources_dir``
        into the output folder.

        The LaTeX template embeds them with ``\\includegraphics``, so they must
        live next to the compiled ``.tex`` for relative paths to resolve. A
        shipped ``*.example.png`` is copied under its real name, so the
        template's ``\\includegraphics{candidate_signature.png}`` resolves
        whether or not you have supplied your own signature yet.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        copied: set[str] = set()
        # Real files first, so an example is only used when its real
        # counterpart is absent.
        assets = sorted(
            self.resources_dir.glob("*.png"),
            key=lambda p: (".example." in p.name, p.name),
        )
        for src in assets:
            name = strip_example(src.name)
            if name in copied:
                continue  # a real file always wins over its example sibling
            copied.add(name)
            dst = self.output_dir / name
            if not dst.exists():
                shutil.copy2(src, dst)

    # --- public API ---------------------------------------------------------
    def generate_cv_pdf(self, cv_data: TailoredCVData) -> str:
        """Render the LaTeX template and compile it into a PDF.

        Accepts a single :class:`TailoredCVData` pydantic object containing all
        required candidate profile fields and returns the absolute path to the
        generated PDF file.
        """
        self._copy_image_assets()

        # Convert Pydantic object into a dictionary for Jinja2 rendering,
        # merged with the static candidate data (name, email, etc.) that
        # doesn't change between tailored CVs.
        candidate_data = json.loads(
            resource_path("candidate_data.json", self.resources_dir).read_text()
        )
        raw_data = {
            **candidate_data,
            "generation_date": date.today().strftime("%Y/%m/%d"),
            **cv_data.model_dump(),
        }

        clean_title = re.sub(r"\W+", "_", raw_data["job_title"])

        data = self._sanitize_obj(raw_data)

        # Render template with Jinja2.
        template = self.jinja_env.get_template(self.resume_file)
        rendered_tex = template.render(**data)

        # Shortened filename: the company name is already in the job folder
        # name (YY-MM-DD_Company_JobTitle), so only the candidate prefix and
        # the job title are used here.
        output_filename = f"{self.filename_prefix}_{clean_title}"

        # Write the rendered .tex next to the image assets, then compile it there
        # so relative \includegraphics paths resolve correctly.
        tex_path = self.output_dir / f"{output_filename}.tex"
        tex_path.write_text(rendered_tex)

        pdf_path = self._compile_tex(tex_path)
        print(f"  [Tool Executed] Compiled PDF at: {pdf_path}")
        return pdf_path

    def recompile_tex(self, tex_filename: Optional[str] = None) -> str:
        """Recompile an existing ``.tex`` file to PDF without re-rendering.

        Useful after manually editing a generated ``.tex`` file. If
        ``tex_filename`` is omitted, the first ``.tex`` file found in
        ``output_dir`` is recompiled. Returns the absolute path to the
        compiled PDF.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        if tex_filename is None:
            tex_files = sorted(self.output_dir.glob("*.tex"))
            if not tex_files:
                raise FileNotFoundError(
                    f"No tex file found in {self.output_dir} to recompile."
                )
            tex_path = tex_files[0]
        else:
            tex_path = self.output_dir / tex_filename
            if not tex_path.exists():
                raise FileNotFoundError(f"Tex file not found: {tex_path}")

        # Make sure image assets are next to the .tex so relative
        # \includegraphics paths resolve.
        self._copy_image_assets()

        pdf_path = self._compile_tex(tex_path)
        print(f"  [Tool Executed] Recompiled PDF at: {pdf_path}")
        return pdf_path

    def _compile_tex(self, tex_path: Path) -> str:
        cmd = ["pdflatex", "-interaction=nonstopmode", tex_path.name]
        result = subprocess.run(
            cmd, cwd=str(self.output_dir), capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pdflatex compilation failed for {tex_path.name}.\n"
                f"--- LaTeX log tail ---\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
            )

        pdf_path = (self.output_dir / f"{tex_path.stem}.pdf").resolve()

        # Clean up auxiliary files produced by the compile.
        for leftover in self.output_dir.glob(f"{tex_path.stem}.*"):
            if leftover.suffix.lower() not in {".pdf", ".png", ".tex"}:
                leftover.unlink(missing_ok=True)

        return str(pdf_path)

    def check_pdf_pages(self, pdf_path: str) -> Dict[str, Any]:
        """Check the total page count of a PDF file given its absolute path.

        Returns a dictionary containing the page count and a status
        description. If the PDF exceeds 2 pages, also reports how many text
        lines overflow onto page 3 (and any subsequent pages).
        """
        try:
            reader = PdfReader(pdf_path)
            pages = len(reader.pages)
        except Exception as e:
            raise RuntimeError(f"Could not read PDF {pdf_path}: {e}")

        result: Dict[str, Any] = {"pages": pages}

        if pages <= 2:
            desc = "length OK"
        else:
            # Count non-empty text lines on each overflowing page (page 3 onward).
            overflow_lines: Dict[str, int] = {}
            for idx in range(2, pages):
                text = reader.pages[idx].extract_text() or ""
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                overflow_lines[f"page_{idx + 1}_lines"] = len(lines)
            result.update(overflow_lines)

            total_overflow = sum(overflow_lines.values())
            desc=""
            if(total_overflow <= 2):
                desc = (
                    f"PDF is rejected because it is too long. PDF is {pages} pages, the mandatory 2 page limit was exceeded. Slight overflow detected."
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

        print(
            f"  [Tool Executed] Checked '{Path(pdf_path).name}': "
            f"{pages} pages -> {desc}"
        )
        return result
