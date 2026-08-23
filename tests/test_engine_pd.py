"""PD engine unit tests."""

import pandas as pd

from app.engine.pd_engine import compute_pd


def test_compute_pd_basic() -> None:
    df = pd.DataFrame(
        {
            "Loan ID": ["L1", "L1", "L2", "L2"],
            "Reporting Month": pd.to_datetime(
                ["2024-01-31", "2024-02-29", "2024-01-31", "2024-02-29"]
            ),
            "Staging": ["Stage 1", "Stage 1", "Stage 2", "Stage 3"],
            "Loan Amount": [100_000.0, 95_000.0, 50_000.0, 48_000.0],
            "SEGMENT": ["Retail", "Retail", "Retail", "Retail"],
        }
    )
    output, cure_rates, _intermediate = compute_pd(df)
    assert not output.empty
    assert "Marginal_PD" in output.columns
    assert "Retail" in cure_rates
    assert output["Month"].max() == 300
