"""End-to-end ECL arithmetic: PD × LGD × EAD on a small IFRS 9-shaped book.

This is the formula the compute worker runs. It does not need Celery or the
API — if this passes, the engines still produce a finite discounted ECL.
"""

from __future__ import annotations

import pandas as pd

from app.engine.ead_engine import compute_ead
from app.engine.lgd_engine import compute_lgd
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
            "Adjusted Maturity Date": pd.to_datetime(["2026-12-31", "2026-06-30", "2025-06-30"]),
            "First Payment Date": pd.to_datetime(["2024-12-31", "2024-12-31", "2024-12-31"]),
            "Outstanding Amount": [95_000.0, 76_000.0, 48_000.0],
            "Repayment Frequency": ["MTH", "MTH", "MTH"],
            "Staging": ["Stage 1", "Stage 2", "Stage 3"],
            "Effective Interest Rate": [0.12, 0.14, 0.16],
        }
    )


def test_ecl_pipeline_pd_lgd_ead_formula() -> None:
    pd_out, cure_rates, _ = compute_pd(_pd_book())
    assert not pd_out.empty
    assert "Retail" in cure_rates
    assert set(pd_out["SEGMENT"].unique()) == {"Retail"}
    assert pd_out["Marginal_PD"].between(0.0, 1.0).all()

    lgd_out = compute_lgd(
        _lgd_book(),
        [{"name": "Property", "haircut": 0.15, "time_to_realize": 18}],
    )
    assert len(lgd_out) == 3
    collateral = lgd_out["Sum of Discounted Collaterals per Loan ID"].astype(float)
    assert (collateral >= 0).all()
    # Shared Property on CU-1 is split 95:76, not copied onto both loans.
    cu1 = lgd_out[lgd_out["Customer ID"] == "CU-1"]
    assert abs(float(cu1["Proportion"].sum()) - 1.0) < 1e-9

    ead_out, warnings = compute_ead(_ead_book(), pd_out, lgd_out)
    assert not ead_out.empty
    assert ead_out["Loan ID"].nunique() == 3
    assert all("EC-08" not in w for w in warnings)

    ead = ead_out["Balance After Missed Payment"].astype(float)
    pd_t = ead_out["Marginal PD"].astype(float)
    lgd_t = ead_out["LGD"].astype(float)
    credit = ead_out["Credit Loss"].astype(float)
    discounted = ead_out["Discounted ECL"].astype(float)

    assert (pd_t.between(0.0, 1.0)).all()
    assert (lgd_t.between(0.0, 1.0)).all()
    assert (ead >= 0).all()
    # Core IFRS 9 identity: Credit Loss_t = EAD_t × PD_t × LGD_t
    assert ((credit - ead * pd_t * lgd_t).abs() < 1e-6).all()
    assert (discounted >= 0).all()
    assert discounted.sum() > 0

    # Stage 3 is not time-discounted (discount factor = 1).
    stage3 = ead_out[ead_out["Staging"] == "Stage 3"]
    assert not stage3.empty
    assert (
        (stage3["Discounted ECL"].astype(float) - stage3["Credit Loss"].astype(float)).abs()
        < 1e-6
    ).all()

    # Stage 1 uses a 12-month horizon (≤ 13 snapshots including t0).
    stage1 = ead_out[ead_out["Loan ID"] == "LN-S1"]
    assert 1 <= len(stage1) <= 13
