"""Shared workbook reader — validation and compute must parse Excel the same way."""

from io import BytesIO

import pandas as pd

from app.engine.pd_engine import is_pd_data_sheet
from app.engine.workbook import concat_sheets, frames_from_workbooks, read_workbook_sheets


def _xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def test_read_workbook_sheets_keeps_sheet_order() -> None:
    content = _xlsx(
        {
            "Cover": pd.DataFrame({"Note": ["ignore"]}),
            "PD": pd.DataFrame(
                {
                    "Loan ID": ["L1"],
                    "Reporting Month": ["2024-10-31"],
                    "Staging": ["Stage 1"],
                    "Loan Amount": [100.0],
                    "SEGMENT": ["Retail"],
                }
            ),
        }
    )
    sheets = read_workbook_sheets(content)
    assert list(sheets) == ["Cover", "PD"]
    assert len(sheets["PD"]) == 1


def test_frames_from_workbooks_uses_the_pd_data_sheet_predicate() -> None:
    content = _xlsx(
        {
            "Notes": pd.DataFrame({"x": [1]}),
            "PD": pd.DataFrame(
                {
                    "Loan ID": ["L1", "L2"],
                    "Reporting Month": ["2024-10-31", "2024-10-31"],
                    "Staging": ["Stage 1", "Stage 2"],
                    "Loan Amount": [10.0, 20.0],
                    "SEGMENT": ["Retail", "Retail"],
                }
            ),
        }
    )
    combined = frames_from_workbooks([content], keep_sheet=is_pd_data_sheet)
    assert len(combined) == 2
    assert list(combined["Loan ID"]) == ["L1", "L2"]


def test_concat_sheets_skips_empty_and_preserves_order() -> None:
    first = pd.DataFrame({"a": [1]})
    empty = pd.DataFrame()
    second = pd.DataFrame({"a": [2]})
    combined = concat_sheets([first, empty, second])
    assert list(combined["a"]) == [1, 2]
