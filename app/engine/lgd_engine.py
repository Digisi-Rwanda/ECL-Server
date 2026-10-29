"""Step 1 – Discounted Collateral per Loan (spec v1.0, section 2).

ECL = EAD x Marginal PD x LGD. This module produces the collateral side of
LGD; ead_engine turns it into LGD = max(0, 1 - Collateral / EAD) x (1 - Cure).
Inputs must already have passed `ecl_rules.prepare_inputs`.
"""

from __future__ import annotations

import pandas as pd

from app.engine.ecl_rules import CUSTOMER_ID, LOAN_ID, OUTSTANDING, PreparedInputs

TOTAL_CUSTOMER_EXPOSURE = "Total Customer Exposure"
LOAN_PROPORTION = "Loan Proportion"
DISCOUNTED_COLLATERAL = "Discounted Collateral"
COLLATERAL_EIR = "Collateral EIR"


def allocated_col(collateral_type: str) -> str:
    return f"Allocated {collateral_type}"


def discounted_col(collateral_type: str) -> str:
    return f"Discounted {collateral_type}"


def discount_factor(haircut: float, eir: pd.Series, years: float) -> pd.Series:
    # (1 - Haircut) x (1 + EIR)^(-Time to Realization): forced-sale haircut, then
    # present value over the years it takes to realise, at the loan's annual EIR.
    return (1.0 - haircut) * (1.0 + eir) ** (-years)


def compute_lgd(prepared: PreparedInputs) -> pd.DataFrame:
    """One row per included loan with every Step 1 column, sorted by Loan ID."""
    working = prepared.exposure[[LOAN_ID, CUSTOMER_ID, OUTSTANDING]].copy()

    # Total Customer Exposure = SUM(Outstanding Amount) GROUP BY Customer ID, over the
    # Exposure Dataset, so loans with no collateral row still count.
    working[TOTAL_CUSTOMER_EXPOSURE] = working.groupby(CUSTOMER_ID)[OUTSTANDING].transform("sum")
    # Loan Proportion = Outstanding Amount / Total Customer Exposure (0 when the total is 0).
    total = working[TOTAL_CUSTOMER_EXPOSURE]
    working[LOAN_PROPORTION] = (working[OUTSTANDING] / total.where(total != 0)).fillna(0.0)

    coll = prepared.collateral.drop(columns=[CUSTOMER_ID])
    working = working.merge(coll, on=LOAN_ID, how="left")
    has_row = working[COLLATERAL_EIR].notna()
    working[COLLATERAL_EIR] = working[COLLATERAL_EIR].fillna(0.0)

    discounted_total = pd.Series(0.0, index=working.index)
    for t in prepared.collateral_types:
        # Step 2: a loan with no collateral record has collateral 0.
        working[t] = working[t].where(has_row, 0.0).fillna(0.0)
        # Allocated Collateral[type] = Loan Proportion x Collateral[type]
        working[allocated_col(t)] = working[LOAN_PROPORTION] * working[t]
        params = prepared.parameters.get(t)
        if params is None:
            working[discounted_col(t)] = 0.0
        else:
            working[discounted_col(t)] = working[allocated_col(t)] * discount_factor(
                params["haircut"], working[COLLATERAL_EIR], params["time_to_realize"]
            )
        discounted_total += working[discounted_col(t)]
    working[DISCOUNTED_COLLATERAL] = discounted_total

    columns = [
        LOAN_ID,
        CUSTOMER_ID,
        OUTSTANDING,
        COLLATERAL_EIR,
        TOTAL_CUSTOMER_EXPOSURE,
        LOAN_PROPORTION,
    ]
    for t in prepared.collateral_types:
        columns += [t, allocated_col(t), discounted_col(t)]
    columns.append(DISCOUNTED_COLLATERAL)
    return working[columns].sort_values(LOAN_ID, kind="mergesort").reset_index(drop=True)
