"""The shared reader accepts real-world workbooks, not only the templates."""

from __future__ import annotations

import io
import time

import numpy as np
import openpyxl
import pandas as pd

from app.engine.pd_engine import compute_pd, is_pd_data_sheet
from app.engine.workbook import frames_from_workbooks, read_workbook_sheets


def _xlsx(rows: list[list], title_rows: int = 0) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for i in range(title_rows):
        ws.append([f"Bank PD extract — note {i}"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_messy_headers_values_and_layout_are_normalised() -> None:
    rows = [
        ["loan_id", " Segment ", "reporting month", "STAGING", "Loan Amount", None, "notes"],
        [1001, "Retail", pd.Timestamp("2025-01-31"), "stage 1", 100.0, None, "x"],
        [1001.0, "Retail", pd.Timestamp("2025-02-28"), "S2", 90.0, "stray", None],
        ["LN-7", "Retail", pd.Timestamp("2025-01-31"), 3, 50.0, None, None],
        [None, None, None, None, None, None, None],
    ]
    sheets = read_workbook_sheets(_xlsx(rows, title_rows=2))
    frame = next(iter(sheets.values()))
    # Title rows skipped, headers canonical, header-less note column dropped.
    assert list(frame.columns) == ["Loan ID", "SEGMENT", "Reporting Month", "Staging", "Loan Amount", "notes"]
    assert frame["Loan ID"].tolist() == ["1001", "1001", "LN-7"]          # one loan, text IDs
    assert frame["Staging"].tolist() == ["Stage 1", "Stage 2", "Stage 3"]
    assert len(frame) == 3                                                # blank row dropped
    assert is_pd_data_sheet(frame)


def test_large_pd_file_reads_and_computes_quickly() -> None:
    rng = np.random.default_rng(7)
    months = pd.date_range("2021-01-31", periods=48, freq="ME")
    rows = [["Loan ID", "SEGMENT", "Reporting Month", "Staging", "Loan Amount"]]
    for loan in range(700):
        seg = ("ST", "LT", "MT", "CT")[loan % 4]
        stage = 1
        for m in months:
            stage = min(3, max(1, stage + int(rng.choice([-1, 0, 0, 0, 0, 1]))))
            rows.append([10_000 + loan, seg, m, f"Stage {stage}", float(rng.integers(1_000, 100_000))])
    content = _xlsx(rows)                                                 # 33,600 rows
    start = time.perf_counter()
    frame = frames_from_workbooks([content], keep_sheet=is_pd_data_sheet)
    curve, _, _ = compute_pd(frame)
    elapsed = time.perf_counter() - start
    assert len(frame) == 700 * 48
    assert set(curve["SEGMENT"]) == {"ST", "LT", "MT", "CT"}
    assert elapsed < 20, f"read + compute took {elapsed:.1f}s"
