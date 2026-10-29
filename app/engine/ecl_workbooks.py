"""Spec v1.0 Step 11 outputs: Workbook 1 (Loan Detail) and Workbook 2 (Portfolio Summary)."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
import pandas as pd
import xlsxwriter

from app.engine.ead_engine import DISCOUNTED_COLLATERAL, ROW_COLUMNS
from app.engine.ecl_pipeline import COVERAGE, SPEC_VERSION, EclRun
from app.engine.ecl_rules import CAT_CAP, CAT_INPUT, CAT_PD, CAT_RECON, LOAN_ID

EXCEL_MAX_ROWS = 1_048_576
LOAN_DETAIL_FILENAME = "ECL Loan Detail.xlsx"
PORTFOLIO_FILENAME = "ECL Portfolio Summary.xlsx"
EXCEPTION_COLUMNS = ["Category", "Dataset", "Loan ID", "Customer ID", "Field", "Value", "Issue", "Action Taken"]
_SLICE_ROWS = 50_000
_MONEY_COLUMNS = {"Outstanding Amount", DISCOUNTED_COLLATERAL, "ECL Before Cap", "Total ECL", "ECL"}


class _Book:
    """xlsxwriter in constant-memory mode: rows stream to disk as they are written,
    so a multi-million-row Row Detail costs flat memory and seconds, not minutes."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.wb = xlsxwriter.Workbook(
            self.buffer,
            {"constant_memory": True, "default_date_format": "yyyy-mm-dd", "nan_inf_to_errors": True},
        )
        self.bold = self.wb.add_format({"bold": True})
        self.money = self.wb.add_format({"num_format": "#,##0.00"})
        self.pct = self.wb.add_format({"num_format": "0.00%"})

    def frame(self, title: str, df: pd.DataFrame, pct: set[str] = frozenset()) -> None:
        ws = self.wb.add_worksheet(title)
        ws.write_row(0, 0, list(df.columns), self.bold)
        for j, col in enumerate(df.columns):
            fmt = self.pct if col in pct else self.money if col in _MONEY_COLUMNS else None
            ws.set_column(j, j, 18, fmt)
        ws.freeze_panes(1, 0)
        # Convert and write in slices so memory stays flat for million-row tabs.
        for start in range(0, len(df), _SLICE_ROWS):
            for i, row in enumerate(_rows(df.iloc[start : start + _SLICE_ROWS]), start=start + 1):
                ws.write_row(i, 0, row)

    def pairs(self, title: str, rows: list[tuple[str, Any]]) -> None:
        ws = self.wb.add_worksheet(title)
        ws.set_column(0, 0, 34)
        ws.set_column(1, 1, 60)
        for i, (label, value) in enumerate(rows):
            ws.write(i, 0, label, self.bold)
            fmt = self.money if label.startswith("Total") else None
            if value is not None and value != "":
                ws.write(i, 1, _value(value), fmt)

    def bytes(self) -> bytes:
        self.wb.close()
        return self.buffer.getvalue()


def _value(v: Any) -> Any:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    return v.item() if hasattr(v, "item") else v


def _rows(df: pd.DataFrame):
    """Rows as plain Python values; NaN/NaT become empty cells."""
    cols = []
    for col in df.columns:
        values = df[col]
        if pd.api.types.is_datetime64_any_dtype(values):
            cols.append(np.array([None if pd.isna(v) else v.to_pydatetime() for v in values], dtype=object))
        else:
            arr = values.to_numpy(dtype=object, copy=True)
            arr[pd.isna(values).to_numpy()] = None
            cols.append(arr)
    return zip(*cols, strict=True) if cols else iter(())


def run_details_rows(run: EclRun, meta: dict[str, Any]) -> list[tuple[str, Any]]:
    """Spec 12.3: everything needed to audit either workbook on its own."""
    counts = {cat: 0 for cat in (CAT_INPUT, CAT_PD, CAT_CAP, CAT_RECON)}
    for e in run.exceptions:
        counts[e.category] = counts.get(e.category, 0) + 1
    rows: list[tuple[str, Any]] = [
        (
            "Reporting Date",
            run.prepared.reporting_date.date() if run.prepared.reporting_date is not None else None,
        ),
        ("Run date and time (UTC)", meta["run_at"]),
        ("Run by", meta["run_by"]),
        ("Run ID", meta["run_id"]),
        ("Specification", f"ECL Calculation Specification Version {SPEC_VERSION}"),
        ("Engine version", meta["engine_version"]),
        ("", ""),
        ("Input dataset", "File · last modified · rows"),
    ]
    for item in meta["inputs"]:
        rows.append(
            (item["dataset"], f"{item['file']} · {item['modified']} · {item['rows']:,} rows")
        )
    rows += [
        ("", ""),
        ("Loans processed", len(run.loan_summary)),
        ("Loans excluded", int(run.prepared.excluded[LOAN_ID].shape[0])),
        *[(f"Exception lines · {cat}", n) for cat, n in counts.items()],
        ("Total Outstanding Amount", run.total_outstanding),
        ("Total ECL (after cap)", run.total_ecl),
        ("", ""),
        ("Reconciliation check", "Result"),
        *[(c.name, "Pass" if c.passed else "Fail") for c in run.reconciliation],
    ]
    return rows


def row_detail_frame(run: EclRun) -> pd.DataFrame:
    """Every column from Steps 1-10, one row per Loan ID and Snapshot Date."""
    step1 = run.lgd.drop(columns=["Customer ID", "Outstanding Amount", DISCOUNTED_COLLATERAL])
    at = ROW_COLUMNS.index(DISCOUNTED_COLLATERAL)
    merged = run.rows.merge(step1, on=LOAN_ID, how="left")
    extra = [c for c in step1.columns if c != LOAN_ID]
    return merged[ROW_COLUMNS[:at] + extra + ROW_COLUMNS[at:]]


def exceptions_frame(run: EclRun) -> pd.DataFrame:
    return pd.DataFrame(
        [
            (e.category, e.dataset, e.loan_id, e.customer_id, e.field, e.value, e.issue, e.action)
            for e in run.exceptions
        ],
        columns=EXCEPTION_COLUMNS,
    )


def build_loan_detail_workbook(run: EclRun, meta: dict[str, Any], max_rows: int = EXCEL_MAX_ROWS) -> bytes:
    book = _Book()
    book.pairs("Run Details", run_details_rows(run, meta))

    detail = row_detail_frame(run)
    capacity = max_rows - 1  # header row repeats on every tab
    # Fill loan by loan; a loan never splits across tabs (detail is sorted by loan).
    sizes = detail.groupby(LOAN_ID, sort=False).size()
    tab_starts, used = [0], 0
    position = 0
    for size in sizes:
        if used and used + size > capacity:
            tab_starts.append(position)
            used = 0
        used += size
        position += size
    bounds = [*tab_starts, len(detail)]
    for i in range(len(tab_starts)):
        book.frame("Row Detail" if i == 0 else f"Row Detail {i + 1}", detail.iloc[bounds[i] : bounds[i + 1]])

    book.frame("Loan Summary", run.loan_summary)
    exceptions = exceptions_frame(run)
    if exceptions.empty:
        exceptions = pd.DataFrame([["No exceptions", *[None] * 7]], columns=EXCEPTION_COLUMNS)
    book.frame("Exception Report", exceptions)
    return book.bytes()


def build_portfolio_workbook(run: EclRun, meta: dict[str, Any]) -> bytes:
    book = _Book()
    book.pairs("Run Details", run_details_rows(run, meta))
    book.frame("Stage Summary", run.stage_summary, pct={COVERAGE})
    book.frame("Segment Summary", run.segment_summary, pct={COVERAGE})
    return book.bytes()
