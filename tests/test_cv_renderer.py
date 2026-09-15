"""LaTeX escaping, Markdown markers, asset handling and the page check."""

import shutil

import pytest

from jobstitch.cv_renderer import CVRenderer, TailoredCVData

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture
def renderer(tmp_path):
    return CVRenderer(output_dir=tmp_path)


def test_escapes_latex_specials(renderer):
    out = renderer._sanitize_latex("R&D costs 50% of $X_1 #2")
    assert r"\&" in out and r"\%" in out and r"\$" in out
    assert r"\_" in out and r"\#" in out


def test_bold_and_italic_markers_become_latex(renderer):
    assert renderer._sanitize_latex("**Kubernetes** rollout") == r"\textbf{Kubernetes} rollout"
    assert renderer._sanitize_latex("*Terraform* modules") == r"\textit{Terraform} modules"


def test_bold_wins_over_italic_inside_the_same_string(renderer):
    out = renderer._sanitize_latex("**Go** and *Rust*")
    assert out == r"\textbf{Go} and \textit{Rust}"


def test_intentional_empty_groups_survive(renderer):
    # "{}--" guards an en-dash in the template; it must not become \{\}--
    assert renderer._sanitize_latex("2024 {}-- 2026") == "2024 {}-- 2026"


def test_sanitize_obj_walks_nested_structures(renderer):
    data = {"a": ["50% off", {"b": "**bold**"}]}
    out = renderer._sanitize_obj(data)
    assert out["a"][0] == r"50\% off"
    assert out["a"][1]["b"] == r"\textbf{bold}"


def test_example_png_is_copied_under_its_real_name(tmp_path):
    resources = tmp_path / "res"
    resources.mkdir()
    (resources / "candidate_signature.example.png").write_bytes(b"\x89PNG-example")
    out = tmp_path / "out"
    CVRenderer(resources_dir=resources, output_dir=out)._copy_image_assets()
    # The template references candidate_signature.png, so that is the name
    # the example must land under.
    assert (out / "candidate_signature.png").read_bytes() == b"\x89PNG-example"
    assert not (out / "candidate_signature.example.png").exists()


def test_real_png_beats_the_example(tmp_path):
    resources = tmp_path / "res"
    resources.mkdir()
    (resources / "candidate_signature.example.png").write_bytes(b"example")
    (resources / "candidate_signature.png").write_bytes(b"mine")
    out = tmp_path / "out"
    CVRenderer(resources_dir=resources, output_dir=out)._copy_image_assets()
    assert (out / "candidate_signature.png").read_bytes() == b"mine"


def sample_cv_data(**overrides):
    data = dict(
        company_name="Globex",
        job_title="Staff Platform Engineer",
        summary="Engineer with 12 years of experience building distributed systems.",
        skills=["Kubernetes", "Terraform", "Go", "PostgreSQL", "AWS", "Observability"],
        experiences=[
            {
                "title": "Principal Platform Engineer",
                "company": "Northwind Logistics",
                "location": "Lisbon, Portugal",
                "dates": "March 2022 -- Present",
                "project_name": "Freight tracking",
                "bullet_points": ["Owned the **event ingestion** architecture."],
            },
            {
                "title": "Senior SRE",
                "company": "Meridian Health",
                "location": "Barcelona, Spain",
                "dates": "August 2018 -- February 2022",
                "project_name": None,
                "bullet_points": ["Designed multi-region failover."],
            },
            {
                "title": "Backend Engineer",
                "company": "Cobalt Analytics",
                "location": "Berlin, Germany",
                "dates": "May 2014 -- July 2018",
                "project_name": None,
                "bullet_points": ["Built a Go query engine."],
            },
        ],
    )
    data.update(overrides)
    return TailoredCVData(**data)


def test_schema_enforces_the_template_s_limits():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        sample_cv_data(skills=["only", "three", "here"])      # min 6
    with pytest.raises(ValidationError):
        sample_cv_data(experiences=[])                        # min 3


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
def test_renders_a_two_page_pdf_from_the_shipped_examples(tmp_path):
    """End to end on a clean checkout: no personal data, no LLM, a real PDF."""
    renderer = CVRenderer(output_dir=tmp_path)
    pdf = renderer.generate_cv_pdf(sample_cv_data())
    info = renderer.check_pdf_pages(pdf)
    assert info["pages"] <= 2
    assert info["description"] == "length OK"
    # Aux files are cleaned up; the sources stay.
    names = {p.name for p in tmp_path.iterdir()}
    assert "cv_Staff_Platform_Engineer.tex" in names
    assert not any(n.endswith((".aux", ".log", ".out")) for n in names)


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex not installed")
def test_recompile_tex_reuses_an_existing_source(tmp_path):
    renderer = CVRenderer(output_dir=tmp_path)
    renderer.generate_cv_pdf(sample_cv_data())
    (tmp_path / "cv_Staff_Platform_Engineer.pdf").unlink()
    pdf = renderer.recompile_tex()
    assert pdf.endswith("cv_Staff_Platform_Engineer.pdf")


def test_page_check_escalates_its_advice(renderer, monkeypatch):
    """The overflow message is fed back to the LLM, so it must scale."""
    class FakePage:
        def __init__(self, n): self.n = n
        def extract_text(self): return "\n".join(f"line {i}" for i in range(self.n))

    class FakeReader:
        def __init__(self, _path, pages): self.pages = pages

    monkeypatch.setattr("jobstitch.cv_renderer.PdfReader",
                        lambda p: FakeReader(p, [FakePage(0), FakePage(0), FakePage(1)]))
    assert "REMOVE 1 bullet point" in renderer.check_pdf_pages("x.pdf")["description"]

    monkeypatch.setattr("jobstitch.cv_renderer.PdfReader",
                        lambda p: FakeReader(p, [FakePage(0), FakePage(0), FakePage(30)]))
    assert "EXTREMELY long" in renderer.check_pdf_pages("x.pdf")["description"]
