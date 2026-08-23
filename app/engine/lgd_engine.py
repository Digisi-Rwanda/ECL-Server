"""LGD (Loss Given Default) computation engine."""

# ECL formula context: Expected Credit Loss = EAD x PD x LGD (see ead_engine.py).
# This module computes the discounted collateral value backing each loan.
# ead_engine.py later turns that into the "LGD" term via:
#   LGD = max(1 - collateral / EAD, 0) x (1 - cure_rate)

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd

_EIR_ALIASES = ("Effective Interest Rate (EIR)", "Effective Interest Rate", "EIR")
_SUM_COL = "Sum of Discounted Collaterals per Loan ID"


def _resolve_eir_column(df: pd.DataFrame) -> str:
    for name in _EIR_ALIASES:
        if name in df.columns:
            return name
    return "Effective Interest Rate (EIR)"


def _to_decimal(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value))


def compute_lgd(df: pd.DataFrame, collateral_config: list[dict[str, Any]]) -> pd.DataFrame:
    """Run the 5-step LGD computation pipeline."""
    working = df.copy()
    eir_col = _resolve_eir_column(working)
    if eir_col != "EIR":
        working = working.rename(columns={eir_col: "EIR"})

    working = working.sort_values(["Customer ID", "Loan ID"], kind="mergesort").reset_index(drop=True)

    config_by_name = {item["name"]: item for item in collateral_config}
    collateral_names = sorted(config_by_name.keys(), key=str)

    # Step 1 — total loan amount per customer.
    # Math: T_c = sum(Outstanding Amount) across every loan of customer c
    customer_totals = (
        working.groupby("Customer ID", sort=True)["Outstanding Amount"]
        .sum()
        .rename("Total Loan Amount per Customer")
    )
    working = working.merge(customer_totals, on="Customer ID", how="left")

    # Step 2 — proportion.
    # Math: w_l = Outstanding Amount_l / T_c
    # This loan's share of the customer's total debt (0 if T_c == 0).
    working["Proportion"] = working["Outstanding Amount"] / working[
        "Total Loan Amount per Customer"
    ].replace(0, pd.NA)
    working["Proportion"] = working["Proportion"].fillna(0.0)

    discounted_sum = pd.Series(0.0, index=working.index, dtype="float64")

    for collateral_name in collateral_names:
        cfg = config_by_name[collateral_name]
        haircut = float(cfg["haircut"])
        time_to_realize = int(cfg["time_to_realize"])

        if collateral_name not in working.columns:
            working[collateral_name] = 0.0

        proportional_col = f"Proportional {collateral_name}"
        discounted_col = f"Discounted {collateral_name}"

        # Step 3 — proportional collateral.
        # Math: ProportionalCollateral_{l,k} = w_l x CollateralValue_{c,k}
        # Splits a customer's shared collateral of type k across their loans
        # in proportion to each loan's share of the total debt.
        working[proportional_col] = working["Proportion"] * working[collateral_name].fillna(0.0)

        # Step 4 — haircut and time discount.
        # Math: DiscountFactor_k = (1 - haircut_k) x (1 + EIR)^(-time_to_realize_k)
        # Standard collateral valuation: a haircut for forced-sale risk, then
        # a present-value time discount (PV = FV / (1+r)^n) for however long
        # repossession/realization is expected to take.
        discount_factor = (1.0 - haircut) * (1.0 + working["EIR"]) ** (-time_to_realize)
        # Math: DiscountedCollateral_{l,k} = ProportionalCollateral_{l,k} x DiscountFactor_k
        working[discounted_col] = working[proportional_col] * discount_factor
        discounted_sum += working[discounted_col]

    # Step 5 — sum discounted collaterals per loan.
    # Math: TotalDiscountedCollateral_l = sum_k(DiscountedCollateral_{l,k})
    working[_SUM_COL] = discounted_sum.apply(_to_decimal)

    output_cols = [
        "Loan ID",
        "Customer ID",
        "Outstanding Amount",
        "EIR",
        "Total Loan Amount per Customer",
        "Proportion",
        _SUM_COL,
    ]
    for collateral_name in collateral_names:
        output_cols.extend([f"Proportional {collateral_name}", f"Discounted {collateral_name}"])

    return working[output_cols].sort_values(["Loan ID"], kind="mergesort").reset_index(drop=True)
