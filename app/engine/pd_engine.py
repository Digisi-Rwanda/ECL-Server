"""PD (Probability of Default) computation engine."""

# ECL formula context: Expected Credit Loss = EAD x PD x LGD (see ead_engine.py).
# This module computes the "PD" term via an IFRS9 stage-transition (Markov
# chain) matrix: build a 1-month transition matrix from observed loan
# movements, pool it across history, raise it to successive powers to
# project multi-month transition probabilities, then convert the resulting
# cumulative Stage-3 probability into a period-over-period marginal
# (hazard-rate) PD. See individual functions below for the exact formulas.
#
# The individual pipeline steps are exposed as public functions so the
# validate-time PD preview (pd_preview.py) can reuse the exact same
# arithmetic instead of re-implementing it. There is one implementation of
# each formula; the preview and the official computation cannot drift.

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from numpy.linalg import matrix_power

STAGES = ("Stage 1", "Stage 2", "Stage 3")
OFFBOOKS = "Offbooks"
NEXT_STAGES = (*STAGES, OFFBOOKS)
MAX_PD_MONTH = 300  # PD.ipynb projects P^1 .. P^300 (300 months), not 299
_DENOM_EPS = 1e-15

REPORTING_MONTH_ALIASES = ("Reporting Month", 'Reporting Month ("As At")', "As At")
_REPORTING_MONTH_ALIASES = REPORTING_MONTH_ALIASES

REQUIRED_PD_COLUMNS = ("Loan ID", "Staging", "Loan Amount", "SEGMENT")


def resolve_reporting_month_column(df: pd.DataFrame) -> str:
    for name in REPORTING_MONTH_ALIASES:
        if name in df.columns:
            return name
    return "Reporting Month"


_resolve_reporting_month_column = resolve_reporting_month_column


def pd_columns_present(df: pd.DataFrame) -> set[str]:
    """Which of the required PD columns this frame carries (month aliases collapsed)."""
    present = {column for column in REQUIRED_PD_COLUMNS if column in df.columns}
    if any(alias in df.columns for alias in REPORTING_MONTH_ALIASES):
        present.add("Reporting Month")
    return present


def is_pd_data_sheet(df: pd.DataFrame) -> bool:
    """True when a workbook sheet actually carries PD loan data.

    Workbooks routinely ship cover pages, notes, and lookup tabs. Concatenating
    those into the PD frame injects all-NaN rows that then read as missing Loan
    IDs. Validation and computation must agree on which sheets count, so both
    paths route through this one predicate.
    """
    return len(pd_columns_present(df)) == len(REQUIRED_PD_COLUMNS) + 1


def _matrix_to_dict(matrix: np.ndarray) -> dict[str, list[float]]:
    return {
        stage: [float(matrix[row_idx, col_idx]) for col_idx in range(3)]
        for row_idx, stage in enumerate(STAGES)
    }


def prepare_pd_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Step 0-1 — canonicalise the reporting-month column, parse, dedupe, sort.

    Deduplication is on (Loan ID, exact reporting date) keeping the first
    occurrence, so the winner is decided by upload `created_at` order and
    then sheet order. The sort is a stable mergesort: that is a determinism
    guarantee, not an incidental detail.
    """
    working = df.copy()
    reporting_col = resolve_reporting_month_column(working)
    if reporting_col != "Reporting Month":
        working = working.rename(columns={reporting_col: "Reporting Month"})

    working["Reporting Month"] = pd.to_datetime(working["Reporting Month"])
    working = working.drop_duplicates(subset=["Loan ID", "Reporting Month"], keep="first")
    working = working.sort_values(["Loan ID", "Reporting Month"], kind="mergesort").reset_index(
        drop=True
    )
    return working


def find_month_collisions(df: pd.DataFrame) -> pd.DataFrame:
    """Rows sharing (Loan ID, calendar month) but differing in day-of-month.

    Dedupe is on the exact date, so two rows for the same loan in the same
    calendar month both survive. The next-month join below keys on the
    calendar month, so such rows would otherwise turn a many-to-one merge
    into a row-expanding one and inflate the money-weighted matrix. This
    surfaces them instead of letting them corrupt the numbers silently.

    Expects a frame already through `prepare_pd_frame`.
    """
    if df.empty:
        return df.iloc[0:0]
    key = pd.DataFrame(
        {
            "Loan ID": df["Loan ID"],
            "_period": df["Reporting Month"].dt.to_period("M"),
        }
    )
    return df[key.duplicated(keep=False)]


def derive_next_month_staging(df: pd.DataFrame) -> pd.DataFrame:
    """Step 2 — attach each row's staging at month t+1, or "Offbooks".

    Join on a calendar-month Period, not on `Reporting Month - DateOffset(months=1)`:
    DateOffset(months=1) preserves day-of-month and only clips when that day
    doesn't exist in the target month, so subtracting it from a true
    month-end date does NOT reliably land back on the prior month's
    month-end whenever the current month is shorter than the prior one
    (e.g. Reporting Month = Feb 29 -> shifted = Jan 29, not Jan 31). That
    silently broke the merge for reporting months Feb/Apr/Jun/Sep/Nov,
    miscoding every loan active in those months as "Offbooks" even when it
    continued normally -- discarding real transition data for ~5 of every
    12 months from the PD model. Period[M] arithmetic has no day component,
    so it can't clip and is immune to this.

    The lookup side is deduplicated on (Loan ID, calendar month) so the merge
    is always many-to-one. Without that, a loan reported twice in one month
    (see `find_month_collisions`) expands the row count and inflates the
    money-weighted matrix. For data with no such collision this is a no-op.
    """
    working = df.copy()
    working["_reporting_period"] = working["Reporting Month"].dt.to_period("M")
    next_lookup = working[["Loan ID", "_reporting_period", "Staging"]].copy()
    next_lookup = next_lookup.drop_duplicates(
        subset=["Loan ID", "_reporting_period"], keep="first"
    )
    next_lookup["_reporting_period"] = next_lookup["_reporting_period"] - 1
    working = working.merge(
        next_lookup.rename(columns={"Staging": "Next Month Staging"}),
        on=["Loan ID", "_reporting_period"],
        how="left",
    )
    working["Next Month Staging"] = working["Next Month Staging"].fillna(OFFBOOKS)
    working = working.drop(columns=["_reporting_period"])
    return working


def build_monthly_matrix(subset: pd.DataFrame) -> np.ndarray:
    """Steps 3-4 — money-weighted 3x4 transition matrix for one segment-month.

    Math: M[i, j] = sum(Loan Amount) for loans with Staging == stage i
    this month and Next Month Staging == stage j next month.
    Rows are Stage 1/2/3; columns are Stage 1/2/3/Offbooks.

    Exposure-weighted, not loan-count-weighted. The Markov projection slices
    off the Offbooks column; the validate-time preview keeps it.
    """
    # Coerced up front so the pivot's float fill_value always fits the result
    # dtype. An integer-typed Loan Amount column otherwise trips a pandas
    # deprecation that is scheduled to become an error.
    weighted = subset.assign(
        **{"Loan Amount": pd.to_numeric(subset["Loan Amount"], errors="coerce").astype("float64")}
    )
    pivot = weighted.pivot_table(
        index="Staging",
        columns="Next Month Staging",
        values="Loan Amount",
        aggfunc="sum",
        fill_value=0.0,
    )
    matrix = np.zeros((3, 4), dtype=np.float64)
    for row_idx, stage in enumerate(STAGES):
        if stage not in pivot.index:
            continue
        for col_idx, next_stage in enumerate(NEXT_STAGES):
            if next_stage in pivot.columns:
                matrix[row_idx, col_idx] = float(pivot.loc[stage, next_stage])
    return matrix


def _build_monthly_matrix(subset: pd.DataFrame) -> np.ndarray:
    """Projection-shaped (3x3) monthly matrix — Offbooks column discarded."""
    return build_monthly_matrix(subset)[:, :3]


def row_proportions(matrix: np.ndarray) -> np.ndarray:
    """Step 5 — row-normalize an observed matrix into transition probabilities.

    Math: P[i, j] = A[i, j] / sum_j(A[i, j])
    Empirical one-step Markov transition probability: of everything that
    started in stage i, what fraction ended up in stage j.

    A row with zero observed exposure is left as all zeros. That is a
    degenerate row, and it silently produces a zero lifetime PD for that
    starting stage -- criterion EC-19 exists to surface it.

    Works on both the 3x3 projection matrix and the 3x4 preview matrix; the
    denominators differ, and deliberately so (see pd_preview.py).
    """
    proportions = np.zeros_like(matrix, dtype=np.float64)
    for row_idx in range(matrix.shape[0]):
        row_total = matrix[row_idx].sum()
        if row_total > 0:
            proportions[row_idx] = matrix[row_idx] / row_total
    return proportions


_row_proportions = row_proportions


def cure_rate_from_proportions(proportions: np.ndarray) -> float:
    """Step 6 — cure rate from the raw proportions, before Stage 3 is absorbed.

    Math: cure_rate = P(Stage3 -> Stage1) + P(Stage3 -> Stage2)
    Of loans in default last period, the fraction that recovered to a
    healthy or watch-list stage rather than remaining in default. Only
    on-book destinations count as a cure; an Offbooks exit is not a cure.
    """
    return float(proportions[2, 0] + proportions[2, 1])


def normalize_matrix(proportions: np.ndarray) -> np.ndarray:
    """Step 7 — make Stage 3 absorbing for the multi-month projection.

    Math: N[Stage1] = [P(1,1), P(1,2), P(1,3)]   (left as raw proportions)
          N[Stage3] = [0, 0, 1]                   (Stage 3 made absorbing)
    Modelling convention (matches PD.ipynb): only Stage 3 is normalized,
    into an absorbing state, for the purpose of multi-month projection.
    Stage 1's raw empirical proportions are kept as-is, including any
    directly observed Stage 1 -> Stage 3 transitions. The cure possibility
    is measured separately as cure_rate (above) and re-applied later in
    ead_engine.py rather than left inside this matrix.
    """
    normalized = proportions.copy()
    normalized[2] = [0.0, 0.0, 1.0]
    return normalized


_normalize_matrix = normalize_matrix


def _compute_marginal_pd(group: pd.DataFrame) -> pd.Series:
    # Math: marginal_PD(n) = (S3(n) - S3(n-1)) / (1 - S3(n-1))
    #   S3(n) = cumulative probability of Stage 3 (default) by month n
    # This is the discrete hazard-rate formula from survival analysis,
    # h(n) = (F(n) - F(n-1)) / (1 - F(n-1)): it converts a *cumulative*
    # default probability into "chance of newly defaulting in month n,
    # given the loan was still not in default going into month n." This
    # marginal_PD is the "PD" plugged into the Credit Loss formula in
    # ead_engine.py. When the prior month's cumulative default probability
    # has already saturated to ~100% (denom ~= 0), marginal PD is treated
    # as certain (1) rather than 0 -- matches PD.ipynb's
    # "if prev_default == 1: marginal_PD = 1".
    s3 = group["Stage_3_prob"].to_numpy(dtype=np.float64)
    marginal = np.zeros(len(s3), dtype=np.float64)
    if len(s3) == 0:
        return pd.Series(marginal, index=group.index)
    marginal[0] = s3[0]
    for idx in range(1, len(s3)):
        denom = 1.0 - s3[idx - 1]
        if denom <= _DENOM_EPS:
            marginal[idx] = 1.0
        else:
            marginal[idx] = (s3[idx] - s3[idx - 1]) / denom
    return pd.Series(marginal, index=group.index)


def segment_names(df: pd.DataFrame) -> list[Any]:
    """Deterministically ordered segment labels present in the frame.

    Values are returned as-is (not coerced to `str`) so the caller's
    `df["SEGMENT"] == segment` comparison keeps matching for non-string
    segment values; only the ordering is imposed.
    """
    return sorted(df["SEGMENT"].dropna().unique(), key=str)


def compute_pd(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, float], dict[str, Any]]:
    """Run the 11-step PD computation pipeline.

    Returns the long-form PD table, cure rates per segment, and intermediate
    matrices for Excel export.
    """
    # Steps 0-1 — combine/sort (caller combines files; dedupe and sort here).
    working = prepare_pd_frame(df)

    # Step 2 — next month staging.
    working = derive_next_month_staging(working)

    segments = segment_names(working)
    monthly_matrices: dict[str, dict[str, dict[str, list[float]]]] = {}
    aggregate_matrices: dict[str, dict[str, list[float]]] = {}
    proportion_matrices: dict[str, dict[str, list[float]]] = {}
    normalized_matrices: dict[str, dict[str, list[float]]] = {}
    cure_rates: dict[str, float] = {}

    rows: list[dict[str, Any]] = []

    for segment in segments:
        segment_df = working[working["SEGMENT"] == segment]
        reporting_months = sorted(segment_df["Reporting Month"].unique())

        monthly_matrices[segment] = {}
        aggregate = np.zeros((3, 3), dtype=np.float64)

        for reporting_month in reporting_months:
            month_df = segment_df[segment_df["Reporting Month"] == reporting_month]
            # Steps 3-4 — the projection pools the 3x3 slice; the Offbooks
            # column is a preview-only concern.
            month_matrix = build_monthly_matrix(month_df)[:, :3]
            aggregate += month_matrix
            month_key = pd.Timestamp(reporting_month).strftime("%Y-%m-%d")
            monthly_matrices[segment][month_key] = _matrix_to_dict(month_matrix)

        aggregate_matrices[segment] = _matrix_to_dict(aggregate)

        # Steps 5–7 — row proportions, cure rate, normalize.
        proportions = row_proportions(aggregate)
        proportion_matrices[segment] = _matrix_to_dict(proportions)
        cure_rates[segment] = cure_rate_from_proportions(proportions)
        normalized = normalize_matrix(proportions)
        normalized_matrices[segment] = _matrix_to_dict(normalized)

        # Steps 8–9 — matrix powers and long-form table.
        for month in range(1, MAX_PD_MONTH + 1):
            # Math: P^(n) = P x P x ... x P   (n times, matrix multiplication)
            # (P^n)[i, j] = probability of being in stage j after n months,
            # starting from stage i -- the standard Chapman-Kolmogorov
            # projection for a time-homogeneous discrete Markov chain.
            powered = matrix_power(normalized, month)
            for transition_idx, transition in enumerate(STAGES):
                rows.append(
                    {
                        "SEGMENT": segment,
                        "Month": month,
                        "Transition": transition,
                        "Stage_1_prob": float(powered[transition_idx, 0]),
                        "Stage_2_prob": float(powered[transition_idx, 1]),
                        "Stage_3_prob": float(powered[transition_idx, 2]),
                    }
                )

    output = pd.DataFrame(rows)
    if output.empty:
        output = pd.DataFrame(
            columns=[
                "SEGMENT",
                "Month",
                "Transition",
                "Stage_1_prob",
                "Stage_2_prob",
                "Stage_3_prob",
                "Marginal_PD",
                "Cure_Rate",
            ]
        )
    else:
        output = output.sort_values(
            ["SEGMENT", "Transition", "Month"],
            kind="mergesort",
        ).reset_index(drop=True)

        # Step 10 — marginal PD.
        output["Marginal_PD"] = (
            output.groupby(["SEGMENT", "Transition"], sort=False, group_keys=False)
            .apply(_compute_marginal_pd)
            .to_numpy(dtype=np.float64)
        )

        # Step 11 — cure rate column.
        output["Cure_Rate"] = output["SEGMENT"].map(cure_rates).astype(np.float64)

    intermediates: dict[str, Any] = {
        "monthly_matrices": monthly_matrices,
        "aggregate": aggregate_matrices,
        "proportion": proportion_matrices,
        "normalized": normalized_matrices,
    }

    return output, cure_rates, intermediates
