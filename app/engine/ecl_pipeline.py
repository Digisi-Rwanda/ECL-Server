"""The whole ECL calculation (spec v1.0, Steps 1-11 and 12.1) as one pure function.

Validation previews and the compute worker both call `run_ecl`, so the
numbers a reviewer accepts are the numbers that get stored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.engine.ead_engine import (
    DISCOUNTED_ECL,
    T_MATURED,
    T_STAGE1,
    T_STAGE3,
    T_ZERO_RATE,
    TREATMENT,
    compute_rows,
)
from app.engine.ecl_rules import (
    CAT_CAP,
    CAT_RECON,
    CUSTOMER_ID,
    LOAN_ID,
    OUTSTANDING,
    REPORTING_DATE,
    SEGMENT,
    STAGE,
    STAGES,
    ExceptionLine,
    PreparedInputs,
)
from app.engine.lgd_engine import DISCOUNTED_COLLATERAL, allocated_col, compute_lgd

SPEC_VERSION = "1.0"
TOLERANCE = 0.01

ECL_BEFORE_CAP = "ECL Before Cap"
ECL_CAPPED = "ECL Capped"
TOTAL_ECL = "Total ECL"
COVERAGE = "ECL Coverage Ratio"
TOTAL_ROW = "Total"


@dataclass(frozen=True, slots=True)
class ReconciliationCheck:
    name: str
    expected: float
    actual: float
    passed: bool
    detail: str

    @property
    def difference(self) -> float:
        return self.actual - self.expected


@dataclass
class EclRun:
    prepared: PreparedInputs
    lgd: pd.DataFrame
    rows: pd.DataFrame
    loan_summary: pd.DataFrame
    stage_summary: pd.DataFrame
    segment_summary: pd.DataFrame
    reconciliation: list[ReconciliationCheck]
    exceptions: list[ExceptionLine] = field(default_factory=list)

    @property
    def total_ecl(self) -> float:
        return float(self.loan_summary[TOTAL_ECL].sum())

    @property
    def total_outstanding(self) -> float:
        return float(self.loan_summary[OUTSTANDING].sum())

    @property
    def coverage_ratio(self) -> float:
        return self.total_ecl / self.total_outstanding if self.total_outstanding else 0.0

    @property
    def reconciliation_passed(self) -> bool:
        return all(c.passed for c in self.reconciliation)


def build_loan_summary(
    prepared: PreparedInputs, lgd: pd.DataFrame, rows: pd.DataFrame
) -> pd.DataFrame:
    base = prepared.exposure[[LOAN_ID, CUSTOMER_ID, SEGMENT, STAGE, REPORTING_DATE, OUTSTANDING]]
    summary = base.merge(lgd[[LOAN_ID, DISCOUNTED_COLLATERAL]], on=LOAN_ID, how="left")
    # ECL Before Cap = SUM(Discounted ECL) GROUP BY Loan ID
    before = rows.groupby(LOAN_ID)[DISCOUNTED_ECL].sum()
    summary[ECL_BEFORE_CAP] = summary[LOAN_ID].map(before).fillna(0.0).astype(float)
    # Total ECL = MIN(ECL Before Cap, Outstanding Amount): ECL never exceeds exposure.
    summary[TOTAL_ECL] = np.minimum(summary[ECL_BEFORE_CAP], summary[OUTSTANDING])
    summary[ECL_CAPPED] = np.where(summary[ECL_BEFORE_CAP] > summary[OUTSTANDING], "Yes", "No")
    columns = [
        LOAN_ID,
        CUSTOMER_ID,
        SEGMENT,
        STAGE,
        REPORTING_DATE,
        OUTSTANDING,
        DISCOUNTED_COLLATERAL,
        ECL_BEFORE_CAP,
        ECL_CAPPED,
        TOTAL_ECL,
    ]
    return summary[columns].sort_values(LOAN_ID, kind="mergesort").reset_index(drop=True)


def _coverage(ecl: pd.Series, outstanding: pd.Series) -> pd.Series:
    # ECL Coverage Ratio = ECL / Outstanding Amount, 0 when Outstanding is 0.
    return (ecl / outstanding.where(outstanding != 0)).fillna(0.0)


def build_group_summary(loan_summary: pd.DataFrame, by: str, keys: list[str]) -> pd.DataFrame:
    """Outstanding (as at Reporting Date), capped ECL and coverage per group, plus Total."""
    grouped = loan_summary.groupby(by)[[OUTSTANDING, TOTAL_ECL]].sum()
    table = grouped.reindex(keys, fill_value=0.0).reset_index().rename(columns={TOTAL_ECL: "ECL"})
    total = pd.DataFrame(
        {by: [TOTAL_ROW], OUTSTANDING: [table[OUTSTANDING].sum()], "ECL": [table["ECL"].sum()]}
    )
    table = pd.concat([table, total], ignore_index=True)
    table[COVERAGE] = _coverage(table["ECL"], table[OUTSTANDING])
    return table


def _check(name: str, expected: float, actual: float, detail: str = "") -> ReconciliationCheck:
    return ReconciliationCheck(
        name, float(expected), float(actual), bool(abs(actual - expected) <= TOLERANCE), detail
    )


def reconcile(run: EclRun) -> list[ReconciliationCheck]:
    """Spec 12.1. A difference up to 0.01 is rounding and passes."""
    prepared, rows, summary = run.prepared, run.rows, run.loan_summary
    excluded = prepared.excluded
    checks = [
        _check(
            "Loan count",
            prepared.exposure_rows_in,
            len(summary) + len(excluded),
            "Exposure rows = Loan Summary loans + excluded rows",
        ),
        _check(
            "Outstanding Amount",
            prepared.exposure_outstanding_in,
            summary[OUTSTANDING].sum() + excluded[OUTSTANDING].fillna(0.0).sum(),
            "Exposure total = Loan Summary total + excluded total",
        ),
    ]

    counts = rows.groupby(LOAN_ID).size().reindex(summary[LOAN_ID], fill_value=0)
    treatment = (
        rows.drop_duplicates(LOAN_ID).set_index(LOAN_ID)[TREATMENT].reindex(summary[LOAN_ID])
    )
    single = treatment.isin([T_STAGE3, T_MATURED, T_ZERO_RATE])
    bad = (counts < 1) | ((treatment == T_STAGE1) & (counts > 13)) | (single & (counts != 1))
    checks.append(
        _check(
            "Row Detail coverage",
            0,
            int(bad.sum()),
            "Loans breaking: ≥1 row each, Stage 1 ≤ 13 rows, single-row loans exactly 1",
        )
    )

    lgd, coll = run.lgd, prepared.collateral
    expected_alloc = actual_alloc = 0.0
    failing_customers: set[str] = set()
    for t in prepared.collateral_types:
        value = coll.groupby(CUSTOMER_ID)[t].first()
        allocated = (
            lgd.groupby(CUSTOMER_ID)[allocated_col(t)].sum().reindex(value.index, fill_value=0.0)
        )
        expected_alloc += float(value.sum())
        actual_alloc += float(allocated.sum())
        failing_customers |= set(value.index[(allocated - value).abs() > TOLERANCE])
    checks.append(
        _check(
            "Collateral allocation",
            expected_alloc,
            actual_alloc,
            f"{len(failing_customers)} customer(s) not fully allocated"
            + (f": {', '.join(sorted(failing_customers)[:5])}" if failing_customers else ""),
        )
    )

    checks.append(
        _check(
            "ECL build-up",
            rows[DISCOUNTED_ECL].sum(),
            summary[ECL_BEFORE_CAP].sum(),
            "Σ Row Detail Discounted ECL = Σ ECL Before Cap",
        )
    )

    stage_total = run.stage_summary.iloc[-1]
    segment_total = run.segment_summary.iloc[-1]
    summary_out, summary_ecl = summary[OUTSTANDING].sum(), summary[TOTAL_ECL].sum()
    worst = max(
        abs(stage_total[OUTSTANDING] - summary_out),
        abs(stage_total["ECL"] - summary_ecl),
        abs(segment_total[OUTSTANDING] - summary_out),
        abs(segment_total["ECL"] - summary_ecl),
    )
    checks.append(
        _check(
            "Summary totals",
            0,
            worst,
            "Largest gap between Stage/Segment Total rows and Loan Summary",
        )
    )

    breaches = int(
        ((summary[TOTAL_ECL] < 0) | (summary[TOTAL_ECL] > summary[OUTSTANDING] + 1e-9)).sum()
    )
    checks.append(
        _check("ECL limits", 0, breaches, "Loans with Total ECL < 0 or > Outstanding Amount")
    )
    return checks


def run_ecl(prepared: PreparedInputs, pd_curve: pd.DataFrame) -> EclRun:
    lgd = compute_lgd(prepared)
    rows, pd_exceptions = compute_rows(prepared.exposure, lgd, pd_curve)
    summary = build_loan_summary(prepared, lgd, rows)
    segments = sorted(summary[SEGMENT].unique().tolist())
    run = EclRun(
        prepared=prepared,
        lgd=lgd,
        rows=rows,
        loan_summary=summary,
        stage_summary=build_group_summary(summary, STAGE, list(STAGES)),
        segment_summary=build_group_summary(summary, SEGMENT, segments),
        reconciliation=[],
        exceptions=[*prepared.exceptions, *pd_exceptions],
    )
    for _, r in summary[summary[ECL_CAPPED] == "Yes"].iterrows():
        before, outstanding = r[ECL_BEFORE_CAP], r[OUTSTANDING]
        run.exceptions.append(
            ExceptionLine(
                CAT_CAP,
                "Loan Summary",
                r[LOAN_ID],
                r[CUSTOMER_ID],
                ECL_BEFORE_CAP,
                f"ECL Before Cap {before:,.2f}; Outstanding {outstanding:,.2f}; "
                f"removed {before - outstanding:,.2f}",
                "ECL Before Cap exceeds the Outstanding Amount",
                "ECL capped",
            )
        )
    run.reconciliation = reconcile(run)
    for c in run.reconciliation:
        if not c.passed:
            run.exceptions.append(
                ExceptionLine(
                    CAT_RECON,
                    "Outputs",
                    "",
                    "",
                    c.name,
                    f"Expected {c.expected:,.2f}; Actual {c.actual:,.2f}; "
                    f"Difference {c.difference:,.2f}",
                    c.detail or "Reconciliation check failed",
                    "None (reported only)",
                )
            )
    return run
