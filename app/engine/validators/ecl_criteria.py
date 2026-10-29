"""LGD and EAD validation checklists (spec v1.0, section 1.5 and 12.1).

Pure and DB-free. Each check is a view over `ecl_rules.prepare_inputs` and
`ecl_pipeline.run_ecl`, the functions compute runs, so a Review shown here is
exactly the exclusion or adjustment compute will apply.

Outcomes: pass, review (the spec adjusts or excludes and reports; the user
must accept), block (the file cannot be used), info (numbers only).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.engine.ead_engine import R_LAST_MONTH, T_MATURED, T_ZERO_RATE, TREATMENT
from app.engine.ecl_pipeline import ECL_CAPPED, EclRun, run_ecl
from app.engine.ecl_rules import (
    CUSTOMER_ID,
    DS_COLLATERAL,
    DS_EXPOSURE,
    DS_PARAMETERS,
    EIR,
    EIR_ALIASES,
    EXPOSURE_FIELDS,
    LOAN_ID,
    OUTSTANDING,
    SEGMENT,
    STAGE,
    STAGE_ALIASES,
    ExceptionLine,
    PreparedInputs,
    canonical_exposure,
    collateral_type_columns,
    prepare_inputs,
)
from app.engine.lgd_engine import DISCOUNTED_COLLATERAL, compute_lgd
from app.engine.validators.pd_criteria import CriterionResult, _criterion

LGD_REQUIRED = (CUSTOMER_ID, LOAN_ID, OUTSTANDING)
# Reconciliation checks whose failure means the numbers cannot be trusted.
# Collateral allocation may legitimately fail (a customer loan without a
# collateral row keeps 0, spec Step 2), so it is reported for review instead.
_REVIEW_ONLY_CHECKS = frozenset({"Collateral allocation"})


@dataclass
class LgdValidation:
    status: str
    criteria: list[CriterionResult]
    prepared: PreparedInputs
    lgd: pd.DataFrame
    provisional: bool


@dataclass
class EadValidation:
    status: str
    criteria: list[CriterionResult]
    run: EclRun | None
    prepared: PreparedInputs | None
    reporting_date_source: str


def _status(criteria: list[CriterionResult]) -> str:
    outcomes = {c.outcome for c in criteria}
    if "block" in outcomes:
        return "blocking"
    return "warn" if "review" in outcomes else "ok"


def _money(v: float) -> str:
    return f"{v:,.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _sample(values: list[str], limit: int = 3) -> str:
    vals = [v for v in values if v]
    head = ", ".join(vals[:limit])
    return head + (f" and {len(vals) - limit} more" if len(vals) > limit else "")


def _lines(
    exceptions: list[ExceptionLine], dataset: str, action_prefix: str, issue: str | None = None
):
    return [
        e
        for e in exceptions
        if e.dataset == dataset
        and e.action.startswith(action_prefix)
        and (issue is None or issue in e.issue)
    ]


def _review_or_pass(
    code: str, name: str, lines: list[ExceptionLine], ok: str, bad: str
) -> CriterionResult:
    if not lines:
        return _criterion(code, name, "business", "pass", ok)
    who = _sample([e.loan_id or e.customer_id or e.value for e in lines])
    return _criterion(
        code, name, "business", "review", f"{bad} ({_plural(len(lines), 'line')}: {who})."
    )


def _missing_columns(df: pd.DataFrame, required: tuple[str, ...]) -> list[str]:
    return [c for c in required if c not in df.columns]


# ── LGD ──────────────────────────────────────────────────────────────────────


def _provisional_exposure(collateral: pd.DataFrame) -> pd.DataFrame:
    """Stand-in Exposure built from the collateral file for the LGD step.

    Spec Step 1 takes Total Customer Exposure from the Exposure Dataset, which
    is uploaded next. Until then the collateral file's own Outstanding Amount
    and EIR stand in, labelled provisional; the EAD step recomputes for real.
    """
    eir_col = next((c for c in EIR_ALIASES if c in collateral.columns), None)
    first = collateral.drop_duplicates(LOAN_ID)
    today = pd.Timestamp(date.today())
    return pd.DataFrame(
        {
            LOAN_ID: first[LOAN_ID],
            CUSTOMER_ID: first[CUSTOMER_ID],
            SEGMENT: "—",
            "Reporting Date": today,
            "First Payment Date": today,
            "Maturity Date": today,
            STAGE: "Stage 1",
            OUTSTANDING: pd.to_numeric(first[OUTSTANDING], errors="coerce")
            .clip(lower=0)
            .fillna(0.0),
            EIR: (
                pd.to_numeric(first[eir_col], errors="coerce").clip(0, 1).fillna(0.0)
                if eir_col
                else 0.0
            ),
        }
    )


def evaluate_lgd(
    collateral: pd.DataFrame,
    collateral_config: list[dict[str, object]],
    *,
    exposure: pd.DataFrame | None,
    pd_loan_ids: set[str],
    engine_version: str,
) -> LgdValidation:
    structural: list[CriterionResult] = []
    if collateral.empty:
        structural.append(
            _criterion(
                "LG-01",
                "Collateral data sheet found",
                "structural",
                "block",
                "No sheet has the Customer ID, Loan ID and Outstanding Amount columns. "
                "Use the LGD template's data sheet.",
            )
        )
        return LgdValidation(
            "blocking", structural, prepare_inputs(pd.DataFrame(), None, []), pd.DataFrame(), True
        )
    structural.append(
        _criterion(
            "LG-01",
            "Collateral data sheet found",
            "structural",
            "pass",
            f"{_plural(len(collateral), 'row')} read.",
        )
    )
    missing = _missing_columns(collateral, LGD_REQUIRED)
    if not any(c in collateral.columns for c in EIR_ALIASES):
        missing.append("Effective Interest Rate (EIR)")
    structural.append(
        _criterion(
            "LG-02",
            "Required columns present",
            "structural",
            "block" if missing else "pass",
            f"Missing: {', '.join(missing)}."
            if missing
            else "Customer ID, Loan ID, Outstanding Amount and EIR are present.",
        )
    )
    types = collateral_type_columns(collateral)
    structural.append(
        _criterion(
            "LG-03",
            "Collateral type columns",
            "structural",
            "pass" if types else "review",
            f"{_plural(len(types), 'collateral type')}: {', '.join(types)}."
            if types
            else "No collateral columns: every loan will have Discounted Collateral 0 (LGW = 100%).",
        )
    )
    if missing:
        return LgdValidation(
            "blocking", structural, prepare_inputs(pd.DataFrame(), None, []), pd.DataFrame(), True
        )

    provisional = exposure is None
    prepared = prepare_inputs(
        _provisional_exposure(collateral) if provisional else exposure,
        collateral,
        collateral_config,
    )
    lgd = compute_lgd(prepared)
    ex = prepared.exceptions

    business = [
        _review_or_pass(
            "LG-11",
            "Collateral parameters",
            _lines(ex, DS_PARAMETERS, "Treated as 0"),
            "Every collateral type has a haircut in 0–1 and a time to realize ≥ 0.",
            "Types without valid parameters count as 0 until fixed in Workspace → Collateral types",
        ),
        _review_or_pass(
            "LG-12",
            "Unique Loan IDs",
            _lines(ex, DS_COLLATERAL, "Loan excluded"),
            "Every Loan ID appears once.",
            "Duplicated Loan IDs are excluded from the run",
        ),
        _review_or_pass(
            "LG-13",
            "Collateral values valid",
            _lines(ex, DS_COLLATERAL, "Treated as 0"),
            "No negative or non-numeric collateral values.",
            "Negative or non-numeric values are treated as 0",
        ),
        _review_or_pass(
            "LG-14",
            "Customer collateral consistent",
            _lines(ex, DS_COLLATERAL, "Lowest value used"),
            "Each customer shows the same total on every loan row, as the spec requires.",
            "Collateral is held per customer and repeated on each loan row; where rows differ the "
            "lowest value is used",
        ),
        _review_or_pass(
            "LG-15",
            "Collateral EIR valid",
            _lines(ex, DS_COLLATERAL, "Exposure EIR used"),
            "Every EIR is a decimal between 0 and 1.",
            "Missing or out-of-range EIR: the Exposure EIR is used",
        ),
    ]
    if not provisional:
        business.append(
            _review_or_pass(
                "LG-16",
                "Loans match the Exposure file",
                _lines(ex, DS_COLLATERAL, "Row ignored")
                + _lines(ex, DS_COLLATERAL, "Exposure Customer ID"),
                "Every collateral row belongs to an Exposure loan with the same Customer ID.",
                "Rows not in the Exposure file are ignored; Customer ID mismatches use the Exposure value",
            )
        )

    outstanding = lgd[OUTSTANDING].astype(float)
    collateral_value = lgd[DISCOUNTED_COLLATERAL].astype(float)
    over = int((collateral_value >= outstanding).sum())
    unsecured = int((collateral_value == 0).sum())
    business.append(
        _criterion(
            "LG-17",
            "Collateral coverage",
            "info",
            "info",
            f"{_plural(len(lgd), 'loan')}: {over:,} fully covered (LGW 0), {unsecured:,} unsecured (LGW 100%); "
            f"discounted collateral {_money(collateral_value.sum())} against {_money(outstanding.sum())} outstanding.",
        )
    )
    no_pd = sorted(set(lgd[LOAN_ID]) - pd_loan_ids) if pd_loan_ids else []
    business.append(
        _criterion(
            "LG-18",
            "Loans with PD history",
            "info",
            "info",
            f"{_plural(len(no_pd), 'loan')} not in the PD files: {_sample(no_pd)}."
            if no_pd
            else "Every collateral loan appears in the PD history.",
        )
    )
    business.append(
        _criterion(
            "LG-19",
            f"Engine {engine_version}",
            "info",
            "info",
            "Discounted Collateral = Σ Loan Proportion × Collateral × (1 − Haircut) × (1 + EIR)^−years; "
            + ("provisional until the Exposure file is validated." if provisional else "final."),
        )
    )
    criteria = structural + business
    return LgdValidation(_status(criteria), criteria, prepared, lgd, provisional)


# ── EAD ──────────────────────────────────────────────────────────────────────


def _exposure_missing_columns(df: pd.DataFrame) -> list[str]:
    missing = []
    for col in EXPOSURE_FIELDS:
        aliases = STAGE_ALIASES if col == STAGE else EIR_ALIASES if col == EIR else (col,)
        if not any(a in df.columns for a in aliases):
            missing.append(col)
    return missing


def evaluate_ead(
    exposure: pd.DataFrame,
    collateral: pd.DataFrame,
    collateral_config: list[dict[str, object]],
    pd_curve: pd.DataFrame,
    *,
    pd_latest: pd.DataFrame,
    run_reporting_date: date | None,
    engine_version: str,
) -> EadValidation:
    """`pd_latest`: Loan ID, Staging, Loan Amount at each loan's last PD month."""
    source = "run" if run_reporting_date else "file"
    structural: list[CriterionResult] = []
    if exposure.empty:
        structural.append(
            _criterion(
                "EA-01",
                "Exposure data sheet found",
                "structural",
                "block",
                "No sheet has the Loan ID and Reporting Date columns. Use the EAD template's data sheet.",
            )
        )
        return EadValidation("blocking", structural, None, None, source)
    structural.append(
        _criterion(
            "EA-01",
            "Exposure data sheet found",
            "structural",
            "pass",
            f"{_plural(len(exposure), 'row')} read.",
        )
    )
    missing = _exposure_missing_columns(exposure)
    structural.append(
        _criterion(
            "EA-02",
            "Required columns present",
            "structural",
            "block" if missing else "pass",
            f"Missing: {', '.join(missing)}."
            if missing
            else "Loan ID, Customer ID, SEGMENT, three dates, Stage, Outstanding Amount and EIR are present.",
        )
    )
    if missing:
        return EadValidation("blocking", structural, None, None, source)

    prepared = prepare_inputs(exposure, collateral, collateral_config, run_reporting_date)
    curve_segments = set(pd_curve["SEGMENT"].astype(str)) if not pd_curve.empty else set()
    loan_segments = set(prepared.exposure[SEGMENT].astype(str))
    no_curve = sorted(loan_segments - curve_segments)
    structural.append(
        _criterion(
            "EA-03",
            "Every SEGMENT has a PD curve",
            "structural",
            "block" if no_curve else "pass",
            f"No PD history for: {', '.join(no_curve)}. Add these segments to the PD files and re-validate PD."
            if no_curve
            else f"{_plural(len(loan_segments), 'segment')} matched to the PD curve.",
        )
    )
    if no_curve:
        return EadValidation("blocking", structural, None, prepared, source)

    run = run_ecl(prepared, pd_curve)
    ex = prepared.exceptions
    excluded = prepared.excluded
    reasons = Counter(
        e.issue.split(" (")[0]
        for e in ex
        if e.dataset == DS_EXPOSURE and e.action == "Loan excluded"
    )
    rep_lines = [
        e
        for e in ex
        if e.dataset == DS_EXPOSURE and e.field == "Reporting Date" and "differs" in e.issue
    ]
    business: list[CriterionResult] = []

    if excluded.empty:
        business.append(
            _criterion(
                "EA-11",
                "Loans pass the input rules",
                "business",
                "pass",
                "No exposure row is excluded.",
            )
        )
    else:
        detail = "; ".join(f"{issue} ×{n}" for issue, n in reasons.most_common(4))
        business.append(
            _criterion(
                "EA-11",
                "Loans pass the input rules",
                "business",
                "review",
                f"{_plural(len(excluded), 'row')} ({_money(excluded[OUTSTANDING].fillna(0).sum())}) will be "
                f"excluded and listed in the Exception Report: {detail}.",
            )
        )
    rep = prepared.reporting_date.date().isoformat() if prepared.reporting_date is not None else "—"
    business.append(
        _criterion(
            "EA-12",
            "Reporting Date",
            "business",
            "review" if rep_lines else "pass",
            f"Run Reporting Date {rep} ({'set on the run' if source == 'run' else 'most common date in the file'})"
            + (
                f"; {_plural(len(rep_lines), 'loan')} with another date excluded."
                if rep_lines
                else "; every loan matches."
            ),
        )
    )

    no_row = sorted(set(prepared.exposure[LOAN_ID]) - set(prepared.collateral[LOAN_ID]))
    business.append(
        _criterion(
            "EA-13",
            "Collateral linked",
            "info",
            "info",
            f"{_plural(len(no_row), 'loan')} without a collateral row (collateral 0): {_sample(no_row)}."
            if no_row
            else "Every exposure loan has a collateral row.",
        )
    )
    business.append(
        _review_or_pass(
            "EA-14",
            "Collateral rows match exposure",
            _lines(ex, DS_COLLATERAL, "Row ignored")
            + _lines(ex, DS_COLLATERAL, "Exposure Customer ID"),
            "Every collateral row belongs to an exposure loan with the same Customer ID.",
            "Rows not in the Exposure file are ignored; Customer ID mismatches use the Exposure value",
        )
    )

    rows = run.rows
    first = rows.drop_duplicates(LOAN_ID)
    single = first[first[TREATMENT].isin([T_MATURED, T_ZERO_RATE])]
    business.append(
        _criterion(
            "EA-15",
            "Matured and zero-rate loans",
            "info",
            "info",
            f"{_plural(len(single), 'loan')} treated as a single row on the Outstanding Amount "
            f"(keeping their own stage for PD): {_sample(single[LOAN_ID].tolist())}."
            if len(single)
            else "No matured or zero-rate loans.",
        )
    )
    beyond = rows.loc[rows["PD Rule"] == R_LAST_MONTH, LOAN_ID].unique().tolist()
    business.append(
        _criterion(
            "EA-16",
            "PD curve horizon",
            "info",
            "info",
            f"{_plural(len(beyond), 'loan')} run past the end of the PD curve and use the last available "
            f"Month: {_sample(beyond)}."
            if beyond
            else "Every PD-bearing row falls inside the PD curve.",
        )
    )

    stage_mismatch: list[str] = []
    amount_gap: list[str] = []
    if not pd_latest.empty:
        latest = pd_latest.set_index(LOAN_ID)
        cur = canonical_exposure(exposure).drop_duplicates(LOAN_ID).set_index(LOAN_ID)
        both = cur.index.intersection(latest.index)
        stage_mismatch = [
            i for i in both if str(latest.at[i, "Staging"]).strip() != str(cur.at[i, STAGE])
        ]
        for i in both:
            a, b = float(cur.at[i, OUTSTANDING] or 0), float(latest.at[i, "Loan Amount"] or 0)
            if max(abs(a), abs(b)) > 0 and abs(a - b) / max(abs(a), abs(b)) > 0.01:
                amount_gap.append(str(i))
    business.append(
        _criterion(
            "EA-17",
            "Stage agrees with the latest PD month",
            "business",
            "review" if stage_mismatch else "pass",
            f"{_plural(len(stage_mismatch), 'loan')} staged differently in the PD file's latest month: "
            f"{_sample(sorted(stage_mismatch))}. The Exposure stage is used."
            if stage_mismatch
            else "Every loan's stage matches its latest PD month.",
        )
    )
    business.append(
        _criterion(
            "EA-18",
            "Outstanding agrees with the PD file",
            "business",
            "review" if amount_gap else "pass",
            f"{_plural(len(amount_gap), 'loan')} differ by more than 1% from the latest PD Loan Amount: "
            f"{_sample(sorted(amount_gap))}. Check both files are for the same date."
            if amount_gap
            else "Outstanding amounts agree with the PD file within 1%.",
        )
    )

    capped = run.loan_summary[run.loan_summary[ECL_CAPPED] == "Yes"]
    business.append(
        _criterion(
            "EA-19",
            "ECL cap",
            "info",
            "info",
            f"{_plural(len(capped), 'loan')} capped at the Outstanding Amount: {_sample(capped[LOAN_ID].tolist())}."
            if len(capped)
            else "No loan's ECL exceeds its Outstanding Amount.",
        )
    )
    for check in run.reconciliation:
        outcome = (
            "pass" if check.passed else ("review" if check.name in _REVIEW_ONLY_CHECKS else "block")
        )
        business.append(
            _criterion(
                f"R-{run.reconciliation.index(check) + 1}",
                f"Reconciliation · {check.name}",
                "business",
                outcome,
                check.detail
                if check.passed
                else f"{check.detail}. Expected {_money(check.expected)}, actual {_money(check.actual)}.",
            )
        )
    business.append(
        _criterion(
            "EA-20",
            f"Engine {engine_version}",
            "info",
            "info",
            f"Spec v1.0: {_plural(len(run.loan_summary), 'loan')}, {_plural(len(rows), 'row')}, "
            f"Total ECL {_money(run.total_ecl)} (coverage {run.coverage_ratio:.2%}). Deterministic.",
        )
    )
    criteria = structural + business
    return EadValidation(_status(criteria), criteria, run, prepared, source)
