"""One workbook reader for validation, compute, and later LGD/EAD.

Excel parsing used to live in three places with different engines and concat
rules. A cover page or a `Reporting Month ("As At")` alias then survived in
one path and vanished in another. Every `.xlsx` bytes blob now enters here.
"""

from __future__ import annotations

import io
import re
from collections.abc import Callable, Iterable, Mapping

import pandas as pd

# Every header the engines read, in its canonical spelling. Files are matched
# case-, space- and underscore-insensitively ("loan_id", "LOAN ID ", "Loan Id").
KNOWN_HEADERS = (
    "Loan ID",
    "Customer ID",
    "SEGMENT",
    "Reporting Month",
    'Reporting Month ("As At")',
    "As At",
    "Reporting Date",
    "First Payment Date",
    "Maturity Date",
    "Adjusted Maturity Date",
    "Repayment Frequency",
    "Payment Frequency",
    "Staging",
    "Staging (Stage)",
    "Stage",
    "Loan Amount",
    "Outstanding Amount",
    "Effective Interest Rate (EIR)",
    "Effective Interest Rate",
    "EIR",
)
STAGE_COLUMNS = ("Staging", "Staging (Stage)", "Stage")
# IDs join PD, LGD and EAD, so they must be text in every file: 1234, 1234.0
# and "1234" are the same loan.
ID_COLUMNS = ("Loan ID", "Customer ID")
# Header row is searched for in the first rows, so title/notes rows above the
# table are allowed.
HEADER_SCAN_ROWS = 20


def _key(value: object) -> str:
    return re.sub(r"[\s_]+", " ", str(value)).strip().casefold()


_CANONICAL = {_key(h): h for h in KNOWN_HEADERS}
_STAGE_VALUE = re.compile(r"^(?:stage|s)?\s*([123])$", re.IGNORECASE)


def _stage(value: object) -> object:
    """"stage 1", "Stage1", "S1", 1 and "1" all mean "Stage 1"; anything else is kept for the checks to report."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return value
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    m = _STAGE_VALUE.match(text)
    return f"Stage {m.group(1)}" if m else text


def _id(value: object) -> object:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header_row(raw: pd.DataFrame) -> int | None:
    """First row (of the first few) holding a "Loan ID" header, else None."""
    for i in range(min(HEADER_SCAN_ROWS, len(raw))):
        if any(_key(v) == "loan id" for v in raw.iloc[i] if isinstance(v, str)):
            return i
    return None


def tidy_sheet(raw: pd.DataFrame) -> pd.DataFrame:
    """Turn a header-less sheet into a clean table, or an empty frame if it holds none."""
    row = _header_row(raw)
    if row is None:
        row = 0
    header = [str(v).strip() if isinstance(v, str) and v.strip() else None for v in raw.iloc[row]] if len(raw) else []
    body = raw.iloc[row + 1 :]
    keep = [i for i, name in enumerate(header) if name is not None]
    names = [_CANONICAL.get(_key(header[i]), header[i]) for i in keep]
    frame = body.iloc[:, keep].copy()
    frame.columns = names
    # Duplicate headers: keep the first, as Excel users expect.
    frame = frame.loc[:, ~frame.columns.duplicated()]
    frame = frame.dropna(how="all").reset_index(drop=True).infer_objects()
    for col in STAGE_COLUMNS:
        if col in frame.columns:
            frame[col] = frame[col].map(_stage)
    for col in ID_COLUMNS:
        if col in frame.columns:
            frame[col] = frame[col].map(_id).astype(object)
    for col in frame.columns:
        if frame[col].dtype == object and frame[col].map(lambda v: isinstance(v, str)).all():
            frame[col] = frame[col].str.strip()
    return frame


def read_workbook_sheets(content: bytes) -> dict[str, pd.DataFrame]:
    """Parse an .xlsx blob into `{sheet_name: clean frame}`.

    calamine (Rust) reads several times faster and with far less memory than
    openpyxl, which matters on small servers and 25 MB uploads.
    """
    raw = pd.read_excel(io.BytesIO(content), sheet_name=None, engine="calamine", header=None)
    return {name: tidy_sheet(frame) for name, frame in raw.items()}


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
