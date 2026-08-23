"""EAD (Exposure at Default) and ECL computation engine."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import numpy_financial as npf
import pandas as pd

_STAGING_ALIASES = ("Staging", "Staging (Stage)")
_EIR_ALIASES = ("Effective Interest Rate", "Effective Interest Rate (EIR)", "EIR")
_FREQ_ALIASES = ("Repayment Frequency", "Payment Frequency")
_LGD_SUM_COL = "Sum of Discounted Collaterals per Loan ID"
_LGD_SUM_SHORT = "Sum of Discounted Collaterals"
_MISSED_EPS = 1e-9

# ECL formula context: Expected Credit Loss = EAD x PD x LGD, discounted to
# present value. This module derives EAD (via loan-balance amortization),
# combines it with PD (from pd_engine.py) and collateral (from
# lgd_engine.py) to get LGD, and computes the final Credit Loss /
# Discounted ECL. See compute_ead() below, particularly the "Credit Loss"
# and "Discounted ECL" lines, for the core formula.


def _resolve_column(df: pd.DataFrame, names: tuple[str, ...], default: str) -> str:
    for name in names:
        if name in df.columns:
            return name
    return default


def _to_decimal(value: float | int | Decimal) -> Decimal:
    return Decimal(str(value))


def _months_between(start: date | pd.Timestamp, end: date | pd.Timestamp) -> int:
    # Math: months = (end.year - start.year) x 12 + (end.month - start.month)
    # Whole-calendar-month difference between two dates (ignores day-of-month).
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    return (end_ts.year - start_ts.year) * 12 + (end_ts.month - start_ts.month)


def _period_instalment(
    outstanding: float,
    eir: float,
    remaining_months: int,
    freq: str = "MTH",
) -> float:
    """Compute the periodic instalment using numpy_financial PMT.

    For MTH: monthly rate = eir/12, periods = remaining_months.
    For QTR: quarterly rate = eir/4, periods = remaining_months // 3.
    Monthly accrual in the balance walk always uses eir/12 regardless of freq.
    """
    # Math: PMT = P x r / (1 - (1 + r)^(-n))
    #   P = outstanding principal, r = rate per period, n = periods remaining
    #   (PMT = P / n when r == 0)
    # The standard fixed-instalment loan amortization payment formula --
    # the same arithmetic behind any mortgage/loan calculator.
    if freq == "QTR":
        periods = max(1, remaining_months // 3)
        period_rate = eir / 4.0
        if period_rate == 0.0:
            return outstanding / periods if periods else 0.0
        return float(-npf.pmt(period_rate, periods, outstanding))

    # Default: MTH
    if remaining_months <= 0:
        return 0.0
    monthly_rate = eir / 12.0
    if monthly_rate == 0.0:
        return outstanding / remaining_months
    return float(-npf.pmt(monthly_rate, remaining_months, outstanding))


def _count_missed_periods(
    row_idx: int,
    opening_balances: list[float],
    monthly_rate: float,
    instalment: float,
    freq: str = "MTH",
) -> int:
    """Count missed payment periods among the last 3 payment events.

    For MTH: each row is a payment month — look back 3 rows.
    For QTR: payments fall on rows where (row_idx+1) % 3 == 0 — look back at
    the last 3 prior payment rows.
    """
    # Math: for each of the last 3 payment checkpoints i,
    #   flag "missed" if Opening_i x (1 + monthly_rate) < instalment
    # This is a proxy built from the projected balance trajectory itself,
    # not from an actual payment-received/not-received record in the data.
    if instalment <= 0:
        return 0

    if freq == "QTR":
        payment_rows = [i for i in range(row_idx) if (i + 1) % 3 == 0]
        lookback = payment_rows[-3:]
    else:
        lookback = range(max(0, row_idx - 3), row_idx)

    missed = 0
    for prior_idx in lookback:
        opening = opening_balances[prior_idx]
        balance_after_interest = opening * (1.0 + monthly_rate)
        if balance_after_interest + _MISSED_EPS < instalment:
            missed += 1
    return min(missed, 3)


def _walk_loan_balances(group: pd.DataFrame) -> pd.DataFrame:
    group = group.sort_values("Snapshot Date", kind="mergesort").copy()
    outstanding = float(group["Outstanding Amount"].iloc[0])
    eir = float(group["EIR"].iloc[0])
    monthly_rate = eir / 12.0
    instalment = float(group["Monthly Instalment"].iloc[0])
    freq = str(group["_freq"].iloc[0]) if "_freq" in group.columns else "MTH"

    balances_after_repayment: list[float] = []
    balances_after_missed: list[float] = []
    opening_balances: list[float] = []

    for row_idx in range(len(group)):
        if row_idx == 0:
            opening = outstanding
        else:
            opening = balances_after_repayment[row_idx - 1]

        opening_balances.append(opening)

        # For QTR loans, apply the instalment only on payment months (every 3rd row).
        is_payment_month = (freq != "QTR") or ((row_idx + 1) % 3 == 0)
        effective_instalment = instalment if is_payment_month else 0.0

        # Math: BalanceAfterRepayment_t = max(0, Opening_t x (1+r) - PMT_t)
        after_repayment = opening * (1.0 + monthly_rate) - effective_instalment
        if after_repayment < 0:
            after_repayment = 0.0
        balances_after_repayment.append(after_repayment)

        missed_count = _count_missed_periods(
            row_idx, opening_balances, monthly_rate, instalment, freq
        )
        # Math: EAD_t = BalanceAfterRepayment_t x (1+r)^(MissedCount_t)
        # Extra interest compounded on top for recently-missed instalments;
        # this final value is the model's Exposure at Default for period t.
        after_missed = after_repayment * ((1.0 + monthly_rate) ** missed_count)
        balances_after_missed.append(after_missed)

    group["Balance After Repayment"] = balances_after_repayment
    group["Balance After Missed Payment"] = balances_after_missed
    group["Period to be Discounted"] = range(len(group))
    return group


def _expand_snapshots(merged: pd.DataFrame) -> pd.DataFrame:
    """Vectorised snapshot date expansion — replaces iterrows() loop.

    Stage 3 → 1 snapshot per loan (reporting date).
    Stage 1 → up to 13 snapshots (offsets 0–12, clipped at maturity).
    Stage 2 → 1 snapshot per month from reporting date to maturity.
    Unknown staging → treated as Stage 3 (single snapshot).
    """
    # Rule (not a formula): IFRS9 requires a 12-month loss horizon for
    # Stage 1 (performing) loans, and a lifetime (to-maturity) horizon for
    # Stage 2/3 loans. This determines how many future snapshot months get
    # a Credit Loss / Discounted ECL computed further down in compute_ead().
    rep_dates = pd.to_datetime(merged["Reporting Date"])
    mat_dates = pd.to_datetime(merged["Maturity Date"])
    merged = merged.copy()
    merged["_rep_date"] = rep_dates
    merged["_mat_date"] = mat_dates

    parts: list[pd.DataFrame] = []

    # Stage 3 — trivial: one snapshot per loan.
    s3 = merged[merged["Staging"] == "Stage 3"].copy()
    if not s3.empty:
        s3["Snapshot Date"] = s3["_rep_date"]
        parts.append(s3)

    # Stage 1 — fixed 13 offsets, clipped at maturity.
    s1 = merged[merged["Staging"] == "Stage 1"].reset_index(drop=True)
    if not s1.empty:
        s1_frames = [
            s1.copy().assign(
                **{"Snapshot Date": s1["_rep_date"] + pd.DateOffset(months=i)}
            )
            for i in range(13)
        ]
        s1_exp = pd.concat(s1_frames, ignore_index=True)
        s1_exp["Snapshot Date"] = pd.to_datetime(s1_exp["Snapshot Date"])
        s1_exp = s1_exp[s1_exp["Snapshot Date"] <= s1_exp["_mat_date"]]
        parts.append(s1_exp)

    # Stage 2 — variable horizon per loan; use np.repeat for row expansion.
    s2 = merged[merged["Staging"] == "Stage 2"].reset_index(drop=True)
    if not s2.empty:
        s2_rep = s2["_rep_date"]
        s2_mat = s2["_mat_date"]
        max_months = [max(0, _months_between(r, m)) for r, m in zip(s2_rep, s2_mat)]
        lens = [n + 1 for n in max_months]
        row_idx_arr = np.repeat(np.arange(len(s2)), lens)
        offset_arr = np.concatenate([np.arange(n) for n in lens])
        s2_exp = s2.iloc[row_idx_arr].reset_index(drop=True)
        s2_exp["Snapshot Date"] = [
            s2_rep.iloc[row_idx_arr[i]] + pd.DateOffset(months=int(offset_arr[i]))
            for i in range(len(s2_exp))
        ]
        s2_exp["Snapshot Date"] = pd.to_datetime(s2_exp["Snapshot Date"])
        s2_exp = s2_exp[s2_exp["Snapshot Date"] <= s2_exp["_mat_date"]]
        parts.append(s2_exp)

    # Unknown staging — single snapshot, emit no warning here (validator catches this).
    other = merged[~merged["Staging"].isin(["Stage 1", "Stage 2", "Stage 3"])].copy()
    if not other.empty:
        other["Snapshot Date"] = other["_rep_date"]
        parts.append(other)

    if not parts:
        return pd.DataFrame()

    result = pd.concat(parts, ignore_index=True)
    result = result.drop(columns=["_rep_date", "_mat_date"], errors="ignore")
    return result


def compute_ead(
    ead_df: pd.DataFrame,
    pd_df: pd.DataFrame,
    lgd_df: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str]]:
    """Run the EAD/ECL computation pipeline."""
    warnings: list[str] = []
    working = ead_df.copy()

    staging_col = _resolve_column(working, _STAGING_ALIASES, "Staging")
    if staging_col != "Staging":
        working = working.rename(columns={staging_col: "Staging"})

    eir_col = _resolve_column(working, _EIR_ALIASES, "Effective Interest Rate")
    if eir_col != "Effective Interest Rate":
        working = working.rename(columns={eir_col: "Effective Interest Rate"})

    # Resolve repayment frequency — default MTH if column is absent.
    freq_col = _resolve_column(working, _FREQ_ALIASES, "")
    if freq_col:
        working["_freq"] = working[freq_col].astype(str).str.upper().str.strip()
    else:
        working["_freq"] = "MTH"
    working["_freq"] = working["_freq"].where(
        working["_freq"].isin({"MTH", "QTR"}), other="MTH"
    )

    working = working.sort_values(["Loan ID"], kind="mergesort").reset_index(drop=True)

    lgd_subset = lgd_df[["Loan ID"]].copy()
    if "EIR" in lgd_df.columns:
        lgd_subset["EIR"] = lgd_df["EIR"]
    if _LGD_SUM_COL in lgd_df.columns:
        lgd_subset[_LGD_SUM_SHORT] = lgd_df[_LGD_SUM_COL]
    elif _LGD_SUM_SHORT in lgd_df.columns:
        lgd_subset[_LGD_SUM_SHORT] = lgd_df[_LGD_SUM_SHORT]
    else:
        lgd_subset[_LGD_SUM_SHORT] = 0.0
    lgd_subset = lgd_subset.drop_duplicates(subset=["Loan ID"], keep="first")

    merged = working.merge(lgd_subset, on="Loan ID", how="left")

    missing_lgd_mask = merged[_LGD_SUM_SHORT].isna()
    for loan_id in sorted(merged.loc[missing_lgd_mask, "Loan ID"].astype(str).unique()):
        warnings.append(
            f"EC-06: Loan {loan_id} in EAD but not in LGD — collateral treated as 0"
        )
    merged[_LGD_SUM_SHORT] = merged[_LGD_SUM_SHORT].fillna(0.0)

    if "EIR" in merged.columns:
        merged["EIR"] = merged["EIR"].fillna(merged["Effective Interest Rate"])
    else:
        merged["EIR"] = merged["Effective Interest Rate"]

    # Step 2 — generate snapshot rows (vectorised per staging group).
    snapshots = _expand_snapshots(merged)

    if snapshots.empty:
        empty = pd.DataFrame(
            columns=[
                "Loan ID",
                "Customer ID",
                "SEGMENT",
                "Staging",
                "Snapshot Date",
                "Period Since Origination",
                "Monthly Instalment",
                "Balance After Repayment",
                "Balance After Missed Payment",
                "Period to be Discounted",
                "Marginal PD",
                "Cure Rate",
                "LGW",
                "LGD",
                "Credit Loss",
                "Discounted ECL",
            ]
        )
        return empty, warnings

    snapshots["Reporting Date"] = pd.to_datetime(snapshots["Reporting Date"])
    snapshots["First Payment Date"] = pd.to_datetime(snapshots["First Payment Date"])
    snapshots["Adjusted Maturity Date"] = pd.to_datetime(snapshots["Adjusted Maturity Date"])
    snapshots["Snapshot Date"] = pd.to_datetime(snapshots["Snapshot Date"])

    # Step 3 — period since origination.
    # +1 matches PD.ipynb's calculate_period_since_origination(): a loan
    # snapshotted in its origination month (0 calendar months elapsed) is
    # treated as "month 1" to align with PD's 1-indexed Month column.
    snapshots["Period Since Origination"] = [
        _months_between(first_payment, snapshot) + 1
        for first_payment, snapshot in zip(
            snapshots["First Payment Date"],
            snapshots["Snapshot Date"],
            strict=True,
        )
    ]

    # Step 4 — periodic instalment (once per loan, frequency-aware).
    loan_terms = snapshots.drop_duplicates(subset=["Loan ID"], keep="first").copy()
    instalments: dict[str, float] = {}
    for _, loan in loan_terms.iterrows():
        elapsed = _months_between(loan["First Payment Date"], loan["Reporting Date"])
        total_term = _months_between(loan["First Payment Date"], loan["Adjusted Maturity Date"])
        remaining = total_term - elapsed
        freq = str(loan.get("_freq", "MTH"))
        instalments[str(loan["Loan ID"])] = _period_instalment(
            float(loan["Outstanding Amount"]),
            float(loan["EIR"]),
            remaining,
            freq,
        )
    snapshots["Monthly Instalment"] = snapshots["Loan ID"].astype(str).map(instalments)

    # Steps 5–7 — balances and discount period via sequential group walk.
    # Use a synthetic integer key so "Loan ID" is preserved as a regular column
    # (pandas 3.0 excludes the groupby key column from group DataFrames passed to apply).
    loan_ids_sorted = sorted(snapshots["Loan ID"].astype(str).unique())
    loan_id_to_grp = {lid: i for i, lid in enumerate(loan_ids_sorted)}
    snapshots["_grp"] = snapshots["Loan ID"].astype(str).map(loan_id_to_grp)
    snapshots = (
        snapshots.groupby("_grp", sort=True, group_keys=False)
        .apply(_walk_loan_balances)
        .reset_index(drop=True)
    )
    snapshots = snapshots.drop(columns=["_grp"], errors="ignore")

    # Step 8 — join PD results.
    pd_lookup = pd_df[
        [
            "SEGMENT",
            "Month",
            "Transition",
            "Marginal_PD",
            "Cure_Rate",
        ]
    ].copy()
    pd_lookup = pd_lookup.rename(
        columns={
            "Month": "Period Since Origination",
            "Transition": "Staging",
        }
    )

    snapshots = snapshots.merge(
        pd_lookup,
        on=["SEGMENT", "Period Since Origination", "Staging"],
        how="left",
    )

    # EC-08 — hard fail if an entire segment has no PD data at all.
    all_segs = set(snapshots["SEGMENT"].astype(str).unique())
    segs_with_pd = set(
        snapshots.loc[snapshots["Marginal_PD"].notna(), "SEGMENT"].astype(str).unique()
    )
    missing_segs = sorted(all_segs - segs_with_pd)
    if missing_segs:
        raise ValueError(
            f"EC-08: No PD history for segment(s): {', '.join(missing_segs)}. "
            "All segments present in the EAD file must have corresponding PD data. "
            "Add these segments to the PD file and re-run."
        )
    # For snapshots beyond the PD horizon (months > 299), treat PD/cure as 0.
    snapshots["Marginal_PD"] = snapshots["Marginal_PD"].fillna(0.0)
    snapshots["Cure_Rate"] = snapshots["Cure_Rate"].fillna(0.0)

    # Steps 9–11 — LGW, LGD, credit loss, discounted ECL.
    bal_after_missed = snapshots["Balance After Missed Payment"].astype(float)
    collateral = snapshots[_LGD_SUM_SHORT].astype(float)

    # Math: LGW_t = max(1 - Collateral / EAD_t, 0)   [0 when EAD_t <= 0]
    # Raw loss rate before accounting for the chance of curing: 100% minus
    # however much of the balance the discounted collateral would cover.
    lgw = np.where(
        bal_after_missed <= 0,
        0.0,
        np.maximum(1.0 - collateral / bal_after_missed, 0.0),
    )
    snapshots["LGW"] = lgw

    cure_rate = snapshots["Cure_Rate"].astype(float)
    # Math: LGD_t = max(0, LGW_t x (1 - CureRate))
    snapshots["LGD"] = np.maximum(0.0, snapshots["LGW"] * (1.0 - cure_rate))

    marginal_pd = snapshots["Marginal_PD"].astype(float)
    # Math: CreditLoss_t = EAD_t x PD_t x LGD_t
    # The core IFRS9 Expected Credit Loss formula for one loan, one period:
    # Expected Loss = Exposure at Default x Probability of Default x Loss
    # Given Default. Everything computed above in this module and in
    # pd_engine.py / lgd_engine.py exists to produce these three numbers.
    snapshots["Credit Loss"] = bal_after_missed * marginal_pd * snapshots["LGD"].astype(float)

    monthly_rate = snapshots["EIR"].astype(float) / 12.0
    period_to_discount = snapshots["Period to be Discounted"].astype(int)
    # Math: DiscountFactor_t = (1 + r)^(-t)   [r = EIR/12, t = months out]
    # Stage 3 (already in default) is not discounted at all, matching
    # PD.ipynb's calculate_discounted_ecl(), which returns raw Credit Loss
    # for Stage 3 and only discounts Stage 1/2.
    is_stage_3 = snapshots["Staging"] == "Stage 3"
    discount_factor = np.where(
        is_stage_3,
        1.0,
        np.where(
            period_to_discount > 0,
            (1.0 + monthly_rate) ** (-period_to_discount),
            1.0,
        ),
    )
    # Math: DiscountedECL_t = CreditLoss_t x DiscountFactor_t
    # Present-value discount of the period's expected loss back to today, at
    # the loan's own effective interest rate. This is the figure that is
    # actually stored and reported per loan per snapshot period; summed
    # across a loan's full horizon it gives that loan's Total ECL.
    snapshots["Discounted ECL"] = snapshots["Credit Loss"] * discount_factor

    decimal_cols = [
        "Monthly Instalment",
        "Balance After Repayment",
        "Balance After Missed Payment",
        "Credit Loss",
        "Discounted ECL",
        _LGD_SUM_SHORT,
    ]
    for col in decimal_cols:
        if col in snapshots.columns:
            snapshots[col] = snapshots[col].apply(_to_decimal)

    output_cols = [
        "Loan ID",
        "Customer ID",
        "SEGMENT",
        "Staging",
        "Snapshot Date",
        "Period Since Origination",
        "Period to be Discounted",
        "Monthly Instalment",
        "Balance After Repayment",
        "Balance After Missed Payment",
        _LGD_SUM_SHORT,
        "Marginal PD",
        "Cure Rate",
        "LGW",
        "LGD",
        "Credit Loss",
        "Discounted ECL",
    ]
    # Rename internal PD columns to display names for output.
    if "Marginal_PD" in snapshots.columns and "Marginal PD" not in snapshots.columns:
        snapshots = snapshots.rename(
            columns={"Marginal_PD": "Marginal PD", "Cure_Rate": "Cure Rate"}
        )

    output = snapshots[output_cols].sort_values(
        ["Loan ID", "Snapshot Date"],
        kind="mergesort",
    ).reset_index(drop=True)

    return output, warnings
