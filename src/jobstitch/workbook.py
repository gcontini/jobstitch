"""Application tracking spreadsheet — :class:`ApplicationsWorkbook`.

One successful job = one row in ``applications.xlsx``, written after the job
folder has landed in ``resume/``. The row is built from ``cv_data.json`` — the
serialized :class:`~jobstitch.cv_renderer.TailoredCVData` the CV was actually
rendered from — so the spreadsheet says what the CV says.

``analysis.json`` is optional here, as everywhere else in the pipeline: when a
JD source left one in the folder its ``posting_url`` fills the ``webLink``
column, and when it did not the column stays empty. Nothing else in the row
depends on it.

The spreadsheet is seeded from ``resources/applications.xlsx`` the first time a
row is written, so the status dropdown and any formatting in that template
carry over. From then on the file's own header row is the schema: rename,
reorder or add columns there and the rows follow.

Tracking is on unless ``JOBSTITCH_XLSX=off`` (or the watcher's ``--no-xlsx``).
openpyxl is not thread-safe — the watcher calls this from its coordinator
thread only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date
from pathlib import Path
from typing import Optional

import openpyxl

from .cv_renderer import TailoredCVData
from .paths import RESOURCES_DIR, XLSX_PATH, resource_path

#: The empty spreadsheet in the resources folder, copied on first use.
TEMPLATE_NAME = "applications.xlsx"

#: The states an application moves through (the template's dropdown).
STATUS_VALUES = ("not applied", "applied", "wait 1st interview", "wait follow up")

#: Column names of a freshly seeded spreadsheet, in order.
HEADERS = (
    "company_name",
    "job_title",
    "application_date",
    "application status",
    "notes",
    "webLink",
)

#: The column the row is keyed on: its first empty cell is the row to fill.
DATE_COLUMN = "application_date"

_HIGHLIGHT_MARKERS = re.compile(r"\*+")


def tracking_enabled() -> bool:
    """``JOBSTITCH_XLSX=off`` disables the spreadsheet (same on/off switch
    convention as ``JOBSTITCH_CLIPBOARD``)."""
    return os.getenv("JOBSTITCH_XLSX", "on").strip().lower() != "off"


def _plain(text: Optional[str]) -> str:
    """Drop the ``**bold**``/``*italics*`` markers the highlighter leaves in
    the CV data — they are LaTeX instructions, not part of the company name."""
    return _HIGHLIGHT_MARKERS.sub("", text or "").strip()


def _posting_url(job_dir: Path) -> str:
    """The ``posting_url`` from ``analysis.json``, when a JD source wrote one.

    The file is optional throughout the pipeline, so anything missing or
    unreadable in it costs the link and nothing else.
    """
    analysis_file = job_dir / "analysis.json"
    if not analysis_file.is_file():
        return ""
    try:
        analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"  ⚠ analysis.json unparsable in {job_dir.name} — no webLink",
              flush=True)
        return ""
    return analysis.get("posting_url") or ""


class ApplicationsWorkbook:
    """Appends one row per generated CV to ``applications.xlsx``.

    Parameters
    ----------
    xlsx_path:
        The spreadsheet to write. Defaults to ``applications.xlsx`` in the
        workspace (``JOBSTITCH_HOME``).
    resources_dir:
        Where the empty template is seeded from when the spreadsheet does not
        exist yet. Defaults to the resources folder.
    """

    def __init__(
        self,
        xlsx_path: Optional[Path] = None,
        resources_dir: Optional[Path] = None,
    ) -> None:
        self.xlsx_path = Path(xlsx_path) if xlsx_path is not None else XLSX_PATH
        self.resources_dir = (
            Path(resources_dir) if resources_dir is not None else RESOURCES_DIR
        )

    def record(self, job_dir: Path) -> None:
        """Append the row for the finished job in ``job_dir``.

        Raises on anything it cannot do — a missing ``cv_data.json``, an
        unwritable file, a spreadsheet whose header row it cannot match. The
        caller decides how much that matters (for the watcher: the CV is
        already delivered, so a failure here is only worth a log line).
        """
        values = self._row_values(job_dir)
        self._ensure_file()
        self._append(values, job_dir.name)

    def _row_values(self, job_dir: Path) -> dict[str, str]:
        """Map lowercased column name -> cell value for one job."""
        cv_data = TailoredCVData.model_validate_json(
            (job_dir / "cv_data.json").read_text(encoding="utf-8")
        )
        return {
            "company_name": _plain(cv_data.company_name),
            "job_title": _plain(cv_data.job_title),
            "application_date": date.today().isoformat(),
            "application status": STATUS_VALUES[0],
            "notes": "",
            "weblink": _posting_url(job_dir),
        }

    def _ensure_file(self) -> None:
        """Copy the empty template into the workspace on first use."""
        if self.xlsx_path.exists():
            return
        template = resource_path(TEMPLATE_NAME, self.resources_dir)
        self.xlsx_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, self.xlsx_path)
        print(f"  📊 created {self.xlsx_path} from {template.name}", flush=True)

    def _append(self, values: dict[str, str], job_name: str) -> None:
        wb = openpyxl.load_workbook(self.xlsx_path)
        ws = wb.active

        # The file's own header row is the schema (case-insensitive).
        headers = [str(c.value).strip() for c in ws[1]]
        norm_headers = [h.lower() for h in headers]
        if DATE_COLUMN not in norm_headers:
            raise RuntimeError(
                f"{self.xlsx_path} has no '{DATE_COLUMN}' column "
                f"(headers: {headers})"
            )

        row = [values.get(name, "") for name in norm_headers]

        # Reuse the first row whose date cell is empty (keeps the formatting
        # and validation a pre-formatted template already put there).
        date_col = norm_headers.index(DATE_COLUMN) + 1
        target_row = None
        for r in range(2, ws.max_row + 2):
            if ws.cell(row=r, column=date_col).value in (None, ""):
                target_row = r
                break
        if target_row is None:
            target_row = ws.max_row + 1

        for col, value in enumerate(row, start=1):
            ws.cell(row=target_row, column=col, value=None if value == "" else value)

        # Extend data-validation ranges (the status dropdown) to the new row.
        for dv in ws.data_validations.dataValidation:
            for rng in dv.sqref.ranges:
                if rng.min_row <= target_row <= rng.max_row:
                    continue
                rng.max_row = max(rng.max_row, target_row)

        wb.save(self.xlsx_path)

        # Verify the write by reloading the file.
        check = openpyxl.load_workbook(self.xlsx_path)
        written = check.active.cell(row=target_row, column=date_col).value
        if written != values[DATE_COLUMN]:
            raise RuntimeError(
                f"verification failed: row {target_row} not found in {self.xlsx_path}"
            )
        print(f"  📊 xlsx row {target_row} written for {job_name}", flush=True)


__all__ = [
    "ApplicationsWorkbook",
    "tracking_enabled",
    "STATUS_VALUES",
    "HEADERS",
    "TEMPLATE_NAME",
]
