"""Validate-time PD preview — loan-level staging and observed transition matrices.

Pure, synchronous, DB-free. Every formula here comes from `pd_engine`; this
module arranges the results for display and does not re-derive any of them.

Two matrices exist per segment and they are deliberately different numbers:

* the **preview** matrix is 3x4 (Stage 1/2/3 -> Stage 1/2/3/Offbooks),
  row-normalized with off-book exits *inside* the denominator, so a cell reads
  "of the exposure that started in stage i, this fraction ended up in j,
  including leaving the book";
* the **projection** matrix `compute_pd` raises to powers is 3x3, normalized
  as if exits never happened, because an absorbing-state Markov model over the
  three IFRS 9 stages has nowhere to put an exit.

Both are correct for their purpose. An auditor comparing the preview matrix to
the computed PD curve must know which is which, hence this note and the
`dest_labels` on the wire.

`cure_rate` is reported on the projection (3x3) basis — it is the number that
flows into LGD via `ead_engine`, so the preview must not show a different one.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.engine.format_utils import format_compact_amount
from app.engine.pd_engine import (
    OFFBOOKS,
    STAGES,
    build_monthly_matrix,
    cure_rate_from_proportions,
    derive_next_month_staging,
    marginal_pd_from_cumulative,
    normalize_matrix,
    prepare_pd_frame,
    project_term_structure,
    row_proportions,
    segment_names,
)

DEST_LABELS = (*STAGES, OFFBOOKS)

# Built explicitly rather than via strftime("%b"), which is locale-dependent.
# Determinism is asserted by EC-18, so it has to hold on every host.
_MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

_STAGE_TO_WIRE: dict[str, int | str] = {
    "Stage 1": 1,
    "Stage 2": 2,
    "Stage 3": 3,
    OFFBOOKS: "offbooks",
}


def month_label(value: pd.Timestamp) -> str:
    """Display form of a reporting month, e.g. "Oct 2024"."""
    ts = pd.Timestamp(value)
    return f"{_MONTH_ABBR[ts.month - 1]} {ts.year}"


def month_key(value: pd.Timestamp) -> str:
    """Sortable form of a reporting month, e.g. "2024-10"."""
    ts = pd.Timestamp(value)
    return f"{ts.year:04d}-{ts.month:02d}"


def stage_to_wire(value: object) -> int | str:
    """Map an engine staging label onto the wire form 1 | 2 | 3 | "offbooks".

    Unrecognised values are passed through as their string form rather than
    silently defaulting to Stage 1. The preview is only built once the
    structural staging criterion has passed, so this is unreachable in
    practice; it exists so a bug shows up as visibly wrong instead of as a
    plausible performing loan.
    """
    key = str(value).strip()
    mapped = _STAGE_TO_WIRE.get(key)
    return mapped if mapped is not None else key


@dataclass(frozen=True, slots=True)
class PdLoanRow:
    """One loan-month row of the preview table."""

    loan_id: str
    segment: str
    reporting_month: str
    reporting_month_key: str
    staging: int | str
    next_staging: int | str
    is_final_month: bool


@dataclass(frozen=True, slots=True)
class PdSegmentStats:
    cure_rate: float
    loans_observed: int
    months_of_history: int


@dataclass(frozen=True, slots=True)
class PdSegmentPreview:
    """Display-ready per-segment preview."""

    segment: str
    matrix: list[list[float]]
    amounts: list[list[str]]
    dest_labels: list[str]
    stats: PdSegmentStats
    # Spec "Lifetime PD Term Structure" Steps 1-7, same arithmetic as compute_pd.
    proportions: list[list[float]]  # Step 1: 3x3, Offbooks dropped, rows sum to 1
    observed_3: list[list[str]]  # Step 1 input balances (3x3), pre-formatted
    normalized: list[list[float]]  # Step 3
    # Steps 4-7: [transition][month - 1] = [Stage 1, Stage 2, Stage 3, Marginal PD]
    term_structure: list[list[list[float]]]


@dataclass(frozen=True, slots=True)
class PdSegmentObservation:
    """Per-segment intermediates. Consumed by criteria evaluation, never serialized.

    `months` are every reporting month present for the segment.
    `transition_months` excludes the segment's final month: that month has no
    t+1 to join to, so every loan in it reads Offbooks. Those are end-of-window
    artefacts, not exits, and including them would drag the preview matrix and
    the Offbooks-rate check toward a fake mass exodus.
    """

    segment: str
    months: list[pd.Timestamp]
    transition_months: list[pd.Timestamp]
    monthly_matrices: dict[str, np.ndarray]
    aggregate_4: np.ndarray
    aggregate_3: np.ndarray
    proportions_4: np.ndarray
    proportions_3: np.ndarray
    cure_rate: float
    loans_observed: int
    exposure_by_stage: np.ndarray


@dataclass(frozen=True, slots=True)
class PdPreview:
    loans: list[PdLoanRow]
    segments: list[PdSegmentPreview]
    observations: list[PdSegmentObservation]
    engine_version: str


def prepare_preview_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Dedupe, sort, and attach next-month staging in one shared step."""
    return derive_next_month_staging(prepare_pd_frame(df))


def _segment_observation(segment: str, segment_df: pd.DataFrame) -> PdSegmentObservation:
    months = [pd.Timestamp(m) for m in sorted(segment_df["Reporting Month"].unique())]
    transition_months = months[:-1] if len(months) > 1 else []
    transition_keys = {month_key(m) for m in transition_months}

    monthly_matrices: dict[str, np.ndarray] = {}
    aggregate_4 = np.zeros((3, 4), dtype=np.float64)
    aggregate_3 = np.zeros((3, 3), dtype=np.float64)

    for reporting_month in months:
        month_df = segment_df[segment_df["Reporting Month"] == reporting_month]
        matrix = build_monthly_matrix(month_df)
        key = month_key(reporting_month)
        monthly_matrices[key] = matrix
        # The projection basis pools every month, exactly as compute_pd does,
        # so the cure rate derived from it matches the one feeding ECL.
        aggregate_3 += matrix[:, :3]
        if key in transition_keys:
            aggregate_4 += matrix

    proportions_4 = row_proportions(aggregate_4)
    proportions_3 = row_proportions(aggregate_3)

    exposure_by_stage = np.zeros(3, dtype=np.float64)
    amounts = pd.to_numeric(segment_df["Loan Amount"], errors="coerce").fillna(0.0)
    staging = segment_df["Staging"].astype(str).str.strip()
    for idx, stage in enumerate(STAGES):
        exposure_by_stage[idx] = float(amounts[staging == stage].sum())

    return PdSegmentObservation(
        segment=segment,
        months=months,
        transition_months=transition_months,
        monthly_matrices=monthly_matrices,
        aggregate_4=aggregate_4,
        aggregate_3=aggregate_3,
        proportions_4=proportions_4,
        proportions_3=proportions_3,
        cure_rate=cure_rate_from_proportions(proportions_3),
        loans_observed=int(segment_df["Loan ID"].astype(str).nunique()),
        exposure_by_stage=exposure_by_stage,
    )


def _term_structure(normalized: np.ndarray) -> list[list[list[float]]]:
    term = project_term_structure(normalized)
    out = []
    for rows in term:
        marginal = marginal_pd_from_cumulative(rows[:, 2])
        out.append(
            [
                [float(s1), float(s2), float(s3), float(m)]
                for (s1, s2, s3), m in zip(rows, marginal, strict=True)
            ]
        )
    return out


def _to_segment_preview(observation: PdSegmentObservation) -> PdSegmentPreview:
    normalized = normalize_matrix(observation.proportions_3)
    return PdSegmentPreview(
        segment=observation.segment,
        # Full float64 on the wire; the client renders, it does not re-derive.
        matrix=[[float(v) for v in row] for row in observation.proportions_4],
        # Pre-formatted for display: the backend owns abbreviation and precision.
        amounts=[[format_compact_amount(float(v)) for v in row] for row in observation.aggregate_4],
        dest_labels=list(DEST_LABELS),
        stats=PdSegmentStats(
            cure_rate=observation.cure_rate,
            loans_observed=observation.loans_observed,
            months_of_history=len(observation.months),
        ),
        proportions=[[float(v) for v in row] for row in observation.proportions_3],
        observed_3=[
            [format_compact_amount(float(v)) for v in row] for row in observation.aggregate_3
        ],
        normalized=[[float(v) for v in row] for row in normalized],
        term_structure=_term_structure(normalized),
    )


def build_pd_preview(joined: pd.DataFrame, *, engine_version: str) -> PdPreview:
    """Build the loan-level rows and per-segment matrices from a prepared frame.

    `joined` must already have been through `prepare_preview_frame`.
    Side-effect free and deterministic: identical input yields identical output.
    """
    if joined.empty:
        return PdPreview(loans=[], segments=[], observations=[], engine_version=engine_version)

    final_month_by_segment: dict[str, pd.Timestamp] = {
        str(segment): pd.Timestamp(latest)
        for segment, latest in joined.groupby("SEGMENT", sort=False)["Reporting Month"]
        .max()
        .items()
    }

    frame = joined.sort_values(["SEGMENT", "Loan ID", "Reporting Month"], kind="mergesort")
    loans = [
        PdLoanRow(
            loan_id=str(loan_id),
            segment=str(segment),
            reporting_month=month_label(reporting_month),
            reporting_month_key=month_key(reporting_month),
            staging=stage_to_wire(staging),
            next_staging=stage_to_wire(next_staging),
            # The final month of a segment has no t+1, so every loan in it
            # reads Offbooks. Flagged rather than dropped: a loan that only
            # appears in that month would otherwise vanish from the preview.
            is_final_month=reporting_month == final_month_by_segment.get(str(segment)),
        )
        for loan_id, segment, reporting_month, staging, next_staging in zip(
            frame["Loan ID"].tolist(),
            frame["SEGMENT"].tolist(),
            [pd.Timestamp(m) for m in frame["Reporting Month"].tolist()],
            frame["Staging"].tolist(),
            frame["Next Month Staging"].tolist(),
            strict=True,
        )
    ]

    observations = [
        _segment_observation(str(segment), joined[joined["SEGMENT"] == segment])
        for segment in segment_names(joined)
    ]

    return PdPreview(
        loans=loans,
        segments=[_to_segment_preview(o) for o in observations],
        observations=observations,
        engine_version=engine_version,
    )
