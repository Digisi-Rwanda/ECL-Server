"""End-to-end ECL arithmetic: PD × LGD × EAD on a small IFRS 9-shaped book.

This is the formula the compute worker runs. It does not need Celery or the
API — if this passes, the engines still produce a finite discounted ECL.
"""

from __future__ import annotations

import pandas as pd

from app.engine.ecl_pipeline import run_ecl
from app.engine.ecl_rules import prepare_inputs
from app.engine.pd_engine import compute_pd


def _pd_book() -> pd.DataFrame:
    months = pd.to_datetime(
        ["2024-07-31", "2024-08-31", "2024-09-30", "2024-10-31", "2024-11-30", "2024-12-31"]
    )
    rows: list[dict[str, object]] = []
    # Performing loan stays Stage 1.
    for month in months:
        rows.append(
            {
                "Loan ID": "LN-S1",
                "Reporting Month": month,
                "Staging": "Stage 1",
                "Loan Amount": 100_000.0,
                "SEGMENT": "Retail",
            }
        )
    # Watchlist loan migrates 1 → 2.
    stages_s2 = ["Stage 1", "Stage 1", "Stage 2", "Stage 2", "Stage 2", "Stage 2"]
    for month, staging in zip(months, stages_s2, strict=True):
        rows.append(
            {
                "Loan ID": "LN-S2",
                "Reporting Month": month,
                "Staging": staging,
                "Loan Amount": 80_000.0,
                "SEGMENT": "Retail",
            }
        )
    # Defaulted loan migrates 2 → 3.
    stages_s3 = ["Stage 2", "Stage 2", "Stage 3", "Stage 3", "Stage 3", "Stage 3"]
    for month, staging in zip(months, stages_s3, strict=True):
        rows.append(
            {
                "Loan ID": "LN-S3",
                "Reporting Month": month,
                "Staging": staging,
                "Loan Amount": 50_000.0,
                "SEGMENT": "Retail",
            }
        )
    return pd.DataFrame(rows)


def _lgd_book() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Customer ID": ["CU-1", "CU-1", "CU-2"],
            "Loan ID": ["LN-S1", "LN-S2", "LN-S3"],
            "Outstanding Amount": [95_000.0, 76_000.0, 48_000.0],
            "Effective Interest Rate (EIR)": [0.12, 0.14, 0.16],
            "Property": [200_000.0, 200_000.0, 40_000.0],
        }
    )


def _ead_book() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Loan ID": ["LN-S1", "LN-S2", "LN-S3"],
            "Customer ID": ["CU-1", "CU-1", "CU-2"],
            "SEGMENT": ["Retail", "Retail", "Retail"],
            "Reporting Date": pd.to_datetime(["2024-12-31", "2024-12-31", "2024-12-31"]),
            "Maturity Date": pd.to_datetime(["2026-12-31", "2026-06-30", "2025-06-30"]),
            "First Payment Date": pd.to_datetime(["2024-01-31", "2024-01-31", "2024-01-31"]),
            "Outstanding Amount": [95_000.0, 76_000.0, 48_000.0],
            "Staging": ["Stage 1", "Stage 2", "Stage 3"],
            "Effective Interest Rate": [0.12, 0.14, 0.16],
        }
    )


def test_ecl_pipeline_pd_lgd_ead_formula() -> None:
    pd_out, cure_rates, _ = compute_pd(_pd_book())
    assert "Retail" in cure_rates
    assert pd_out["Marginal_PD"].between(0.0, 1.0).all()

    prepared = prepare_inputs(
        _ead_book(), _lgd_book(), [{"name": "Property", "haircut": 0.15, "time_to_realize": 1.5}]
    )
    run = run_ecl(prepared, pd_out)
    rows = run.rows
    assert rows["Loan ID"].nunique() == 3
    assert run.reconciliation_passed

    ead = rows["Balance After Missed Payment"]
    # Core IFRS 9 identity: Credit Loss_t = EAD_t x PD_t x LGD_t
    assert ((rows["Credit Loss"] - ead * rows["Marginal PD"] * rows["LGD"]).abs() < 1e-6).all()
    assert rows["LGD"].between(0.0, 1.0).all()
    assert run.total_ecl > 0

    # Stage 3 is in default: PD 100% on its single, undiscounted row.
    stage3 = rows[rows["Stage"] == "Stage 3"]
    assert len(stage3) == 1 and stage3["Marginal PD"].iloc[0] == 1.0
    assert stage3["Discounted ECL"].iloc[0] == stage3["Credit Loss"].iloc[0]

    # Stage 1: reporting row + 12 months, the reporting row carries no PD.
    stage1 = rows[rows["Loan ID"] == "LN-S1"]
    assert len(stage1) == 13 and stage1["Marginal PD"].iloc[0] == 0.0
