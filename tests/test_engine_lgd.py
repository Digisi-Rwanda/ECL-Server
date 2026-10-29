"""LGD engine unit tests (spec Step 1)."""

import pandas as pd
import pytest

from app.engine.ecl_rules import prepare_inputs
from app.engine.lgd_engine import compute_lgd


def test_shared_customer_collateral_is_split_by_exposure_share() -> None:
    exposure = pd.DataFrame(
        {
            "Loan ID": ["L1", "L2"],
            "Customer ID": ["C1", "C1"],
            "SEGMENT": ["Retail", "Retail"],
            "Reporting Date": ["2025-03-31", "2025-03-31"],
            "First Payment Date": ["2024-01-31", "2024-01-31"],
            "Maturity Date": ["2027-01-31", "2027-01-31"],
            "Staging": ["Stage 1", "Stage 1"],
            "Outstanding Amount": [60_000.0, 40_000.0],
            "Effective Interest Rate (EIR)": [0.12, 0.12],
        }
    )
    collateral = pd.DataFrame(
        {
            "Customer ID": ["C1", "C1"],
            "Loan ID": ["L1", "L2"],
            "Outstanding Amount": [60_000.0, 40_000.0],
            "Effective Interest Rate (EIR)": [0.12, 0.12],
            "Property": [500_000.0, 500_000.0],
        }
    )
    config = [{"name": "Property", "haircut": 0.2, "time_to_realize": 1.5}]
    result = compute_lgd(prepare_inputs(exposure, collateral, config)).set_index("Loan ID")
    assert result["Loan Proportion"].tolist() == [0.6, 0.4]
    assert result["Allocated Property"].sum() == pytest.approx(500_000)
    assert result.loc["L1", "Discounted Collateral"] == pytest.approx(300_000 * 0.8 * 1.12**-1.5)
