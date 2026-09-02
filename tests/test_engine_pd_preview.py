"""PD preview unit tests.

The preview is what an analyst approves before committing a run, so it has to
agree with the engine exactly and has to be reproducible run over run.
"""

import numpy as np
import pandas as pd

from app.engine.pd_engine import compute_pd
from app.engine.pd_preview import (
    DEST_LABELS,
    build_pd_preview,
    month_key,
    month_label,
    prepare_preview_frame,
    stage_to_wire,
)

MONTHS = ["2024-10-31", "2024-11-30", "2024-12-31"]


def _portfolio() -> pd.DataFrame:
    rows = [
        # Stays performing for the whole window.
        ("L1", "2024-10-31", "Stage 1", 100_000.0, "Retail"),
        ("L1", "2024-11-30", "Stage 1", 100_000.0, "Retail"),
        ("L1", "2024-12-31", "Stage 1", 100_000.0, "Retail"),
        # Deteriorates, then defaults.
        ("L2", "2024-10-31", "Stage 1", 200_000.0, "Retail"),
        ("L2", "2024-11-30", "Stage 2", 200_000.0, "Retail"),
        ("L2", "2024-12-31", "Stage 3", 200_000.0, "Retail"),
        # Leaves the book after October.
        ("L3", "2024-10-31", "Stage 2", 50_000.0, "Retail"),
        # Cures out of Stage 3.
        ("L4", "2024-10-31", "Stage 3", 75_000.0, "SME"),
        ("L4", "2024-11-30", "Stage 1", 75_000.0, "SME"),
        ("L4", "2024-12-31", "Stage 1", 75_000.0, "SME"),
    ]
    df = pd.DataFrame(
        rows, columns=["Loan ID", "Reporting Month", "Staging", "Loan Amount", "SEGMENT"]
    )
    df["Reporting Month"] = pd.to_datetime(df["Reporting Month"])
    return df


def _preview():  # type: ignore[no-untyped-def]
    return build_pd_preview(prepare_preview_frame(_portfolio()), engine_version="v-test")


def test_month_formatting_is_locale_independent() -> None:
    assert month_label(pd.Timestamp("2024-10-31")) == "Oct 2024"
    assert month_key(pd.Timestamp("2024-10-31")) == "2024-10"
    assert month_key(pd.Timestamp("2024-01-05")) == "2024-01"


def test_stage_wire_form() -> None:
    assert stage_to_wire("Stage 1") == 1
    assert stage_to_wire("Stage 3") == 3
    assert stage_to_wire("Offbooks") == "offbooks"
    # Unknown labels surface as themselves rather than defaulting to performing.
    assert stage_to_wire("Stage 9") == "Stage 9"


def test_loan_rows_cover_every_loan_month_including_the_last() -> None:
    preview = _preview()
    assert len(preview.loans) == 10

    l3 = [row for row in preview.loans if row.loan_id == "L3"]
    assert len(l3) == 1
    # L3 exits mid-window, so its Offbooks is a real exit and not a final-month artefact.
    assert l3[0].next_staging == "offbooks"
    assert l3[0].is_final_month is False

    december = [row for row in preview.loans if row.reporting_month_key == "2024-12"]
    assert december
    assert all(row.is_final_month for row in december)
    assert all(row.next_staging == "offbooks" for row in december)


def test_next_staging_matches_the_following_month() -> None:
    preview = _preview()
    by_key = {(row.loan_id, row.reporting_month_key): row for row in preview.loans}
    assert by_key[("L2", "2024-10")].next_staging == 2
    assert by_key[("L2", "2024-11")].next_staging == 3
    assert by_key[("L4", "2024-10")].next_staging == 1


def test_preview_matrix_is_3x4_and_row_normalized() -> None:
    preview = _preview()
    retail = next(s for s in preview.segments if s.segment == "Retail")

    assert retail.dest_labels == list(DEST_LABELS) == [
        "Stage 1",
        "Stage 2",
        "Stage 3",
        "Offbooks",
    ]
    assert len(retail.matrix) == 3
    assert all(len(row) == 4 for row in retail.matrix)

    for row in retail.matrix:
        total = sum(row)
        assert total == 0.0 or abs(total - 1.0) < 1e-12


def test_preview_matrix_excludes_the_terminal_month() -> None:
    """December is every segment's last month, so its all-Offbooks rows are noise.

    Including it would report a near-total exodus that never happened.
    """
    preview = _preview()
    retail = next(s for s in preview.segments if s.segment == "Retail")

    # Oct + Nov only: Stage 1 exposure is L1 (100k) + L2 (200k) in Oct, L1 (100k) in Nov.
    # L1 stays Stage 1 twice (200k), L2 moves to Stage 2 (200k). Nothing exits.
    assert retail.matrix[0][0] == 0.5
    assert retail.matrix[0][1] == 0.5
    assert retail.matrix[0][3] == 0.0

    # Stage 2 in the window: L3 (50k, exits in Oct) and L2 (200k, defaults in Nov).
    assert retail.matrix[1][2] == 0.8
    assert retail.matrix[1][3] == 0.2


def test_stage3_row_keeps_its_cure_before_absorbing() -> None:
    preview = _preview()
    sme = next(s for s in preview.segments if s.segment == "SME")
    # L4's single non-terminal Stage 3 observation cures to Stage 1.
    assert sme.matrix[2][0] == 1.0
    assert sme.stats.cure_rate == 1.0


def test_cure_rate_matches_compute_pd() -> None:
    """The preview must not show a cure rate different from the one feeding ECL."""
    preview = _preview()
    _output, cure_rates, _intermediate = compute_pd(_portfolio())
    for segment in preview.segments:
        assert segment.stats.cure_rate == cure_rates[segment.segment]


def test_loans_observed_counts_distinct_ids_not_rows() -> None:
    preview = _preview()
    retail = next(s for s in preview.segments if s.segment == "Retail")
    assert retail.stats.loans_observed == 3  # L1, L2, L3 across 7 rows
    assert retail.stats.months_of_history == 3


def test_amounts_are_preformatted_for_display() -> None:
    preview = _preview()
    retail = next(s for s in preview.segments if s.segment == "Retail")
    assert retail.amounts[0][0] == "200,000"
    assert retail.amounts[0][3] == "0.00"
    assert all(isinstance(cell, str) for row in retail.amounts for cell in row)


def test_preview_is_deterministic_under_row_shuffling() -> None:
    ordered = _portfolio()
    shuffled = ordered.sample(frac=1.0, random_state=7)

    first = build_pd_preview(prepare_preview_frame(ordered), engine_version="v-test")
    second = build_pd_preview(prepare_preview_frame(shuffled), engine_version="v-test")

    assert first.loans == second.loans
    assert first.segments == second.segments


def test_empty_frame_yields_an_empty_preview() -> None:
    empty = _portfolio().iloc[0:0]
    preview = build_pd_preview(prepare_preview_frame(empty), engine_version="v-test")
    assert preview.loans == []
    assert preview.segments == []
    assert preview.engine_version == "v-test"


def test_single_month_segment_has_no_transitions() -> None:
    df = _portfolio()
    df = df[df["Reporting Month"] == pd.Timestamp("2024-10-31")]
    preview = build_pd_preview(prepare_preview_frame(df), engine_version="v-test")

    for segment in preview.segments:
        assert np.allclose(np.array(segment.matrix), 0.0)
        assert segment.stats.months_of_history == 1
