"""Steps 2-10 of the ECL specification (v1.0): exposure rundown, PD join, loss.

Expected Credit Loss for one loan and one month is
    Credit Loss = EAD x Marginal PD x LGD,
discounted back to the Reporting Date at the loan's EIR. This module builds
EAD (Steps 3-7), joins the PD curve (Step 8) and applies LGW/LGD and the
discount (Steps 9-10). Inputs come from `ecl_rules.prepare_inputs` and
`lgd_engine.compute_lgd`, so every row here belongs to an included loan.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.engine.ecl_rules import (
    CAT_PD,
    CUSTOMER_ID,
    DS_PD,
    EIR,
    FIRST_PAYMENT_DATE,
    LOAN_ID,
    MATURITY_DATE,
    OUTSTANDING,
    REPORTING_DATE,
    SEGMENT,
    STAGE,
    ExceptionLine,
)
from app.engine.lgd_engine import DISCOUNTED_COLLATERAL

ADJUSTED_MATURITY = "Adjusted Maturity Date"
TREATMENT = "Treatment"
SNAPSHOT_DATE = "Snapshot Date"
PERIOD_SINCE_ORIGINATION = "Period Since Origination"
MONTHLY_INSTALMENT = "Monthly Instalment"
BAL_AFTER_REPAYMENT = "Balance After Repayment"
BAL_AFTER_MISSED = "Balance After Missed Payment"
PERIOD_TO_DISCOUNT = "Period to Be Discounted"
MARGINAL_PD = "Marginal PD"
CURE_RATE = "Cure Rate"
PD_RULE = "PD Rule"
LGW = "LGW"
LGD = "LGD"
CREDIT_LOSS = "Credit Loss"
DISCOUNTED_ECL = "Discounted ECL"

MAX_MISSED_PAYMENTS = 3

# Treatment labels, also shown in the UI and the workbooks.
T_STAGE1 = "Stage 1 · 12 months"
T_STAGE2 = "Stage 2 · lifetime"
T_STAGE3 = "Stage 3 · single row"
T_MATURED = "Matured · single row"
T_ZERO_RATE = "Zero rate · single row"

# PD Rule labels (Step 8).
R_NO_PD_ROW0 = "Reporting-date row: no PD"
R_STAGE3 = "Stage 3: Marginal PD = 100%"
R_CURVE = "PD curve"
R_LAST_MONTH = "Beyond curve: last available Month"
R_UNMATCHED = "No PD match"

ROW_COLUMNS = [
    LOAN_ID,
    CUSTOMER_ID,
    SEGMENT,
    STAGE,
    TREATMENT,
    REPORTING_DATE,
    FIRST_PAYMENT_DATE,
    MATURITY_DATE,
    ADJUSTED_MATURITY,
    OUTSTANDING,
    EIR,
    DISCOUNTED_COLLATERAL,
    SNAPSHOT_DATE,
    PERIOD_SINCE_ORIGINATION,
    MONTHLY_INSTALMENT,
    BAL_AFTER_REPAYMENT,
    BAL_AFTER_MISSED,
    PERIOD_TO_DISCOUNT,
    MARGINAL_PD,
    CURE_RATE,
    PD_RULE,
    LGW,
    LGD,
    CREDIT_LOSS,
    DISCOUNTED_ECL,
]


def month_index(dates: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """Calendar months since year 0: months_between(A, B) = index(B) - index(A)."""
    d = pd.DatetimeIndex(dates)
    return np.asarray(d.year * 12 + (d.month - 1), dtype=np.int64)


def add_months(start: pd.Series, months: np.ndarray) -> pd.DatetimeIndex:
    """Same day n months later, clamped to the month's last day (31 Jan + 1 = 28/29 Feb)."""
    start = pd.DatetimeIndex(start)
    total = start.year.to_numpy() * 12 + (start.month.to_numpy() - 1) + months
    first = pd.to_datetime(pd.DataFrame({"year": total // 12, "month": total % 12 + 1, "day": 1}))
    day = np.minimum(start.day.to_numpy(), first.dt.days_in_month.to_numpy())
    return pd.DatetimeIndex(first + pd.to_timedelta(day - 1, unit="D"))


def instalment(outstanding: float, monthly_rate: float, remaining_months: int) -> float:
    # Level payment: P x r / (1 - (1 + r)^(-n)), the standard amortisation formula.
    return outstanding * monthly_rate / (1.0 - (1.0 + monthly_rate) ** (-remaining_months))


def project_exposure(exposure: pd.DataFrame, lgd: pd.DataFrame) -> pd.DataFrame:
    """Steps 2-7: snapshot rows, period since origination, instalment, balances."""
    loans = exposure.sort_values(LOAN_ID, kind="mergesort").reset_index(drop=True)
    # Step 2: left-join Discounted Collateral; no collateral record -> 0.
    collateral = lgd.set_index(LOAN_ID)[DISCOUNTED_COLLATERAL]
    loans[DISCOUNTED_COLLATERAL] = loans[LOAN_ID].map(collateral).fillna(0.0).astype(float)
    if loans.empty:
        return pd.DataFrame(columns=ROW_COLUMNS[: ROW_COLUMNS.index(PERIOD_TO_DISCOUNT) + 1])

    # Adjusted Maturity Date = last day of the Maturity Date's month.
    loans[ADJUSTED_MATURITY] = loans[MATURITY_DATE] + pd.offsets.MonthEnd(0)
    rep_m = month_index(loans[REPORTING_DATE])
    fp_m = month_index(loans[FIRST_PAYMENT_DATE])
    adj_m = month_index(loans[ADJUSTED_MATURITY])
    # Remaining months = Total term - Elapsed = months(FP, AdjMat) - months(FP, Reporting).
    remaining = adj_m - rep_m
    # Matured: Adjusted Maturity on/before the Reporting Date or in its month.
    matured = remaining <= 0
    stage = loans[STAGE].to_numpy()
    eir = loans[EIR].to_numpy(dtype=float)
    zero_rate = eir == 0
    single = (stage == "Stage 3") | matured | zero_rate

    loans[TREATMENT] = np.select(
        [stage == "Stage 3", matured, zero_rate, stage == "Stage 1"],
        [T_STAGE3, T_MATURED, T_ZERO_RATE, T_STAGE1],
        default=T_STAGE2,
    )
    # Rows n = 0..max_n. AdjMat is a month end, so Reporting + n months <= AdjMat
    # exactly when n <= remaining.
    max_n = np.where(single, 0, np.where(stage == "Stage 1", np.minimum(12, remaining), remaining))

    monthly_rate = eir / 12.0
    outstanding = loans[OUTSTANDING].to_numpy(dtype=float)
    # Step 5: instalment once per loan, as at the Reporting Date.
    pmt = np.zeros(len(loans))
    for i in np.flatnonzero(~((stage == "Stage 3") | matured | (monthly_rate == 0))):
        pmt[i] = instalment(outstanding[i], monthly_rate[i], int(remaining[i]))
    loans[MONTHLY_INSTALMENT] = pmt

    lengths = max_n + 1
    loan_idx = np.repeat(np.arange(len(loans)), lengths)
    t = np.concatenate([np.arange(n) for n in lengths])
    rows = loans.iloc[loan_idx].reset_index(drop=True)
    rows[SNAPSHOT_DATE] = add_months(rows[REPORTING_DATE], t)
    # Step 4: months_between(First Payment Date, Snapshot Date), calendar months.
    rows[PERIOD_SINCE_ORIGINATION] = rep_m[loan_idx] + t - fp_m[loan_idx]
    rows[PERIOD_TO_DISCOUNT] = t

    # Step 6: first row = Outstanding; then max(0, previous x (1 + EIR/12) - instalment).
    g = 1.0 + monthly_rate[loan_idx]
    p = pmt[loan_idx]
    bal = np.empty(len(rows))
    for j in range(len(rows)):
        bal[j] = outstanding[loan_idx[j]] if t[j] == 0 else max(0.0, bal[j - 1] * g[j] - p[j])
    rows[BAL_AFTER_REPAYMENT] = bal
    # Step 7: k = min(t, 3) missed payments; Balance After Repayment[t-k] x (1 + EIR/12)^k.
    k = np.minimum(t, MAX_MISSED_PAYMENTS)
    rows[BAL_AFTER_MISSED] = bal[np.arange(len(rows)) - k] * g**k
    return rows


def attach_pd(
    rows: pd.DataFrame, pd_curve: pd.DataFrame
) -> tuple[pd.DataFrame, list[ExceptionLine]]:
    """Step 8: join Marginal PD and Cure Rate on (SEGMENT, Month, Transition)."""
    curve = pd_curve[["SEGMENT", "Month", "Transition", "Marginal_PD", "Cure_Rate"]].copy()
    # The PD Dataset is produced by pd_engine, which guarantees one row per key and
    # values in 0-1; a breach is an engine bug, not user data to report.
    if curve.duplicated(["SEGMENT", "Month", "Transition"]).any():
        raise ValueError("PD curve has duplicate (SEGMENT, Month, Transition) keys")
    if not curve[["Marginal_PD", "Cure_Rate"]].stack().between(0.0, 1.0).all():
        raise ValueError("PD curve has Marginal PD or Cure Rate outside 0-1")

    out = rows.copy()
    last_month = curve.groupby(["SEGMENT", "Transition"])["Month"].max()
    last = pd.MultiIndex.from_arrays([out[SEGMENT], out[STAGE]]).map(last_month.to_dict().get)
    last = pd.to_numeric(pd.Series(last, index=out.index), errors="coerce")
    pso = out[PERIOD_SINCE_ORIGINATION]
    beyond = last.notna() & (pso > last)
    lookup_month = pso.where(~beyond, last)

    keyed = curve.set_index(["SEGMENT", "Transition", "Month"])
    key = pd.MultiIndex.from_arrays([out[SEGMENT], out[STAGE], lookup_month.astype("Int64")])
    joined_pd = keyed["Marginal_PD"].reindex(key).to_numpy()
    cure_by_segment = curve.groupby("SEGMENT")["Cure_Rate"].first()
    out[CURE_RATE] = out[SEGMENT].map(cure_by_segment).astype(float)

    first_row = out[PERIOD_TO_DISCOUNT] == 0
    multi_row = out[TREATMENT].isin([T_STAGE1, T_STAGE2])
    is_stage3 = out[STAGE] == "Stage 3"
    matched = ~np.isnan(joined_pd)

    out[MARGINAL_PD] = np.select(
        [is_stage3, first_row & multi_row, matched],
        [1.0, 0.0, joined_pd],
        default=np.nan,
    )
    out[PD_RULE] = np.select(
        [is_stage3, first_row & multi_row, matched & beyond, matched],
        [R_STAGE3, R_NO_PD_ROW0, R_LAST_MONTH, R_CURVE],
        default=R_UNMATCHED,
    )

    unmatched = out[out[PD_RULE] == R_UNMATCHED]
    exceptions = [
        ExceptionLine(
            CAT_PD,
            DS_PD,
            str(r[LOAN_ID]),
            str(r[CUSTOMER_ID]),
            "SEGMENT / Month / Transition",
            f"{r[SEGMENT]} / {int(r[PERIOD_SINCE_ORIGINATION])} / {r[STAGE]}",
            f"No PD row for this key (snapshot {r[SNAPSHOT_DATE].date().isoformat()})",
            "None (reported only) — row carries no Credit Loss",
        )
        for _, r in unmatched.iterrows()
    ]
    return out, exceptions


def compute_losses(rows: pd.DataFrame) -> pd.DataFrame:
    """Steps 9-10: LGW, LGD, Credit Loss and Discounted ECL."""
    out = rows.copy()
    ead = out[BAL_AFTER_MISSED].astype(float)
    collateral = out[DISCOUNTED_COLLATERAL].astype(float)
    # LGW = 0 if EAD = 0 else max(0, 1 - Discounted Collateral / EAD)
    safe_ead = ead.where(ead != 0, 1.0)
    out[LGW] = np.where(ead == 0, 0.0, np.maximum(0.0, 1.0 - collateral / safe_ead))
    # LGD = LGW x (1 - Cure Rate)
    out[LGD] = out[LGW] * (1.0 - out[CURE_RATE].fillna(0.0))
    # Credit Loss = EAD x Marginal PD x LGD; an unmatched PD row carries none (reported in Step 8).
    out[CREDIT_LOSS] = ead * out[MARGINAL_PD].fillna(0.0) * out[LGD]
    # Discounted ECL = Credit Loss x (1 + EIR/12)^(-Period to Be Discounted), period 0 undiscounted.
    period = out[PERIOD_TO_DISCOUNT].astype(int)
    factor = np.where(period > 0, (1.0 + out[EIR].astype(float) / 12.0) ** (-period), 1.0)
    out[DISCOUNTED_ECL] = out[CREDIT_LOSS] * factor
    return out


def compute_rows(
    exposure: pd.DataFrame, lgd: pd.DataFrame, pd_curve: pd.DataFrame
) -> tuple[pd.DataFrame, list[ExceptionLine]]:
    """Row Detail for Steps 2-10, sorted by Loan ID then Snapshot Date."""
    rows = project_exposure(exposure, lgd)
    if rows.empty:
        return pd.DataFrame(columns=ROW_COLUMNS), []
    rows, exceptions = attach_pd(rows, pd_curve)
    rows = compute_losses(rows)
    rows = (
        rows[ROW_COLUMNS]
        .sort_values([LOAN_ID, SNAPSHOT_DATE], kind="mergesort")
        .reset_index(drop=True)
    )
    return rows, exceptions
