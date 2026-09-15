"""jobstitch — turn a job description into a tailored LaTeX CV and cover letter.

Modules:

- ``paths`` — the two configurable roots (``JOBSTITCH_RESOURCES`` for
  templates/prompts/candidate data, ``JOBSTITCH_HOME`` for the watched
  workspace) and :func:`resource_path`, which falls back to a shipped
  ``*.example.*`` file when you have not supplied your own yet.
- ``model_selector`` — :class:`ModelSelector` over any OpenAI-compatible
  endpoint, built from the three models declared in ``resources/models.toml``
  (``summary`` for JD analysis and the cover letter, ``cv`` for CV writing and
  review, ``highlight`` for the keyword pass): :func:`load_model_config`,
  :func:`build_model`, :func:`build_models`.
- ``jd_validator`` — :class:`JDAnalysis` / :class:`JDValidator` (JD intake and
  match scoring). :class:`JDAnalysis` is the published shape of
  ``analysis.json``, the optional companion a JD source may write next to the
  ``JD.txt`` the pipeline actually runs on.
- ``cv_renderer`` — :class:`CVRenderer` (Jinja LaTeX render + ``pdflatex``
  compile + page check) and the :class:`TailoredCVData` /
  :class:`WorkExperienceItem` structured-data models it consumes.
- ``cv_creation`` — :class:`CVGenerator` (LLM -> schema-validated
  :class:`TailoredCVData` -> render through :class:`CVRenderer` -> 2-page
  condensing loop).
- ``letter_generation`` — :class:`LetterGenerator` (LLM -> lightly validated
  plain-text cover letter), optionally researching the employer on the web
  for a direct posting from a real company.
- ``workbook`` — :class:`ApplicationsWorkbook`, the ``applications.xlsx``
  tracking sheet: one row per generated CV, seeded from the empty template in
  the resources folder.
- ``job_watcher`` — the coordinator + daemon worker threads that watch
  ``incoming/``, generate CVs and cover letters, and hand each finished job to
  the workbook.

JD sources — programs that publish job folders into ``incoming/`` — live
outside this package, in ``jd_sources``: the folder contract is what joins
them to the pipeline, so they import :mod:`jobstitch` and never the reverse.

The ``job_watcher`` symbols are exposed through a lazy ``__getattr__`` so that
``python -m jobstitch.job_watcher`` does not pre-import the module (which
would make runpy re-execute it and warn).

Run the watcher from the repo root with::

    job-watcher            # console script (uv sync), or: uv run job-watcher
"""

from .cv_creation import CVGenerator
from .cv_renderer import CVRenderer, TailoredCVData, WorkExperienceItem
from .jd_validator import JDAnalysis, JDValidator
from .letter_generation import LetterGenerator
from .model_selector import (
    MODEL_ROLES,
    ModelConfig,
    ModelSelector,
    ModelSpec,
    build_model,
    build_models,
    load_model_config,
)
from .paths import JOBSTITCH_HOME, RESOURCES_DIR, XLSX_PATH, resource_path
from .workbook import ApplicationsWorkbook

__all__ = [
    # cv_creation / cv_renderer
    "CVGenerator",
    "CVRenderer",
    "TailoredCVData",
    "WorkExperienceItem",
    # jd_validator
    "JDAnalysis",
    "JDValidator",
    # letter_generation
    "LetterGenerator",
    # model_selector
    "MODEL_ROLES",
    "ModelConfig",
    "ModelSelector",
    "ModelSpec",
    "build_model",
    "build_models",
    "load_model_config",
    # paths
    "JOBSTITCH_HOME",
    "RESOURCES_DIR",
    "XLSX_PATH",
    "resource_path",
    # workbook
    "ApplicationsWorkbook",
    # job_watcher (resolved lazily via __getattr__)
    "INCOMING_DIR",
    "WORKING_DIR",
    "ERROR_DIR",
    "RESUME_DIR",
    "MAX_WORKERS",
    "JobConfig",
    "JobOutcome",
    "build_job_config",
    "main",
]

# Names re-exported from jobstitch.job_watcher, loaded on first access so
# importing the package never triggers job_watcher's module-level code.
_LAZY_JOB_WATCHER_NAMES = frozenset(
    {
        "INCOMING_DIR",
        "WORKING_DIR",
        "ERROR_DIR",
        "RESUME_DIR",
        "MAX_WORKERS",
        "JobConfig",
        "JobOutcome",
        "build_job_config",
        "main",
    }
)


def __getattr__(name: str):
    if name in _LAZY_JOB_WATCHER_NAMES:
        from . import job_watcher as _job_watcher

        return getattr(_job_watcher, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_JOB_WATCHER_NAMES))
