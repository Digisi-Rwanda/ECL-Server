"""PD criteria checklist tests.

Every criterion is exercised both ways. A check that can only be seen failing
is a check nobody can prove ran, which is the whole reason this module reports
`pass` explicitly instead of staying silent.
"""

import numpy as np
import pandas as pd

from app.engine.validators.pd_criteria import (
    CriterionResult,
    PdSourceSheet,
    PdThresholds,
    _marginal_pd_criterion,
    classify_sheets,
    combine_pd_sheets,
    evaluate_pd_validation,
)

COLUMNS = ["Loan ID", "Reporting Month", "Staging", "Loan Amount", "SEGMENT"]
MONTH_ENDS = ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30", "2024-05-31"]


def _sheet(rows: list[tuple], *, filename: str = "pd.xlsx", sheet: str = "Sheet1") -> PdSourceSheet:
    return PdSourceSheet(
        filename=filename, sheet_name=sheet, frame=pd.DataFrame(rows, columns=COLUMNS)
    )


def _healthy_rows() -> list[tuple]:
    """Five consecutive months of a portfolio that should clear every check."""
    rows: list[tuple] = []
    for month in MONTH_ENDS:
        rows.append(("L001", month, "Stage 1", 100_000.0, "Retail"))
        rows.append(("L002", month, "Stage 1", 100_000.0, "Retail"))
        rows.append(("L003", month, "Stage 2", 60_000.0, "Retail"))
    # L004 defaults, then cures back to Stage 2 and stays there.
    rows.append(("L004", MONTH_ENDS[0], "Stage 3", 40_000.0, "Retail"))
    rows.append(("L004", MONTH_ENDS[1], "Stage 3", 40_000.0, "Retail"))
    for month in MONTH_ENDS[2:]:
        rows.append(("L004", month, "Stage 2", 40_000.0, "Retail"))
    return rows


def _evaluate(
    sources: list[PdSourceSheet],
    *,
    allowed_segments: set[str] | None = None,
    thresholds: PdThresholds | None = None,
    file_count: int = 1,
    combine_pd_files: bool = True,
):  # type: ignore[no-untyped-def]
    return evaluate_pd_validation(
        sources,
        allowed_segments={"Retail", "SME"} if allowed_segments is None else allowed_segments,
        thresholds=thresholds or PdThresholds(),
        engine_version="v-test",
        engine_method="test method",
        file_count=file_count,
        combine_pd_files=combine_pd_files,
    )


def _by_code(criteria: list[CriterionResult]) -> dict[str, CriterionResult]:
    return {c.code: c for c in criteria}


def _outcome(outcome, code: str) -> str:  # type: ignore[no-untyped-def]
    return _by_code(outcome.criteria)[code].outcome


# ── Baseline ─────────────────────────────────────────────────────────────────


def test_healthy_portfolio_passes_every_criterion() -> None:
    outcome = _evaluate([_sheet(_healthy_rows())])
    codes = _by_code(outcome.criteria)

    assert outcome.status == "ok"
    assert outcome.issues == []
    for code, criterion in codes.items():
        expected = "info" if code == "EC-18" else "pass"
        assert criterion.outcome == expected, f"{code}: {criterion.rationale}"


def test_checklist_is_complete_and_stably_identified() -> None:
    outcome = _evaluate([_sheet(_healthy_rows())])
    codes = [c.code for c in outcome.criteria]

    assert codes == [
        "PD-01",
        "PD-02",
        "PD-03",
        "PD-04",
        "PD-05",
        "PD-06",
        "PD-07",
        "EC-10",
        "EC-11",
        "EC-12",
        "EC-13",
        "EC-14",
        "EC-15",
        "EC-16",
        "EC-17",
        "EC-19",
        "EC-18",
    ]
    assert all(c.id == c.code.lower() for c in outcome.criteria)
    # The UI groups on category, so statistical checks must read as business.
    assert {c.category for c in outcome.criteria if c.code.startswith("PD-")} == {"structural"}
    assert _by_code(outcome.criteria)["EC-15"].category == "business"
    assert _by_code(outcome.criteria)["EC-18"].category == "info"


def test_evaluation_is_deterministic() -> None:
    first = _evaluate([_sheet(_healthy_rows())])
    second = _evaluate([_sheet(_healthy_rows())])
    assert first.criteria == second.criteria
    assert first.preview.segments == second.preview.segments
    assert first.preview.loans == second.preview.loans


# ── Structural criteria ──────────────────────────────────────────────────────


def test_pd01_blocks_when_a_sheet_looks_like_pd_data_but_lacks_columns() -> None:
    partial = pd.DataFrame({"Loan ID": ["L1"], "Staging": ["Stage 1"]})
    outcome = _evaluate([PdSourceSheet(filename="pd.xlsx", sheet_name="Data", frame=partial)])

    assert outcome.status == "blocking"
    assert _outcome(outcome, "PD-01") == "block"
    assert "Loan Amount" in _by_code(outcome.criteria)["PD-01"].rationale
    # Dependent checks say they did not run rather than reporting a pass.
    assert _outcome(outcome, "EC-15") == "review"
    assert outcome.preview.loans == []
    assert outcome.preview.segments == []
    assert outcome.preview.engine_version == "v-test"


def test_pd01_blocks_when_no_sheet_holds_pd_data() -> None:
    cover = pd.DataFrame({"Notes": ["Prepared by Risk"]})
    outcome = _evaluate([PdSourceSheet(filename="pd.xlsx", sheet_name="Cover", frame=cover)])
    assert _outcome(outcome, "PD-01") == "block"
    assert "No sheet" in _by_code(outcome.criteria)["PD-01"].rationale


def test_non_data_sheets_are_skipped_not_blocked() -> None:
    cover = PdSourceSheet(
        filename="pd.xlsx", sheet_name="Cover", frame=pd.DataFrame({"Notes": ["Q4 pack"]})
    )
    data = _sheet(_healthy_rows(), sheet="Data")
    outcome = _evaluate([cover, data])

    assert outcome.status == "ok"
    assert outcome.skipped_sheet_count == 1
    assert outcome.data_sheet_count == 1
    assert "1 sheet(s) were skipped" in _by_code(outcome.criteria)["EC-18"].rationale


def test_pd02_blocks_on_blank_loan_id() -> None:
    rows = [*_healthy_rows(), (None, MONTH_ENDS[0], "Stage 1", 10.0, "Retail")]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "PD-02") == "block"
    assert "1 row has a blank Loan ID" in _by_code(outcome.criteria)["PD-02"].rationale
    assert any("Loan ID is empty" in issue.title for issue in outcome.issues)


def test_pd03_blocks_on_blank_segment() -> None:
    rows = [*_healthy_rows(), ("L900", MONTH_ENDS[0], "Stage 1", 10.0, "  ")]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "PD-03") == "block"


def test_pd04_blocks_on_unparseable_reporting_month() -> None:
    rows = [*_healthy_rows(), ("L900", "not-a-date", "Stage 1", 10.0, "Retail")]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "PD-04") == "block"


def test_pd05_blocks_on_unknown_staging_value() -> None:
    rows = [*_healthy_rows(), ("L900", MONTH_ENDS[0], "Stage 4", 10.0, "Retail")]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "PD-05") == "block"
    assert any("Invalid Staging" in issue.title for issue in outcome.issues)


def test_pd06_blocks_on_negative_loan_amount() -> None:
    rows = [*_healthy_rows(), ("L900", MONTH_ENDS[0], "Stage 1", -5.0, "Retail")]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "PD-06") == "block"


def test_pd07_blocks_unconfigured_segment_and_skips_when_none_configured() -> None:
    rows = [*_healthy_rows(), ("L900", MONTH_ENDS[0], "Stage 1", 10.0, "Offshore")]
    blocked = _evaluate([_sheet(rows)])
    assert _outcome(blocked, "PD-07") == "block"
    assert "Offshore" in _by_code(blocked.criteria)["PD-07"].rationale

    unconfigured = _evaluate([_sheet(_healthy_rows())], allowed_segments=set())
    assert _outcome(unconfigured, "PD-07") == "review"


def test_ec10_blocks_duplicates_across_sheets_and_files() -> None:
    duplicate = [("L001", MONTH_ENDS[0], "Stage 1", 100_000.0, "Retail")]
    outcome = _evaluate(
        [
            _sheet(_healthy_rows(), filename="pd_a.xlsx"),
            _sheet(duplicate, filename="pd_b.xlsx", sheet="Sheet1"),
        ],
        file_count=2,
    )
    assert _outcome(outcome, "EC-10") == "block"
    locations = " ".join(issue.location for issue in outcome.issues)
    assert "pd_a.xlsx" in locations
    assert "pd_b.xlsx" in locations


def test_ec10_treats_same_month_different_day_as_duplicate() -> None:
    """The transition join keys on the calendar month, so day-of-month is not a tiebreak.

    Two rows for one loan in one month would otherwise fan out into a cartesian
    product and inflate the money-weighted matrix.
    """
    rows = [*_healthy_rows(), ("L001", "2024-01-15", "Stage 2", 100_000.0, "Retail")]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-10") == "block"
    assert "calendar month is the transition join key" in _by_code(outcome.criteria)[
        "EC-10"
    ].rationale


def test_issue_list_is_capped_with_an_explicit_truncation_notice() -> None:
    rows = [
        (None, MONTH_ENDS[0], "Stage 1", 100.0, "Retail")
        for _ in range(120)
    ]
    outcome = _evaluate([_sheet(rows)])

    assert outcome.issues_truncated is True
    assert len(outcome.issues) == 101
    assert outcome.issues[-1].title == "Additional errors were found but not shown"
    # Capping what is reported must never soften what was detected.
    assert "120 rows have a blank Loan ID" in _by_code(outcome.criteria)["PD-02"].rationale


# ── Business and statistical criteria ────────────────────────────────────────


def test_ec11_flags_a_missing_reporting_month() -> None:
    rows = [row for row in _healthy_rows() if row[1] != MONTH_ENDS[2]]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-11") == "review"
    assert "2024-02 to 2024-04" in _by_code(outcome.criteria)["EC-11"].rationale
    assert outcome.status == "warn"


def test_ec12_flags_mixed_loan_id_formats() -> None:
    rows = [*_healthy_rows()]
    for month in MONTH_ENDS:
        rows.append(("legacy-77-a", month, "Stage 1", 10_000.0, "Retail"))
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-12") == "review"
    assert "legacy-77-a" in _by_code(outcome.criteria)["EC-12"].rationale


def test_ec13_flags_a_single_month_upload() -> None:
    rows = [row for row in _healthy_rows() if row[1] == MONTH_ENDS[0]]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-13") == "review"
    assert "Fewer than 3 monthly transitions" in _by_code(outcome.criteria)["EC-13"].rationale


def test_ec13_respects_a_tenant_override() -> None:
    rows = [row for row in _healthy_rows() if row[1] in MONTH_ENDS[:2]]
    assert _outcome(_evaluate([_sheet(rows)]), "EC-13") == "review"
    relaxed = _evaluate([_sheet(rows)], thresholds=PdThresholds(min_transitions=1))
    assert _outcome(relaxed, "EC-13") == "pass"


def test_ec14_flags_an_implausible_stage3_share() -> None:
    rows = [
        (loan, month, "Stage 3", 100_000.0, "Retail")
        for loan in ("L001", "L002")
        for month in MONTH_ENDS
    ]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-14") == "review"
    assert "100.0%" in _by_code(outcome.criteria)["EC-14"].rationale


def test_ec15_blocks_on_out_of_range_or_non_monotonic_probabilities() -> None:
    """The one blocking statistical check: arithmetic that cannot be right.

    Driven directly because a well-formed workbook cannot produce it — reaching
    this state means the projection itself broke.
    """
    valid = pd.DataFrame(
        {
            "SEGMENT": ["Retail"] * 3,
            "Transition": ["Stage 1"] * 3,
            "Month": [1, 2, 3],
            "Marginal_PD": [0.01, 0.02, 0.03],
            "Stage_3_prob": [0.01, 0.03, 0.06],
        }
    )
    assert _marginal_pd_criterion(valid).outcome == "pass"

    out_of_bounds = valid.copy()
    out_of_bounds.loc[1, "Marginal_PD"] = 1.4
    blocked = _marginal_pd_criterion(out_of_bounds)
    assert blocked.outcome == "block"
    assert "outside 0–100%" in blocked.rationale

    regressing = valid.copy()
    regressing.loc[2, "Stage_3_prob"] = 0.02
    assert _marginal_pd_criterion(regressing).outcome == "block"

    assert _marginal_pd_criterion(pd.DataFrame()).outcome == "review"


def test_ec15_tolerates_floating_point_noise() -> None:
    noisy = pd.DataFrame(
        {
            "SEGMENT": ["Retail"] * 2,
            "Transition": ["Stage 1"] * 2,
            "Month": [1, 2],
            "Marginal_PD": [-1e-17, 1.0 + 1e-17],
            "Stage_3_prob": [0.5, 0.5 - 1e-17],
        }
    )
    assert _marginal_pd_criterion(noisy).outcome == "pass"


def test_ec16_flags_a_zero_cure_rate() -> None:
    rows = [
        ("L001", month, "Stage 3", 100_000.0, "Retail") for month in MONTH_ENDS
    ] + [("L002", month, "Stage 1", 100_000.0, "Retail") for month in MONTH_ENDS]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-16") == "review"
    assert "0.0%" in _by_code(outcome.criteria)["EC-16"].rationale


def test_ec16_is_not_evaluated_without_stage3_exposure() -> None:
    rows = [("L001", month, "Stage 1", 100_000.0, "Retail") for month in MONTH_ENDS]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-16") == "review"
    assert "no segment has any observed Stage 3 exposure" in _by_code(outcome.criteria)[
        "EC-16"
    ].rationale


def test_ec17_flags_an_offbooks_spike() -> None:
    months = MONTH_ENDS[:4]
    rows: list[tuple] = []
    for index in range(10):
        loan = f"L{index:03d}"
        # The first five loans close after month three.
        active = months[:3] if index < 5 else months
        rows.extend((loan, month, "Stage 1", 100_000.0, "Retail") for month in active)
    outcome = _evaluate([_sheet(rows)])

    assert _outcome(outcome, "EC-17") == "review"
    assert "Mar 2024" in _by_code(outcome.criteria)["EC-17"].rationale


def test_ec17_ignores_the_final_month_mass_exit() -> None:
    """Every loan reads Offbooks in the last month because there is no t+1.

    Counting that as an exit would fail EC-17 on every well-formed upload.
    """
    outcome = _evaluate([_sheet(_healthy_rows())])
    assert _outcome(outcome, "EC-17") == "pass"
    # The loan table still shows those rows; only the aggregate excludes them.
    final = [row for row in outcome.preview.loans if row.reporting_month_key == "2024-05"]
    assert final
    assert all(row.next_staging == "offbooks" and row.is_final_month for row in final)


def test_ec17_needs_two_transition_months() -> None:
    rows = [row for row in _healthy_rows() if row[1] in MONTH_ENDS[:2]]
    outcome = _evaluate([_sheet(rows)])
    assert _outcome(outcome, "EC-17") == "review"
    assert "two or more transition months" in _by_code(outcome.criteria)["EC-17"].rationale


def test_ec19_flags_a_stage_with_no_observed_exposure() -> None:
    """A zero row projects a 0% lifetime PD that looks measured but never was."""
    rows = [
        ("L001", month, "Stage 1", 100_000.0, "Retail") for month in MONTH_ENDS
    ] + [("L002", month, "Stage 2", 50_000.0, "Retail") for month in MONTH_ENDS]
    outcome = _evaluate([_sheet(rows)])

    assert _outcome(outcome, "EC-19") == "review"
    assert "Retail · Stage 3" in _by_code(outcome.criteria)["EC-19"].rationale
    segment = outcome.preview.segments[0]
    assert np.allclose(np.array(segment.matrix[2]), 0.0)


def test_ec18_warns_when_compute_will_not_combine_the_files() -> None:
    combined = _evaluate([_sheet(_healthy_rows())], file_count=2, combine_pd_files=True)
    assert "computed result will not match" not in _by_code(combined.criteria)["EC-18"].rationale

    diverging = _evaluate([_sheet(_healthy_rows())], file_count=2, combine_pd_files=False)
    rationale = _by_code(diverging.criteria)["EC-18"].rationale
    assert "will use only the first file" in rationale
    assert diverging.status == "ok"


def test_ec18_reports_the_engine_stamp_and_row_count() -> None:
    outcome = _evaluate([_sheet(_healthy_rows())])
    rationale = _by_code(outcome.criteria)["EC-18"].rationale
    assert "v-test" in rationale
    assert f"{outcome.row_count:,} rows" in rationale
    assert "Nothing has been saved" in rationale


# ── Sheet handling ───────────────────────────────────────────────────────────


def test_classify_sheets_separates_data_malformed_and_cover_pages() -> None:
    data = _sheet(_healthy_rows(), sheet="Data")
    malformed = PdSourceSheet(
        filename="pd.xlsx",
        sheet_name="Partial",
        frame=pd.DataFrame({"Loan ID": ["L1"], "SEGMENT": ["Retail"]}),
    )
    cover = PdSourceSheet(
        filename="pd.xlsx", sheet_name="Cover", frame=pd.DataFrame({"Notes": ["x"]})
    )

    kept, broken, skipped = classify_sheets([data, malformed, cover])
    assert [s.sheet_name for s in kept] == ["Data"]
    assert [s.sheet_name for s in broken] == ["Partial"]
    assert [s.sheet_name for s in skipped] == ["Cover"]


def test_combine_normalizes_the_reporting_month_alias_and_keeps_provenance() -> None:
    frame = pd.DataFrame(_healthy_rows(), columns=COLUMNS).rename(
        columns={"Reporting Month": 'Reporting Month ("As At")'}
    )
    combined = combine_pd_sheets(
        [PdSourceSheet(filename="pd.xlsx", sheet_name="Data", frame=frame)]
    )
    assert "Reporting Month" in combined.columns
    assert combined["_pd_source_file"].unique().tolist() == ["pd.xlsx"]
    # Excel rows are 1-based with a header, so the first data row is row 2.
    assert int(combined["_pd_source_row"].iloc[0]) == 2


def test_multiple_files_are_combined_in_upload_order() -> None:
    early = [("L001", month, "Stage 1", 100_000.0, "Retail") for month in MONTH_ENDS[:3]]
    late = [("L001", month, "Stage 2", 100_000.0, "Retail") for month in MONTH_ENDS[3:]]
    outcome = _evaluate(
        [_sheet(early, filename="pd_jan.xlsx"), _sheet(late, filename="pd_apr.xlsx")],
        file_count=2,
    )

    assert _outcome(outcome, "EC-10") == "pass"
    assert outcome.row_count == 5
    assert outcome.preview.segments[0].stats.months_of_history == 5
