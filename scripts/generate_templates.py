#!/usr/bin/env python3
"""Generate comprehensive IFRS 9 ECL input templates (PD, LGD, EAD).

Run from the ECL-Server root:
    python scripts/generate_templates.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_OUT_DIR = _REPO_ROOT / "app" / "static" / "templates"

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
except ImportError:
    sys.exit("openpyxl is required.  Run: pip install openpyxl")


# ── Palette / constants ────────────────────────────────────────────────────────
_HDR_BG      = "1A3C5E"   # ECL dark navy
_HDR_FG      = "FFFFFF"
_ALT_ROW     = "F0F7FF"   # light blue alternating rows
_REQ_TINT    = "D9E8F5"   # required-column tint in Instructions
_BORDER_CLR  = "94A3B8"

_TAB_PD  = "2563EB"
_TAB_LGD = "16A34A"
_TAB_EAD = "9333EA"

_FMT_DATE     = "YYYY-MM-DD"
_FMT_CURRENCY = '#,##0.00'
_FMT_DEC4     = '0.0000'

_DV_STAGE = '"Stage 1,Stage 2,Stage 3"'


# ── Style helpers ──────────────────────────────────────────────────────────────
def _font(bold: bool = False, size: int = 10, color: str = "000000") -> Font:
    return Font(name="Calibri", bold=bold, size=size, color=color)


def _fill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def _thin_border() -> Border:
    s = Side(style="thin", color=_BORDER_CLR)
    return Border(left=s, right=s, top=s, bottom=s)


def _header_border() -> Border:
    thin = Side(style="thin",   color=_BORDER_CLR)
    thick = Side(style="medium", color=_HDR_BG)
    return Border(left=thin, right=thin, top=thin, bottom=thick)


def _align(h: str = "left", wrap: bool = False) -> Alignment:
    return Alignment(horizontal=h, vertical="center", wrap_text=wrap)


def _col_letter(n: int) -> str:
    return get_column_letter(n)


def _style_header_row(ws, n_cols: int) -> None:
    ws.row_dimensions[1].height = 30
    for c in range(1, n_cols + 1):
        cell = ws.cell(row=1, column=c)
        cell.font      = _font(bold=True, size=11, color=_HDR_FG)
        cell.fill      = _fill(_HDR_BG)
        cell.alignment = _align("center", wrap=True)
        cell.border    = _header_border()


def _style_data_rows(ws, first: int, last: int, n_cols: int) -> None:
    for r in range(first, last + 1):
        bg = _ALT_ROW if r % 2 == 0 else "FFFFFF"
        ws.row_dimensions[r].height = 18
        for c in range(1, n_cols + 1):
            cell = ws.cell(row=r, column=c)
            cell.fill   = _fill(bg)
            cell.border = _thin_border()


def _add_dv(ws, formula: str, sqref: str, title: str, prompt: str) -> None:
    dv = DataValidation(
        type="list",
        formula1=formula,
        allow_blank=True,
        showDropDown=False,
        showInputMessage=True,
        promptTitle=title,
        prompt=prompt,
        showErrorMessage=True,
        errorTitle="Invalid value",
        error=f"Choose from the list: {title}",
    )
    ws.add_data_validation(dv)
    dv.sqref = sqref


def _col_width(ws, col: int, width: float) -> None:
    ws.column_dimensions[_col_letter(col)].width = width


# ── Instructions sheet factory ─────────────────────────────────────────────────
def _make_instructions(
    wb: Workbook,
    tab_color: str,
    title: str,
    intro: str,
    col_rows: list[tuple],  # (name, required, type_, allowed, rule, example)
    extra: list[tuple[str, list[str]]] | None = None,
) -> None:
    ws = wb.create_sheet("Instructions")
    ws.sheet_properties.tabColor = tab_color
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 32
    ws.column_dimensions["E"].width = 50
    ws.column_dimensions["F"].width = 28

    def _write_banner(row: int, text: str, merge: bool = True) -> None:
        ws.row_dimensions[row].height = 28
        c = ws.cell(row=row, column=1, value=text)
        c.font      = _font(bold=True, size=13, color=_HDR_FG)
        c.fill      = _fill(_HDR_BG)
        c.alignment = _align("left")
        if merge:
            ws.merge_cells(f"A{row}:F{row}")

    def _write_note(row: int, text: str) -> None:
        ws.row_dimensions[row].height = 40
        c = ws.cell(row=row, column=1, value=text)
        c.font      = _font(size=10, color="334155")
        c.fill      = _fill("EFF6FF")
        c.alignment = _align("left", wrap=True)
        c.border    = _thin_border()
        ws.merge_cells(f"A{row}:F{row}")

    _write_banner(1, title)
    _write_note(2, intro)

    # Table header row
    tbl_hdr_row = 4
    ws.row_dimensions[tbl_hdr_row].height = 22
    for ci, label in enumerate(
        ["Column Name", "Required", "Type", "Allowed Values", "Business Rule", "Example"], 1
    ):
        c = ws.cell(row=tbl_hdr_row, column=ci, value=label)
        c.font      = _font(bold=True, size=10, color=_HDR_FG)
        c.fill      = _fill(_HDR_BG)
        c.alignment = _align("center", wrap=True)
        c.border    = _header_border()

    for i, (name, req, typ, allowed, rule, example) in enumerate(col_rows):
        r = tbl_hdr_row + 1 + i
        ws.row_dimensions[r].height = 36
        vals = [name, req, typ, allowed, rule, example]
        bg = _REQ_TINT if req == "Yes" else "FAFAFA"
        for ci, val in enumerate(vals, 1):
            c = ws.cell(row=r, column=ci, value=val)
            c.fill      = _fill(bg)
            c.font      = _font(bold=(ci == 1))
            c.alignment = _align("left", wrap=True)
            c.border    = _thin_border()

    next_row = tbl_hdr_row + 1 + len(col_rows) + 1

    if extra:
        for section_title, lines in extra:
            _write_banner(next_row, section_title)
            next_row += 1
            for line in lines:
                ws.row_dimensions[next_row].height = 24
                c = ws.cell(row=next_row, column=1, value=line)
                c.font      = _font(size=10, color="1e293b")
                c.fill      = _fill("F8FAFC")
                c.alignment = _align("left", wrap=True)
                c.border    = _thin_border()
                ws.merge_cells(f"A{next_row}:F{next_row}")
                next_row += 1
            next_row += 1


# ── PD template ───────────────────────────────────────────────────────────────
def _build_pd(wb: Workbook) -> None:
    ws = wb.active
    ws.title = "Data"
    ws.sheet_properties.tabColor = _TAB_PD

    headers = ["Loan ID", "SEGMENT", "Reporting Month", "Staging", "Loan Amount"]
    for c, h in enumerate(headers, 1):
        ws.cell(row=1, column=c, value=h)

    # 5 loans × 3 months showing staging transitions
    rows = [
        # Loan ID,         SEGMENT,      Reporting Month,  Staging,   Loan Amount
        ("LN-2025-0001", "Retail",      "2025-01-31",    "Stage 1",  500_000.00),
        ("LN-2025-0002", "Retail",      "2025-01-31",    "Stage 1",  750_000.00),
        ("LN-2025-0003", "Corporate",   "2025-01-31",    "Stage 1", 3_200_000.00),
        ("LN-2025-0004", "SME",         "2025-01-31",    "Stage 2", 1_100_000.00),
        ("LN-2025-0005", "SME",         "2025-01-31",    "Stage 1",   880_000.00),
        # February — LN-0002 deteriorates to Stage 2
        ("LN-2025-0001", "Retail",      "2025-02-28",    "Stage 1",  495_000.00),
        ("LN-2025-0002", "Retail",      "2025-02-28",    "Stage 2",  750_000.00),
        ("LN-2025-0003", "Corporate",   "2025-02-28",    "Stage 1", 3_150_000.00),
        ("LN-2025-0004", "SME",         "2025-02-28",    "Stage 2", 1_080_000.00),
        ("LN-2025-0005", "SME",         "2025-02-28",    "Stage 1",   870_000.00),
        # March — LN-0004 deteriorates to Stage 3, LN-0002 remains Stage 2
        ("LN-2025-0001", "Retail",      "2025-03-31",    "Stage 1",  490_000.00),
        ("LN-2025-0002", "Retail",      "2025-03-31",    "Stage 2",  748_000.00),
        ("LN-2025-0003", "Corporate",   "2025-03-31",    "Stage 1", 3_100_000.00),
        ("LN-2025-0004", "SME",         "2025-03-31",    "Stage 3", 1_060_000.00),
        ("LN-2025-0005", "SME",         "2025-03-31",    "Stage 1",   860_000.00),
    ]

    for r, (loan_id, segment, month, staging, amount) in enumerate(rows, 2):
        ws.cell(row=r, column=1, value=loan_id).alignment = _align("center")
        ws.cell(row=r, column=2, value=segment).alignment  = _align("center")
        d = ws.cell(row=r, column=3, value=month)
        d.number_format = _FMT_DATE
        d.alignment = _align("center")
        ws.cell(row=r, column=4, value=staging).alignment  = _align("center")
        a = ws.cell(row=r, column=5, value=amount)
        a.number_format = _FMT_CURRENCY
        a.alignment = _align("right")

    n_data = len(rows)
    _style_header_row(ws, 5)
    _style_data_rows(ws, 2, 1 + n_data, 5)
    ws.freeze_panes = "A2"

    for w, col in zip([20, 16, 20, 16, 20], range(1, 6)):
        _col_width(ws, col, w)

    _add_dv(ws, _DV_STAGE, "D2:D50000", "Staging", "Stage 1 = performing  |  Stage 2 = SICR  |  Stage 3 = default")

    _make_instructions(
        wb,
        tab_color=_TAB_PD,
        title="Probability of Default (PD) — Upload Guide",
        intro=(
            "Upload one or more monthly PD files per run. Each file is a snapshot of your loan book for "
            "one or more reporting months. The engine automatically derives month-to-month staging "
            "transitions to build an IFRS 9 3×3 transition matrix per segment.  Supply at least "
            "2–3 months of history to obtain meaningful lifetime PD curves."
        ),
        col_rows=[
            ("Loan ID",         "Yes", "Text",   "Any non-empty string",
             "Must be non-empty. Each (Loan ID, Reporting Month) pair must be unique across all "
             "uploaded PD files for this run — duplicates trigger EC-10.",
             "LN-2025-0001"),
            ("SEGMENT",         "Yes", "Text",   "Must match a configured segment name",
             "Case-sensitive match to one of your workspace's Segment names "
             "(Workspace → Admin → Segments).",
             "Retail"),
            ("Reporting Month", "Yes", "Date",   "YYYY-MM-DD or Excel date",
             "The as-at date for this row. Typically the last calendar day of the reporting month.",
             "2025-01-31"),
            ("Staging",         "Yes", "Enum",   "Stage 1  /  Stage 2  /  Stage 3",
             "IFRS 9 credit stage at the reporting date. Case-sensitive. "
             "Stage 1 = performing, Stage 2 = significant increase in credit risk (SICR), "
             "Stage 3 = credit-impaired / default.",
             "Stage 1"),
            ("Loan Amount",     "Yes", "Number", "≥ 0",
             "Outstanding principal balance at the reporting date. Must be non-negative.",
             "500000.00"),
        ],
        extra=[
            ("How the transition matrix is constructed", [
                "The engine joins each loan's staging in month N with its staging in month N+1.",
                "  • LN-001 is Stage 1 in Jan-25 and Stage 2 in Feb-25  →  Stage 1→Stage 2 transition recorded.",
                "  • LN-001 is Stage 1 in Jan-25 and absent in Feb-25  →  Stage 1→Offbooks (fully repaid/written off).",
                "All monthly transitions are summed into a 3×3 aggregate matrix per SEGMENT.",
                "Row proportions are computed, Stage 1 and Stage 3 rows are normalised (IFRS 9 boundary conditions),",
                "and the resulting matrix is raised to powers 1–299 to produce the lifetime PD curve.",
                "Cure rate = P(Stage 3 → Stage 1) + P(Stage 3 → Stage 2) from the proportion matrix.",
            ]),
            ("Accepted aliases for Reporting Month column", [
                "The engine also recognises the column name as:   Reporting Month (\"As At\")   or   As At",
            ]),
            ("Multiple files per run (PD only)", [
                "PD is the only input type where multiple files can be uploaded per run (up to 10 files, 25 MB each).",
                "All files are concatenated and deduplicated on (Loan ID, Reporting Month) before the engine runs.",
                "This allows you to split large monthly exports into separate files.",
            ]),
        ],
    )


# ── LGD template ──────────────────────────────────────────────────────────────
def _spec_data_sheet(ws, headers: list[str], rows: list[list]) -> None:
    ws.append(headers)
    _style_header_row(ws, len(headers))
    for row in rows:
        ws.append(row)
    _style_data_rows(ws, 2, len(rows) + 1, len(headers))
    for i in range(1, len(headers) + 1):
        _col_width(ws, i, 24)
    ws.freeze_panes = "A2"


def _spec_guide(ws, title: str, lines: list) -> None:
    ws.append([title])
    ws["A1"].font = _font(bold=True, size=14)
    for line in lines:
        ws.append(line if isinstance(line, list) else [line])
        if isinstance(line, list) and line and line[0] == "Column Name":
            for cell in ws[ws.max_row]:
                cell.font = _font(bold=True)
    for col, width in zip("ABCDE", (34, 14, 12, 26, 90), strict=True):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = _align(wrap=True)


def _build_lgd(wb: Workbook) -> None:
    """Collateral Dataset, ECL Calculation Specification v1.0 section 1.1."""
    ws = wb.active
    ws.title = "Data"
    ws.sheet_properties.tabColor = _TAB_LGD
    _spec_data_sheet(
        ws,
        ["Customer ID", "Loan ID", "Outstanding Amount", "Effective Interest Rate (EIR)",
         "Real Estate", "Motor Vehicle", "Cash Deposit", "Corporate Guarantee"],
        [
            ["CUST-001", "LN-2025-0001", 490000, 0.14, 350000, 250000, 0, 0],
            ["CUST-001", "LN-2025-0002", 748000, 0.14, 350000, 250000, 0, 0],
            ["CUST-002", "LN-2025-0003", 215000, 0.16, 0, 0, 40000, 100000],
        ],
    )
    _spec_guide(wb.create_sheet("Instructions"), "Collateral Dataset (LGD) — ECL Calculation Specification v1.0", [
        "One row per loan. Collateral is held at CUSTOMER level: when a customer has several loans, every one of "
        "that customer's rows shows the customer's TOTAL collateral for each type (the same value repeated). The "
        "engine splits it across the loans by each loan's share of the customer's exposure.",
        [],
        ["Column Name", "Required", "Type", "Allowed values", "Rule"],
        ["Customer ID", "Yes", "Text", "Same as the EAD file", "A different Customer ID is logged and the EAD value is used."],
        ["Loan ID", "Yes", "Text", "Unique, present in EAD", "Duplicates exclude the loan; Loan IDs not in the EAD file are ignored."],
        ["Outstanding Amount", "Yes", "Number", "≥ 0", "Informational; allocation uses the EAD file's Outstanding Amount."],
        ["Effective Interest Rate (EIR)", "Yes", "Number", "0 – 1 (decimal)", "Discount rate for collateral; if missing or invalid the EAD EIR is used."],
        ["[Collateral type]", "One per type", "Number", "≥ 0", "Header must match a Collateral Type in Workspace → Collateral types. "
         "Blank = 0. Negative or text = treated as 0. Different values for one customer → the lowest is used."],
        [],
        ["Formulas (spec Step 1)"],
        ["Total Customer Exposure = Σ Outstanding Amount (EAD file) by Customer ID"],
        ["Loan Proportion = Outstanding Amount / Total Customer Exposure"],
        ["Allocated[type] = Loan Proportion × Collateral[type]"],
        ["Discounted[type] = Allocated[type] × (1 − Haircut) × (1 + EIR)^(−Time to realize in years)"],
        ["Discounted Collateral = Σ Discounted[type]"],
        ["LGD = max(0, 1 − Discounted Collateral / EAD) × (1 − Cure Rate)"],
        [],
        ["Collateral parameters come from Workspace → Collateral types: haircut in %, time to realize in months "
         "(converted to years). A collateral column without valid parameters counts as 0 and is reported."],
    ])


def _build_ead(wb: Workbook) -> None:
    """Exposure Dataset, ECL Calculation Specification v1.0 section 1.3."""
    ws = wb.active
    ws.title = "Data"
    ws.sheet_properties.tabColor = _TAB_EAD
    _spec_data_sheet(
        ws,
        ["Loan ID", "Customer ID", "SEGMENT", "Reporting Date", "First Payment Date", "Maturity Date",
         "Stage", "Outstanding Amount", "Effective Interest Rate (EIR)"],
        [
            ["LN-2025-0001", "CUST-001", "Retail", "2025-03-31", "2022-04-30", "2029-03-31", "Stage 1", 490000, 0.14],
            ["LN-2025-0002", "CUST-001", "Retail", "2025-03-31", "2021-07-31", "2028-06-30", "Stage 2", 748000, 0.14],
            ["LN-2025-0003", "CUST-002", "SME", "2025-03-31", "2020-01-31", "2026-12-31", "Stage 3", 215000, 0.16],
        ],
    )
    _spec_guide(wb.create_sheet("Instructions"), "Exposure Dataset (EAD) — ECL Calculation Specification v1.0", [
        "One row per loan, all at the run's Reporting Date. A loan that fails a rule below is excluded from the run "
        "and listed in the Exception Report; it does not stop the run.",
        [],
        ["Column Name", "Required", "Type", "Allowed values", "Rule"],
        ["Loan ID", "Yes", "Text", "Unique", "A Loan ID that appears more than once is excluded (all its rows)."],
        ["Customer ID", "Yes", "Text", "Non-empty", "Groups loans for collateral allocation (Total Customer Exposure)."],
        ["SEGMENT", "Yes", "Text", "Must have a PD curve", "Joins the PD term structure (SEGMENT, Month, Transition)."],
        ["Reporting Date", "Yes", "Date", "= the run's Reporting Date", "Rows with another date are excluded."],
        ["First Payment Date", "Yes", "Date", "≤ Reporting Date", "Period Since Origination = months between First Payment Date and each Snapshot Date."],
        ["Maturity Date", "Yes", "Date", "≥ First Payment Date", "Adjusted Maturity Date = last day of this month (derived, do not supply)."],
        ["Stage", "Yes", "Text", "Stage 1 / Stage 2 / Stage 3", "Also accepted as 'Staging' or 'Staging (Stage)'."],
        ["Outstanding Amount", "Yes", "Number", "≥ 0", "Balance at the Reporting Date; ECL is capped at this amount."],
        ["Effective Interest Rate (EIR)", "Yes", "Number", "0 – 1 (decimal)", "Annual rate, e.g. 0.14 for 14%. Above 1 is treated as a percentage mistake and excluded."],
        [],
        ["How exposure is projected"],
        ["Stage 1: Reporting Date + 12 monthly rows (12 months of risk; the Reporting-Date row carries no PD)."],
        ["Stage 2: monthly rows to the Adjusted Maturity Date (lifetime)."],
        ["Stage 3, matured loans and zero-rate loans: a single row on the Outstanding Amount. Stage 3 Marginal PD = 100%."],
        ["Instalment = Outstanding × r / (1 − (1 + r)^−n), r = EIR / 12, n = remaining months."],
        ["Balance After Repayment: first row = Outstanding, then max(0, previous × (1 + r) − instalment)."],
        ["EAD (Balance After Missed Payment) = Balance After Repayment[t − k] × (1 + r)^k, k = min(t, 3)."],
        ["Older templates' 'Adjusted Maturity Date' and 'Repayment Frequency' columns are ignored (monthly instalments)."],
    ])


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    specs = [
        ("PD",  _build_pd,  "PD.xlsx"),
        ("LGD", _build_lgd, "LGD.xlsx"),
        ("EAD", _build_ead, "EAD.xlsx"),
    ]

    print("Generating ECL input templates …")
    for kind, builder, filename in specs:
        wb = Workbook()
        builder(wb)
        out_path = _OUT_DIR / filename
        wb.save(out_path)
        size_kb = out_path.stat().st_size // 1024
        print(f"  {kind:4s}  →  {out_path}  ({size_kb} KB)")

    print(f"\nDone.  Templates written to:  {_OUT_DIR}")


if __name__ == "__main__":
    main()
