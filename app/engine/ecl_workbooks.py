"""Spec v1.0 Step 11 outputs: Workbook 1 (Loan Detail) and Workbook 2 (Portfolio Summary)."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font

from app.engine.ead_engine import DISCOUNTED_COLLATERAL, ROW_COLUMNS
from app.engine.ecl_pipeline import COVERAGE, SPEC_VERSION, EclRun
from app.engine.ecl_rules import CAT_CAP, CAT_INPUT, CAT_PD, CAT_RECON, LOAN_ID

EXCEL_MAX_ROWS = 1_048_576
LOAN_DETAIL_FILENAME = "ECL Loan Detail.xlsx"
PORTFOLIO_FILENAME = "ECL Portfolio Summary.xlsx"

_BOLD = Font(bold=True)
_MONEY = "#,##0.00"
_PCT = "0.00%"
EXCEPTION_COLUMNS = [
    "Category",
    "Dataset",
    "Loan ID",
    "Customer ID",
    "Field",
    "Value",
    "Issue",
    "Action Taken",
]


def _value(v: Any) -> Any:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    return v.item() if hasattr(v, "item") else v


def _header(ws, columns: list[str]) -> None:
    cells = []
    for name in columns:
        c = WriteOnlyCell(ws, value=name)
        c.font = _BOLD
        cells.append(c)
    ws.append(cells)


def _append_frame(
    ws, df: pd.DataFrame, money: set[str] = frozenset(), pct: set[str] = frozenset()
) -> None:
    _header(ws, list(df.columns))
    fmts = [(_MONEY if c in money else _PCT if c in pct else None) for c in df.columns]
    for row in df.itertuples(index=False, name=None):
        out = []
        for v, fmt in zip(row, fmts, strict=True):
            if fmt is None:
                out.append(_value(v))
            else:
                c = WriteOnlyCell(ws, value=_value(v))
                c.number_format = fmt
                out.append(c)
        ws.append(out)


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


def _write_run_details(wb: Workbook, run: EclRun, meta: dict[str, Any]) -> None:
    ws = wb.create_sheet("Run Details")
    for label, value in run_details_rows(run, meta):
        c = WriteOnlyCell(ws, value=label)
        c.font = _BOLD
        v = WriteOnlyCell(ws, value=_value(value))
        if label.startswith("Total"):
            v.number_format = _MONEY
        ws.append([c, v])


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


def build_loan_detail_workbook(
    run: EclRun, meta: dict[str, Any], max_rows: int = EXCEL_MAX_ROWS
) -> bytes:
    wb = Workbook(write_only=True)
    _write_run_details(wb, run, meta)

    detail = row_detail_frame(run)
    capacity = max_rows - 1  # header row repeats on every tab
    tabs: list[list[pd.DataFrame]] = [[]]
    used = 0
    # Fill loan by loan; a loan never splits across tabs.
    for _, loan_rows in detail.groupby(LOAN_ID, sort=False):
        if used and used + len(loan_rows) > capacity:
            tabs.append([])
            used = 0
        tabs[-1].append(loan_rows)
        used += len(loan_rows)
    for i, parts in enumerate(tabs):
        ws = wb.create_sheet("Row Detail" if i == 0 else f"Row Detail {i + 1}")
        frame = pd.concat(parts) if parts else detail.iloc[0:0]
        _append_frame(ws, frame)

    ws = wb.create_sheet("Loan Summary")
    _append_frame(
        ws,
        run.loan_summary,
        money={"Outstanding Amount", DISCOUNTED_COLLATERAL, "ECL Before Cap", "Total ECL"},
    )

    ws = wb.create_sheet("Exception Report")
    exceptions = exceptions_frame(run)
    if exceptions.empty:
        _header(ws, EXCEPTION_COLUMNS)
        ws.append(["No exceptions"])
    else:
        _append_frame(ws, exceptions)
    return _to_bytes(wb)


def build_portfolio_workbook(run: EclRun, meta: dict[str, Any]) -> bytes:
    wb = Workbook(write_only=True)
    _write_run_details(wb, run, meta)
    for title, table in (
        ("Stage Summary", run.stage_summary),
        ("Segment Summary", run.segment_summary),
    ):
        ws = wb.create_sheet(title)
        _append_frame(ws, table, money={"Outstanding Amount", "ECL"}, pct={COVERAGE})
    return _to_bytes(wb)


def _to_bytes(wb: Workbook) -> bytes:
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
