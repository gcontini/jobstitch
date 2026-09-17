"""The application spreadsheet — one row per delivered CV.

Bookkeeping on top of the CV, never a reason to fail a job — but that promise
is kept by the caller, not here: a broken or locked spreadsheet raises, and
``JobRunner._produce`` turns it into a warning and a delivered CV anyway. The
file's own header row is the schema, so renaming or reordering columns in your
copy is supported by doing nothing.
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Optional, Protocol

import openpyxl
from jobstitch_contracts import JDAnalysis

#: The empty spreadsheet shipped with the client, copied on first use.
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


def _plain(text: Optional[str]) -> str:
    """Drop the ``**bold**``/``*italics*`` markers the highlighter leaves in
    the CV data — they are LaTeX instructions, not part of the company name."""
    return _HIGHLIGHT_MARKERS.sub("", text or "").strip()


class Tracker(Protocol):
    """Records a delivered job somewhere. Or not."""

    def record(self, job_dir: Path, analysis: JDAnalysis) -> None: ...


class NullTracker:
    """Tracking off — ``--no-xlsx``."""

    def record(self, job_dir: Path, analysis: JDAnalysis) -> None:
        return None


class XlsxTracker:
    """Appends to ``applications.xlsx`` in the output folder."""

    def __init__(self, xlsx_path: Path) -> None:
        self.xlsx_path = Path(xlsx_path)

    def record(self, job_dir: Path, analysis: JDAnalysis) -> None:
        values = {
            "company_name": _plain(analysis.company_name),
            "job_title": _plain(analysis.job_title),
            "application_date": date.today().isoformat(),
            "application status": STATUS_VALUES[0],
            "notes": "",
            "weblink": analysis.posting_url or "",
        }
        self._ensure_file()
        self._append(values, job_dir.name)

    def _ensure_file(self) -> None:
        """Copy the empty template into the output folder on first use."""
        if self.xlsx_path.exists():
            return
        template = Path(str(resources.files("jobstitch_client.resources") / TEMPLATE_NAME))
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



def build_tracker(root: Path, enabled: bool = True) -> Tracker:
    """The tracker for one run: the spreadsheet, or nothing at all."""
    return XlsxTracker(root / TEMPLATE_NAME) if enabled else NullTracker()


__all__ = [
    "Tracker",
    "XlsxTracker",
    "NullTracker",
    "build_tracker",
    "STATUS_VALUES",
    "HEADERS",
    "TEMPLATE_NAME",
]
