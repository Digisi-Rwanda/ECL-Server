"""One workbook reader for validation, compute, and later LGD/EAD.

Excel parsing used to live in three places with different engines and concat
rules. A cover page or a `Reporting Month ("As At")` alias then survived in
one path and vanished in another. Every `.xlsx` bytes blob now enters here.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable, Mapping

import pandas as pd


def read_workbook_sheets(content: bytes) -> dict[str, pd.DataFrame]:
    """Parse an .xlsx blob into `{sheet_name: frame}` with a single engine."""
    return pd.read_excel(io.BytesIO(content), sheet_name=None, engine="openpyxl")


def concat_sheets(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Stack non-empty frames in the given order. Empty input → empty frame."""
    kept = [frame for frame in frames if frame is not None and not frame.empty]
    if not kept:
        return pd.DataFrame()
    return pd.concat(kept, ignore_index=True)


def frames_from_workbooks(
    files: Iterable[bytes],
    keep_sheet: Callable[[pd.DataFrame], bool] | None = None,
) -> pd.DataFrame:
    """Concatenate every kept sheet across workbooks, file order then sheet order."""
    frames: list[pd.DataFrame] = []
    for data in files:
        for sheet_df in read_workbook_sheets(data).values():
            if sheet_df.empty:
                continue
            if keep_sheet is not None and not keep_sheet(sheet_df):
                continue
            frames.append(sheet_df)
    return concat_sheets(frames)


def sheets_row_count(sheets: Mapping[str, pd.DataFrame]) -> tuple[int, int]:
    return len(sheets), sum(len(frame) for frame in sheets.values())
