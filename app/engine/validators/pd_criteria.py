"""PD validation criteria — the full pass/fail checklist behind ?scope=pd.

Pure, synchronous, DB-free. Thresholds arrive as a keyword-only dataclass so
this module never imports a model or touches a session.

Unlike `pd_validator`, which only ever emits blocking issues, every criterion
here reports an outcome — including `pass`. A checklist that can only say
"something is wrong" cannot tell a reviewer that a check actually ran, and an
unrun check silently reading as fine is the failure mode this module exists to
prevent. Criteria that cannot be evaluated say so explicitly.

Code registry
-------------
`test_data/README.md` already spends EC-02..EC-09 on LGD, EAD, and cross-file
checks. Reusing those numbers for PD structural checks would give one code two
meanings depending on which file it came from, which makes the audit trail
ambiguous. PD structural checks therefore get their own PD-NN namespace:

    PD-01  Required columns present
    PD-02  Loan ID present
    PD-03  Segment present
    PD-04  Reporting month valid
    PD-05  Staging value valid
    PD-06  Loan amount non-negative
    PD-07  Segment recognized
    EC-10  No duplicate (Loan ID, reporting month)   [pre-existing PD code]
    EC-11  Chronological month coverage
    EC-12  Loan ID format consistency
    EC-13  Minimum transition history
    EC-14  Stage-mix plausibility
    EC-15  Marginal PD bounds                        [blocking]
    EC-16  Cure rate sanity
    EC-17  Offbooks rate sanity
    EC-18  Reproducibility / engine version          [info]
    EC-19  Degenerate transition rows

EC-19 is new. When a segment has zero observed exposure in a starting stage,
that row of the transition matrix is all zeros, every projected month for that
starting stage yields Marginal_PD = 0, and the segment quietly reports no
lifetime credit risk at all. Nothing warned about this before.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, SupportsInt

import numpy as np
import pandas as pd

from app.engine.pd_engine import (
    REQUIRED_PD_COLUMNS,
    STAGES,
    compute_pd,
    is_pd_data_sheet,
    pd_columns_present,
    resolve_reporting_month_column,
)
from app.engine.pd_preview import (
    PdPreview,
    PdSegmentObservation,
    month_key,
    month_label,
)
from app.engine.validators.base import MAX_ISSUES, STAGING_VALUES, ValidationIssue

CriterionCategory = Literal["structural", "business", "info"]
CriterionOutcome = Literal["pass", "review", "block", "info"]

SOURCE_FILE = "_pd_source_file"
SOURCE_SHEET = "_pd_source_sheet"
SOURCE_ROW = "_pd_source_row"
_PROVENANCE_COLUMNS = (SOURCE_FILE, SOURCE_SHEET, SOURCE_ROW)

# A sheet matching fewer than this many required columns is treated as a cover
# page or notes tab and skipped. At or above it, the sheet was clearly meant to
# be PD data, so missing columns are reported rather than silently ignored.
_DATA_SHEET_HINT_THRESHOLD = 2

_NOT_EVALUATED = "Not evaluated — the structural checks above must pass first."

# Floating-point slack. Probabilities are products of float64 matrix powers, so
# exact 0/1 comparisons produce false blocks on well-formed data.
_PROB_EPS = 1e-9


@dataclass(frozen=True, slots=True, kw_only=True)
class PdThresholds:
    """Tenant-configurable validation bounds. Defaults match the platform baseline."""

    max_month_gap: int = 1
    min_transitions: int = 3
    stage3_share_min: float = 0.0
    stage3_share_max: float = 0.40
    cure_rate_min: float = 0.02
    cure_rate_max: float = 0.90
    offbooks_abs_band: float = 0.10


@dataclass(frozen=True, slots=True)
class CriterionResult:
    id: str
    code: str
    name: str
    category: CriterionCategory
    outcome: CriterionOutcome
    rationale: str


@dataclass(frozen=True, slots=True)
class PdSourceSheet:
    """One sheet of one uploaded PD workbook, with the provenance to cite it."""

    filename: str
    sheet_name: str
    frame: pd.DataFrame


@dataclass(frozen=True, slots=True)
class PdValidationOutcome:
    status: Literal["ok", "warn", "blocking"]
    criteria: list[CriterionResult]
    preview: PdPreview
    issues: list[ValidationIssue]
    issues_truncated: bool
    data_sheet_count: int
    skipped_sheet_count: int
    row_count: int


@dataclass
class _IssueSink:
    """Collects row-level issues under a per-response cap.

    The cap limits what is *reported*, never what is *detected*: criterion
    outcomes are computed from full-column masks before anything is emitted, so
    hitting the cap can never turn a failing check into a passing one.
    """

    issues: list[ValidationIssue] = field(default_factory=list)
    truncated: bool = False

    def add(
        self,
        *,
        level: Literal["warn", "block"],
        title: str,
        location: str,
        fix: str,
        category: str | None = None,
    ) -> None:
        if len(self.issues) >= MAX_ISSUES:
            self.truncated = True
            return
        self.issues.append(
            ValidationIssue(
                level=level, title=title, location=location, fix=fix, category=category
            )
        )

    def finalize(self) -> list[ValidationIssue]:
        """Return the issues, with a closing notice if anything was withheld.

        Called on every exit path. A silently short list would read as "these
        are all the problems", which is the opposite of what the cap means.
        """
        if not self.truncated:
            return self.issues
        return [
            *self.issues,
            ValidationIssue(
                level="block",
                title="Additional errors were found but not shown",
                location="Summary",
                fix=(
                    f"Fix the errors above and re-validate. Up to {MAX_ISSUES} issues "
                    "are shown per validation."
                ),
            ),
        ]


def _criterion(
    code: str,
    name: str,
    category: CriterionCategory,
    outcome: CriterionOutcome,
    rationale: str,
) -> CriterionResult:
    return CriterionResult(
        id=code.lower(),
        code=code,
        name=name,
        category=category,
        outcome=outcome,
        rationale=rationale,
    )


def _join(items: Sequence[str], limit: int = 3) -> str:
    shown = list(items[:limit])
    remainder = len(items) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {remainder} more" if remainder > 0 else text


def _excel_row(index: SupportsInt) -> int:
    """1-based Excel row for a pandas index, accounting for the header row."""
    return int(index) + 2


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


# ── Sheet classification and combination ──────────────────────────────────────


def classify_sheets(
    sources: Sequence[PdSourceSheet],
) -> tuple[list[PdSourceSheet], list[PdSourceSheet], list[PdSourceSheet]]:
    """Split sheets into (data, malformed, skipped).

    Malformed sheets look like they were meant to hold PD data but are missing
    required columns; they block. Skipped sheets are cover pages and notes tabs
    and are reported in EC-18 so the exclusion is visible rather than silent.
    """
    data: list[PdSourceSheet] = []
    malformed: list[PdSourceSheet] = []
    skipped: list[PdSourceSheet] = []
    for source in sources:
        if is_pd_data_sheet(source.frame):
            data.append(source)
        elif len(pd_columns_present(source.frame)) >= _DATA_SHEET_HINT_THRESHOLD:
            malformed.append(source)
        else:
            skipped.append(source)
    return data, malformed, skipped


def combine_pd_sheets(sources: Sequence[PdSourceSheet]) -> pd.DataFrame:
    """Concatenate PD data sheets, carrying provenance so issues can cite a cell.

    Sheets are concatenated in the caller's order, which is upload `created_at`
    then sheet order. That order decides which row wins deduplication, so it is
    part of the contract, not an implementation detail.
    """
    frames: list[pd.DataFrame] = []
    for source in sources:
        if source.frame.empty:
            continue
        frame = source.frame.copy()
        reporting_col = resolve_reporting_month_column(frame)
        if reporting_col != "Reporting Month" and reporting_col in frame.columns:
            frame = frame.rename(columns={reporting_col: "Reporting Month"})
        frame[SOURCE_FILE] = source.filename
        frame[SOURCE_SHEET] = source.sheet_name
        frame[SOURCE_ROW] = [_excel_row(i) for i in frame.index]
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    from app.engine.workbook import concat_sheets

    return concat_sheets(frames)


def strip_provenance(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in _PROVENANCE_COLUMNS if c in df.columns])


def _location(row: pd.Series, column: str) -> str:
    filename = row.get(SOURCE_FILE, "upload")
    sheet = row.get(SOURCE_SHEET, "sheet")
    excel_row = row.get(SOURCE_ROW, "?")
    return f"{filename} · {sheet}, row {excel_row}, column {column}"


# ── Structural detection masks ───────────────────────────────────────────────


def _blank_mask(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype(str).str.strip().eq("")


def _bad_date_mask(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce")
    return series.isna() | parsed.isna()


def _bad_enum_mask(series: pd.Series, allowed: frozenset[str] | set[str]) -> pd.Series:
    return series.isna() | ~series.astype(str).str.strip().isin(allowed)


def _bad_amount_mask(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    return numeric.isna() | (numeric < 0)


def _row_criterion(
    combined: pd.DataFrame,
    sink: _IssueSink,
    *,
    code: str,
    name: str,
    column: str,
    mask: pd.Series,
    issue_title: str,
    fix: str,
    pass_rationale: str,
    fail_rationale: str,
) -> CriterionResult:
    failures = int(mask.sum())
    if failures == 0:
        return _criterion(code, name, "structural", "pass", pass_rationale)
    for index in combined.index[mask]:
        sink.add(
            level="block",
            title=issue_title,
            location=_location(combined.loc[index], column),
            fix=fix,
        )
    return _criterion(
        code,
        name,
        "structural",
        "block",
        fail_rationale.format(
            count=failures, rows="row has" if failures == 1 else "rows have"
        ),
    )


# ── Structural criteria ──────────────────────────────────────────────────────


def _required_columns_criterion(
    malformed: Sequence[PdSourceSheet],
    data_sheets: Sequence[PdSourceSheet],
    row_count: int,
    sink: _IssueSink,
) -> CriterionResult:
    expected = {*REQUIRED_PD_COLUMNS, "Reporting Month"}
    if not data_sheets and not malformed:
        return _criterion(
            "PD-01",
            "Required columns present",
            "structural",
            "block",
            "No sheet in the uploaded workbook(s) contains PD loan data. Expected "
            "columns Loan ID, SEGMENT, Reporting Month, Staging and Loan Amount.",
        )
    if not malformed and row_count == 0:
        return _criterion(
            "PD-01",
            "Required columns present",
            "structural",
            "block",
            f"The {len(data_sheets)} PD data sheet(s) have the right columns but contain "
            "no rows.",
        )
    if not malformed:
        return _criterion(
            "PD-01",
            "Required columns present",
            "structural",
            "pass",
            "Loan ID, SEGMENT, Reporting Month, Staging and Loan Amount are present "
            f"in all {len(data_sheets)} PD data sheet(s).",
        )

    details: list[str] = []
    for source in malformed:
        missing = sorted(expected - pd_columns_present(source.frame))
        details.append(f"{source.filename} · {source.sheet_name} (missing {', '.join(missing)})")
        for column in missing:
            sink.add(
                level="block",
                title=f"Missing required column: {column}",
                location=f"{source.filename} · {source.sheet_name}, header row",
                fix=(
                    "Download the official PD template and copy your data into it, "
                    f"or add a column named '{column}'."
                ),
                category="template_format",
            )
    return _criterion(
        "PD-01",
        "Required columns present",
        "structural",
        "block",
        f"Required PD columns are missing from {_join(details)}.",
    )


def _segment_recognized_criterion(
    combined: pd.DataFrame, allowed_segments: set[str], sink: _IssueSink
) -> CriterionResult:
    if not allowed_segments:
        return _criterion(
            "PD-07",
            "Segment recognized",
            "structural",
            "review",
            "Not evaluated — this workspace has no configured segments to check against.",
        )
    allowed = {segment.strip() for segment in allowed_segments}
    values = combined["SEGMENT"].astype(str).str.strip()
    mask = combined["SEGMENT"].notna() & ~values.isin(allowed)
    unknown = sorted(values[mask].unique())
    if not unknown:
        return _criterion(
            "PD-07",
            "Segment recognized",
            "structural",
            "pass",
            "Every SEGMENT value matches one this workspace has configured.",
        )
    for value in unknown:
        count = int((values == value).sum())
        sink.add(
            level="block",
            title="SEGMENT is not configured for this tenant",
            location=f"column SEGMENT ({count} row{'s' if count != 1 else ''})",
            fix=f"Use a segment from your tenant configuration (found {value!r}).",
        )
    return _criterion(
        "PD-07",
        "Segment recognized",
        "structural",
        "block",
        f"{_join(unknown)} {'is' if len(unknown) == 1 else 'are'} not configured "
        "for this workspace.",
    )


def _duplicate_criterion(combined: pd.DataFrame, sink: _IssueSink) -> CriterionResult:
    """EC-10 — uniqueness on (Loan ID, calendar month), not on the exact date.

    The calendar month is the join key for next-month staging. Two rows for one
    loan in one month are therefore duplicates even when their day-of-month
    differs, and they would otherwise expand the money-weighted matrix.
    """
    parsed = pd.to_datetime(combined["Reporting Month"], errors="coerce")
    usable = parsed.notna() & combined["Loan ID"].notna()
    if not usable.any():
        return _criterion(
            "EC-10",
            "No duplicate rows",
            "structural",
            "review",
            _NOT_EVALUATED,
        )
    key = pd.DataFrame(
        {"Loan ID": combined["Loan ID"].astype(str), "_period": parsed.dt.to_period("M")}
    )
    mask = key.duplicated(keep=False) & usable
    if not mask.any():
        return _criterion(
            "EC-10",
            "No duplicate rows",
            "structural",
            "pass",
            "No (Loan ID, reporting month) pair repeats across the uploaded sheets.",
        )
    affected = combined.index[mask]
    examples = sorted({str(combined.loc[i, "Loan ID"]) for i in affected})
    for index in affected:
        row = combined.loc[index]
        month = parsed.loc[index]
        sink.add(
            level="block",
            title="Duplicate Loan ID and Reporting Month (EC-10)",
            location=_location(row, "Loan ID"),
            fix=(
                f"Remove duplicate rows for Loan ID={row['Loan ID']!r}, "
                f"month={month.strftime('%Y-%m')}."
            ),
        )
    return _criterion(
        "EC-10",
        "No duplicate rows",
        "structural",
        "block",
        f"{len(affected)} rows repeat a (Loan ID, reporting month) pair — {_join(examples)}. "
        "Same-month rows with different days count as duplicates because the "
        "calendar month is the transition join key.",
    )


def _structural_criteria(
    combined: pd.DataFrame,
    data_sheets: Sequence[PdSourceSheet],
    malformed: Sequence[PdSourceSheet],
    allowed_segments: set[str],
    sink: _IssueSink,
) -> list[CriterionResult]:
    results = [_required_columns_criterion(malformed, data_sheets, len(combined), sink)]

    if combined.empty:
        for code, name in (
            ("PD-02", "Loan ID present"),
            ("PD-03", "Segment present"),
            ("PD-04", "Reporting month valid"),
            ("PD-05", "Staging value valid"),
            ("PD-06", "Loan amount non-negative"),
            ("PD-07", "Segment recognized"),
            ("EC-10", "No duplicate rows"),
        ):
            results.append(_criterion(code, name, "structural", "review", _NOT_EVALUATED))
        return results

    results.append(
        _row_criterion(
            combined,
            sink,
            code="PD-02",
            name="Loan ID present",
            column="Loan ID",
            mask=_blank_mask(combined["Loan ID"]),
            issue_title="Loan ID is empty",
            fix="Provide a non-empty Loan ID for every row.",
            pass_rationale="Every row has a non-blank Loan ID.",
            fail_rationale="{count} {rows} a blank Loan ID.",
        )
    )
    results.append(
        _row_criterion(
            combined,
            sink,
            code="PD-03",
            name="Segment present",
            column="SEGMENT",
            mask=_blank_mask(combined["SEGMENT"]),
            issue_title="SEGMENT is empty",
            fix="Provide a non-empty SEGMENT for every row.",
            pass_rationale="Every row has a non-blank SEGMENT value.",
            fail_rationale="{count} {rows} a blank SEGMENT.",
        )
    )
    results.append(
        _row_criterion(
            combined,
            sink,
            code="PD-04",
            name="Reporting month valid",
            column="Reporting Month",
            mask=_bad_date_mask(combined["Reporting Month"]),
            issue_title="Reporting Month is not a valid date",
            fix="Use a valid date for Reporting Month.",
            pass_rationale="Every Reporting Month parses as a real calendar date.",
            fail_rationale="{count} {rows} a Reporting Month that is not a valid date.",
        )
    )
    results.append(
        _row_criterion(
            combined,
            sink,
            code="PD-05",
            name="Staging value valid",
            column="Staging",
            mask=_bad_enum_mask(combined["Staging"], STAGING_VALUES),
            issue_title="Invalid Staging value",
            fix='Use exactly one of "Stage 1", "Stage 2", or "Stage 3".',
            pass_rationale='Every Staging value is exactly "Stage 1", "Stage 2" or "Stage 3".',
            fail_rationale="{count} {rows} a Staging value outside Stage 1/2/3.",
        )
    )
    results.append(
        _row_criterion(
            combined,
            sink,
            code="PD-06",
            name="Loan amount non-negative",
            column="Loan Amount",
            mask=_bad_amount_mask(combined["Loan Amount"]),
            issue_title="Loan Amount must be non-negative",
            fix="Enter a Loan Amount greater than or equal to 0.",
            pass_rationale="Every Loan Amount is numeric and zero or greater.",
            fail_rationale="{count} {rows} a missing, non-numeric or negative Loan Amount.",
        )
    )
    results.append(_segment_recognized_criterion(combined, allowed_segments, sink))
    results.append(_duplicate_criterion(combined, sink))
    return results


# ── Business and statistical criteria ────────────────────────────────────────


def _month_coverage_criterion(
    observations: Sequence[PdSegmentObservation], thresholds: PdThresholds
) -> CriterionResult:
    gapped: list[str] = []
    for observation in observations:
        periods = [pd.Timestamp(m).to_period("M") for m in observation.months]
        if len(periods) < 2:
            continue
        missing: list[str] = []
        for earlier, later in zip(periods, periods[1:], strict=False):
            gap = int((later - earlier).n)
            if gap > thresholds.max_month_gap:
                missing.append(f"{earlier} to {later}")
        if missing:
            gapped.append(f"{observation.segment} ({_join(missing, 2)})")
    if not gapped:
        return _criterion(
            "EC-11",
            "Chronological month coverage",
            "business",
            "pass",
            "Reporting months run consecutively in every segment — no gaps to "
            "silently distort the transition matrix.",
        )
    return _criterion(
        "EC-11",
        "Chronological month coverage",
        "business",
        "review",
        f"Missing reporting months in {_join(gapped)}. A skipped month is read as an "
        "off-book exit followed by a re-entry, which understates persistence.",
    )


_SHAPE_PATTERN = re.compile(r"[A-Za-z]+|\d+")


def _id_shape(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return "#" * len(token) if token.isdigit() else "A" * len(token)

    return _SHAPE_PATTERN.sub(replace, value.strip())


def _loan_id_format_criterion(combined: pd.DataFrame) -> CriterionResult:
    ids = sorted({str(v).strip() for v in combined["Loan ID"].dropna().unique() if str(v).strip()})
    if not ids:
        return _criterion(
            "EC-12", "Loan ID format consistency", "business", "review", _NOT_EVALUATED
        )
    shapes: dict[str, list[str]] = {}
    for loan_id in ids:
        shapes.setdefault(_id_shape(loan_id), []).append(loan_id)
    if len(shapes) == 1:
        example = ids[0]
        return _criterion(
            "EC-12",
            "Loan ID format consistency",
            "business",
            "pass",
            f"All {len(ids)} Loan IDs follow one consistent pattern ({example}).",
        )
    ranked = sorted(shapes.items(), key=lambda item: (-len(item[1]), item[0]))
    dominant_shape, dominant_ids = ranked[0]
    odd = [loan_id for _, group in ranked[1:] for loan_id in group]
    return _criterion(
        "EC-12",
        "Loan ID format consistency",
        "business",
        "review",
        f"{len(dominant_ids)} Loan IDs match the dominant pattern ({dominant_ids[0]}) but "
        f"{len(odd)} {'does' if len(odd) == 1 else 'do'} not — {_join(odd)}. Mixed formats "
        "usually mean the same borrower appears under two identifiers, which breaks the "
        "month-to-month join.",
    )


def _transition_history_criterion(
    observations: Sequence[PdSegmentObservation], thresholds: PdThresholds
) -> CriterionResult:
    thin = [
        f"{o.segment} ({len(o.transition_months)})"
        for o in observations
        if len(o.transition_months) < thresholds.min_transitions
    ]
    if not thin:
        return _criterion(
            "EC-13",
            "Minimum transition history",
            "business",
            "pass",
            f"Every segment has at least {thresholds.min_transitions} monthly transitions "
            "behind its matrix.",
        )
    return _criterion(
        "EC-13",
        "Minimum transition history",
        "business",
        "review",
        f"Fewer than {thresholds.min_transitions} monthly transitions observed for "
        f"{_join(thin)}. The matrix is usable but will move materially as more "
        "months are uploaded.",
    )


def _stage_mix_criterion(
    observations: Sequence[PdSegmentObservation], thresholds: PdThresholds
) -> CriterionResult:
    implausible: list[str] = []
    for observation in observations:
        total = float(observation.exposure_by_stage.sum())
        if total <= 0:
            continue
        share = float(observation.exposure_by_stage[2]) / total
        if share < thresholds.stage3_share_min or share > thresholds.stage3_share_max:
            implausible.append(f"{observation.segment} ({_pct(share)})")
    if not implausible:
        return _criterion(
            "EC-14",
            "Stage-mix plausibility",
            "business",
            "pass",
            "The Stage 1/2/3 exposure split falls within the expected range for every "
            f"segment (Stage 3 at or below {_pct(thresholds.stage3_share_max)}).",
        )
    return _criterion(
        "EC-14",
        "Stage-mix plausibility",
        "business",
        "review",
        f"Stage 3 exposure share is outside the configured "
        f"{_pct(thresholds.stage3_share_min)}–{_pct(thresholds.stage3_share_max)} band for "
        f"{_join(implausible)}. Shares are exposure-weighted, not loan counts.",
    )


def _marginal_pd_criterion(pd_output: pd.DataFrame) -> CriterionResult:
    """EC-15 — the only blocking statistical check.

    A probability outside [0, 1] or a cumulative default curve that goes
    backwards is not a data-quality opinion; it means the projected numbers are
    arithmetically wrong and must not reach a financial statement.
    """
    if pd_output.empty or "Marginal_PD" not in pd_output.columns:
        return _criterion("EC-15", "Marginal PD bounds", "business", "review", _NOT_EVALUATED)

    marginal = pd_output["Marginal_PD"].to_numpy(dtype=np.float64)
    out_of_bounds = int(
        np.count_nonzero(
            ~np.isfinite(marginal) | (marginal < -_PROB_EPS) | (marginal > 1.0 + _PROB_EPS)
        )
    )

    regressions = 0
    for _, group in pd_output.groupby(["SEGMENT", "Transition"], sort=False):
        cumulative = group.sort_values("Month", kind="mergesort")["Stage_3_prob"].to_numpy(
            dtype=np.float64
        )
        regressions += int(np.count_nonzero(np.diff(cumulative) < -_PROB_EPS))

    if out_of_bounds == 0 and regressions == 0:
        return _criterion(
            "EC-15",
            "Marginal PD bounds",
            "business",
            "pass",
            "Every computed default probability is between 0% and 100%, and the "
            "cumulative Stage 3 probability never decreases over the 300-month horizon.",
        )
    parts = []
    if out_of_bounds:
        parts.append(f"{out_of_bounds} marginal PD value(s) outside 0–100%")
    if regressions:
        parts.append(f"{regressions} month(s) where cumulative default probability decreases")
    return _criterion(
        "EC-15",
        "Marginal PD bounds",
        "business",
        "block",
        f"Projected PD is not arithmetically valid: {' and '.join(parts)}. "
        "This blocks computation.",
    )


def _cure_rate_criterion(
    observations: Sequence[PdSegmentObservation], thresholds: PdThresholds
) -> CriterionResult:
    checked: list[PdSegmentObservation] = [
        o for o in observations if float(o.aggregate_3[2].sum()) > 0
    ]
    if not checked:
        return _criterion(
            "EC-16",
            "Cure rate sanity",
            "business",
            "review",
            "Not evaluated — no segment has any observed Stage 3 exposure to cure from.",
        )
    outliers = [
        f"{o.segment} ({_pct(o.cure_rate)})"
        for o in checked
        if o.cure_rate < thresholds.cure_rate_min or o.cure_rate > thresholds.cure_rate_max
    ]
    band = f"{_pct(thresholds.cure_rate_min)}–{_pct(thresholds.cure_rate_max)}"
    if not outliers:
        rates = ", ".join(f"{o.segment} {_pct(o.cure_rate)}" for o in checked[:3])
        return _criterion(
            "EC-16",
            "Cure rate sanity",
            "business",
            "pass",
            f"Stage 3 loans recovering to a performing stage — {rates} — all sit within "
            f"the plausible {band} band.",
        )
    return _criterion(
        "EC-16",
        "Cure rate sanity",
        "business",
        "review",
        f"Cure rate outside the configured {band} band for {_join(outliers)}. The cure "
        "rate feeds LGD, so an implausible value moves ECL directly.",
    )


def _offbooks_criterion(
    observations: Sequence[PdSegmentObservation], thresholds: PdThresholds
) -> CriterionResult:
    """EC-17 — a month whose off-book exits spike above the segment's own trend.

    The final reporting month of each segment is excluded upstream: it has no
    t+1, so every loan in it reads Offbooks. Including it would report a mass
    exodus every single time.
    """
    evaluated = False
    spikes: list[str] = []
    for observation in observations:
        if len(observation.transition_months) < 2:
            continue
        evaluated = True
        shares: list[float] = []
        for month in observation.transition_months:
            matrix = observation.monthly_matrices[month_key(month)]
            total = float(matrix.sum())
            shares.append(float(matrix[:, 3].sum()) / total if total > 0 else 0.0)
        for idx in range(1, len(shares)):
            trailing = sum(shares[:idx]) / idx
            if shares[idx] > trailing + thresholds.offbooks_abs_band:
                spikes.append(
                    f"{observation.segment} {month_label(observation.transition_months[idx])} "
                    f"({_pct(shares[idx])} vs {_pct(trailing)} trailing)"
                )
    if not evaluated:
        return _criterion(
            "EC-17",
            "Offbooks rate sanity",
            "business",
            "review",
            "Not evaluated — no segment has two or more transition months to compare "
            "off-book exit rates across.",
        )
    if not spikes:
        return _criterion(
            "EC-17",
            "Offbooks rate sanity",
            "business",
            "pass",
            "Off-book exits stay within "
            f"{_pct(thresholds.offbooks_abs_band)} of each segment's trailing average.",
        )
    return _criterion(
        "EC-17",
        "Offbooks rate sanity",
        "business",
        "review",
        f"Off-book exits spike above the trailing average in {_join(spikes)}. "
        "Worth confirming these are genuine closures rather than a missing month of data.",
    )


def _degenerate_rows_criterion(observations: Sequence[PdSegmentObservation]) -> CriterionResult:
    degenerate: list[str] = []
    for observation in observations:
        for idx, stage in enumerate(STAGES):
            if float(observation.aggregate_3[idx].sum()) == 0.0:
                degenerate.append(f"{observation.segment} · {stage}")
    if not degenerate:
        return _criterion(
            "EC-19",
            "Degenerate transition rows",
            "business",
            "pass",
            "Every starting stage in every segment has observed exposure, so no PD curve "
            "is zero by construction.",
        )
    return _criterion(
        "EC-19",
        "Degenerate transition rows",
        "business",
        "review",
        f"No observed exposure starting from {_join(degenerate)}. Those rows of the "
        "transition matrix are all zeros, so every projected month reports a 0% "
        "lifetime PD for that starting stage — a silent zero, not a measured one.",
    )


def _engine_stamp_criterion(
    *,
    engine_version: str,
    engine_method: str,
    file_count: int,
    data_sheet_count: int,
    skipped_sheet_count: int,
    row_count: int,
    combine_pd_files: bool,
) -> CriterionResult:
    parts = [
        f"Generated by engine {engine_version} ({engine_method}) from {row_count:,} rows "
        f"across {data_sheet_count} data sheet(s) in {file_count} PD file(s).",
    ]
    if skipped_sheet_count:
        parts.append(
            f"{skipped_sheet_count} sheet(s) were skipped as non-data (no PD columns)."
        )
    if file_count > 1 and not combine_pd_files:
        parts.append(
            "This preview combines all PD files, but 'Combine PD files' is off for this "
            "run, so computation will use only the first file — the computed result will "
            "not match this preview."
        )
    parts.append("Nothing has been saved; re-running on the same files reproduces this exactly.")
    return _criterion(
        "EC-18", "Reproducibility / engine version", "info", "info", " ".join(parts)
    )


def _unevaluated_business_criteria() -> list[CriterionResult]:
    return [
        _criterion("EC-11", "Chronological month coverage", "business", "review", _NOT_EVALUATED),
        _criterion("EC-12", "Loan ID format consistency", "business", "review", _NOT_EVALUATED),
        _criterion("EC-13", "Minimum transition history", "business", "review", _NOT_EVALUATED),
        _criterion("EC-14", "Stage-mix plausibility", "business", "review", _NOT_EVALUATED),
        _criterion("EC-15", "Marginal PD bounds", "business", "review", _NOT_EVALUATED),
        _criterion("EC-16", "Cure rate sanity", "business", "review", _NOT_EVALUATED),
        _criterion("EC-17", "Offbooks rate sanity", "business", "review", _NOT_EVALUATED),
        _criterion("EC-19", "Degenerate transition rows", "business", "review", _NOT_EVALUATED),
    ]


# ── Entry point ──────────────────────────────────────────────────────────────


def evaluate_pd_validation(
    sources: Sequence[PdSourceSheet],
    *,
    allowed_segments: set[str],
    thresholds: PdThresholds,
    engine_version: str,
    engine_method: str,
    file_count: int,
    combine_pd_files: bool,
) -> PdValidationOutcome:
    """Run the whole PD checklist and build the preview in one pass.

    Deterministic: the same sheets in the same order always produce the same
    criteria, the same rationales, and the same preview numbers.
    """
    data_sheets, malformed, skipped = classify_sheets(sources)
    combined = combine_pd_sheets(data_sheets)
    sink = _IssueSink()

    criteria = _structural_criteria(combined, data_sheets, malformed, allowed_segments, sink)
    structural_ok = not any(c.outcome == "block" for c in criteria)

    if not structural_ok:
        # Dependent checks are reported as unevaluated rather than passed. The
        # preview stays an empty-but-valid object so the client renders the real
        # failure instead of falling back to fabricated sample data.
        criteria.extend(_unevaluated_business_criteria())
        criteria.append(
            _engine_stamp_criterion(
                engine_version=engine_version,
                engine_method=engine_method,
                file_count=file_count,
                data_sheet_count=len(data_sheets),
                skipped_sheet_count=len(skipped),
                row_count=len(combined),
                combine_pd_files=combine_pd_files,
            )
        )
        return PdValidationOutcome(
            status="blocking",
            criteria=criteria,
            preview=PdPreview(
                loans=[], segments=[], observations=[], engine_version=engine_version
            ),
            issues=sink.finalize(),
            issues_truncated=sink.truncated,
            data_sheet_count=len(data_sheets),
            skipped_sheet_count=len(skipped),
            row_count=len(combined),
        )

    engine_frame = strip_provenance(combined)
    # One call to the audited pipeline. compute_pd builds the observed preview
    # from the same prepared frame it projects, so validation and results
    # cannot drift.
    pd_output, _cure_rates, intermediates = compute_pd(engine_frame)
    preview = intermediates["preview"]

    criteria.extend(
        [
            _month_coverage_criterion(preview.observations, thresholds),
            _loan_id_format_criterion(combined),
            _transition_history_criterion(preview.observations, thresholds),
            _stage_mix_criterion(preview.observations, thresholds),
            _marginal_pd_criterion(pd_output),
            _cure_rate_criterion(preview.observations, thresholds),
            _offbooks_criterion(preview.observations, thresholds),
            _degenerate_rows_criterion(preview.observations),
            _engine_stamp_criterion(
                engine_version=engine_version,
                engine_method=engine_method,
                file_count=file_count,
                data_sheet_count=len(data_sheets),
                skipped_sheet_count=len(skipped),
                row_count=len(combined),
                combine_pd_files=combine_pd_files,
            ),
        ]
    )

    for criterion in criteria:
        if criterion.outcome == "block" and criterion.category != "structural":
            sink.add(
                level="block",
                title=f"{criterion.code} · {criterion.name}",
                location="PD dataset",
                fix=criterion.rationale,
            )

    if any(c.outcome == "block" for c in criteria):
        status: Literal["ok", "warn", "blocking"] = "blocking"
    elif any(c.outcome == "review" for c in criteria):
        status = "warn"
    else:
        status = "ok"

    return PdValidationOutcome(
        status=status,
        criteria=criteria,
        preview=preview,
        issues=sink.finalize(),
        issues_truncated=sink.truncated,
        data_sheet_count=len(data_sheets),
        skipped_sheet_count=len(skipped),
        row_count=len(combined),
    )


__all__ = [
    "CriterionCategory",
    "CriterionOutcome",
    "CriterionResult",
    "PdSourceSheet",
    "PdThresholds",
    "PdValidationOutcome",
    "classify_sheets",
    "combine_pd_sheets",
    "evaluate_pd_validation",
    "strip_provenance",
]
