"""LaTeX escaping, the golden render, the compile sandbox and the page check."""

from __future__ import annotations

import logging
import shutil
from datetime import date
from pathlib import Path

import pytest

from jobstitch_server.pipeline.cv_renderer import CVRenderer, check_pdf_pages
from jobstitch_server.pipeline.errors import LatexCompileError, LatexTimeoutError

from server_helpers import GOLDEN

needs_latex = pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")


@pytest.fixture
def renderer(bundle, tmp_path) -> CVRenderer:
    return CVRenderer(
        template_source=bundle.template_source,
        assets=bundle.assets,
        work_dir=tmp_path,
    )


def _context(document: dict) -> dict:
    """What render_document builds before it renders: the flat namespace plus
    the date, which is added at render time and not stored."""
    from datetime import date

    return {"generation_date": date.today().strftime("%Y/%m/%d"), **document}


def tex_renderer(source: str, tmp_path: Path, **kw) -> CVRenderer:
    return CVRenderer(template_source=source, work_dir=tmp_path, **kw)


# --- escaping ---------------------------------------------------------------
def test_escapes_latex_specials(renderer):
    out = renderer._sanitize_latex("R&D costs 50% of $X_1 #2")
    assert r"\&" in out and r"\%" in out and r"\$" in out
    assert r"\_" in out and r"\#" in out


def test_bold_and_italic_markers_become_latex(renderer):
    assert renderer._sanitize_latex("**Kubernetes** rollout") == r"\textbf{Kubernetes} rollout"
    assert renderer._sanitize_latex("*Terraform* modules") == r"\textit{Terraform} modules"


def test_bold_wins_over_italic_inside_the_same_string(renderer):
    assert renderer._sanitize_latex("**Go** and *Rust*") == r"\textbf{Go} and \textit{Rust}"


def test_intentional_empty_groups_survive(renderer):
    # "{}--" guards an en-dash in the template; it must not become \{\}--
    assert renderer._sanitize_latex("2024 {}-- 2026") == "2024 {}-- 2026"


def test_sanitize_obj_walks_nested_structures(renderer):
    out = renderer._sanitize_obj({"a": ["50% off", {"b": "**bold**"}]})
    assert out["a"][0] == r"50\% off"
    assert out["a"][1]["b"] == r"\textbf{bold}"


# --- the golden render ------------------------------------------------------
def test_render_matches_the_golden_tex(renderer, fixture_document):
    """The whole escaping + templating pipeline, pinned byte for byte.

    A regression here is otherwise invisible until someone reads a PDF, so
    this is the test that guards every refactor of the renderer. The one line
    that cannot be pinned is the date printed on the CV: nothing stamps the
    document any more, so it is always the day of the render.
    """
    expected = (GOLDEN / "cv_golden.tex").read_text().replace(
        "2026/01/15", date.today().strftime("%Y/%m/%d")
    )
    assert renderer.render_tex(_context(fixture_document)) == expected


def test_render_is_pure(renderer, fixture_document, tmp_path):
    renderer.render_tex(_context(fixture_document))
    assert list(tmp_path.iterdir()) == []


def test_empty_certifications_drop_the_whole_section(renderer, sample_document):
    """sample_document carries no certifications — the heading must go too."""
    tex = renderer.render_tex(_context(sample_document))
    assert "Professional Certifications" not in tex


# --- assets -----------------------------------------------------------------
def test_assets_land_next_to_the_tex(tmp_path):
    renderer = tex_renderer(r"\documentclass{article}\begin{document}x\end{document}",
                            tmp_path, assets={"candidate_signature.png": b"\x89PNG-mine"})
    renderer._write_assets()
    assert (tmp_path / "candidate_signature.png").read_bytes() == b"\x89PNG-mine"


def test_asset_names_cannot_escape_the_work_dir(tmp_path):
    renderer = tex_renderer("x", tmp_path, assets={"../../escaped.png": b"nope"})
    renderer._write_assets()
    assert (tmp_path / "escaped.png").exists()
    assert not (tmp_path.parent.parent / "escaped.png").exists()


# --- compiling --------------------------------------------------------------
@needs_latex
def test_renders_a_two_page_pdf_from_the_shipped_examples(renderer, sample_document):
    """End to end on a clean checkout: no personal data, no LLM, a real PDF."""
    result = renderer.render_document(sample_document)
    assert result.pages <= 2
    assert result.pdf.startswith(b"%PDF")


@needs_latex
def test_compile_tex_takes_source_directly(renderer, sample_document, tmp_path):
    tex = renderer.render_tex(_context(sample_document))
    assert renderer.compile_tex(tex, stem="hand_edited").pdf.startswith(b"%PDF")
    assert (tmp_path / "hand_edited.tex").is_file()


@needs_latex
def test_a_broken_template_is_reported_without_leaking_paths(tmp_path, caplog):
    """The TeX log is kilobytes, so it is logged rather than returned; the
    scratch path must not survive into either."""
    renderer = tex_renderer(
        r"\documentclass{article}\begin{document}\undefinedmacro\end{document}", tmp_path
    )
    with caplog.at_level(logging.ERROR), pytest.raises(LatexCompileError) as excinfo:
        renderer.compile_tex(r"\documentclass{article}\begin{document}\undefinedmacro\end{document}")

    assert excinfo.value.detail == {"file": "cv.tex"}
    assert str(tmp_path) not in str(excinfo.value.detail)
    assert "Undefined control sequence" in caplog.text
    assert str(tmp_path) not in caplog.text


def test_a_template_error_is_a_compile_error(tmp_path):
    with pytest.raises(LatexCompileError):
        tex_renderer(r"\VAR{ missing.attribute.here }", tmp_path).render_tex({})


# --- the pdflatex sandbox ---------------------------------------------------
@needs_latex
def test_shell_escape_is_refused(tmp_path):
    """A client-supplied template must not be able to run commands."""
    marker = tmp_path / "pwned"
    tex = (r"\documentclass{article}\begin{document}"
           rf"\immediate\write18{{touch {marker}}}x\end{{document}}")
    try:
        tex_renderer(tex, tmp_path).compile_tex(tex)
    except LatexCompileError:
        pass
    assert not marker.exists()


@needs_latex
def test_reading_outside_the_work_dir_is_refused(tmp_path):
    tex = r"\documentclass{article}\begin{document}\input{/etc/passwd}\end{document}"
    with pytest.raises(LatexCompileError):
        tex_renderer(tex, tmp_path).compile_tex(tex)


@needs_latex
def test_a_looping_template_hits_the_timeout(tmp_path):
    tex = (r"\documentclass{article}\begin{document}"
           r"\newcount\n \loop \advance\n by 1 \ifnum\n>0 \repeat"
           r"\end{document}")
    with pytest.raises(LatexTimeoutError):
        tex_renderer(tex, tmp_path, latex_timeout=5.0).compile_tex(tex)


# --- the page check ---------------------------------------------------------
def test_page_check_escalates_its_advice(monkeypatch, tmp_path):
    """The overflow message is fed back to the LLM, so it must scale."""

    class FakePage:
        def __init__(self, n): self.n = n
        def extract_text(self): return "\n".join(f"line {i}" for i in range(self.n))

    class FakeReader:
        def __init__(self, pages): self.pages = pages

    def reader_with(*counts):
        monkeypatch.setattr("jobstitch_server.pipeline.cv_renderer.PdfReader",
                            lambda p: FakeReader([FakePage(n) for n in counts]))

    reader_with(0, 0, 1)
    assert "REMOVE 1 bullet point" in check_pdf_pages(tmp_path / "x.pdf")["description"]
    reader_with(0, 0, 6)
    assert "REMOVE 3 bullet points" in check_pdf_pages(tmp_path / "x.pdf")["description"]
    reader_with(0, 0, 30)
    assert "EXTREMELY long" in check_pdf_pages(tmp_path / "x.pdf")["description"]
    reader_with(0, 0)
    assert check_pdf_pages(tmp_path / "x.pdf")["description"] == "length OK"


def test_page_check_honors_a_custom_limit(monkeypatch, tmp_path):
    """A caller asking for fewer pages gets advice scoped to that limit."""

    class FakePage:
        def __init__(self, n): self.n = n
        def extract_text(self): return "\n".join(f"line {i}" for i in range(self.n))

    class FakeReader:
        def __init__(self, pages): self.pages = pages

    monkeypatch.setattr(
        "jobstitch_server.pipeline.cv_renderer.PdfReader",
        lambda p: FakeReader([FakePage(0), FakePage(1)]),
    )
    result = check_pdf_pages(tmp_path / "x.pdf", limit=1)
    assert result["description"] != "length OK"
    assert "1 page limit" in result["description"]
    # range(limit, pages) must start at the limit, not the old hardcoded 2.
    assert "page_2_lines" in result
