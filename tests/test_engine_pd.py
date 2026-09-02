"""PD engine unit tests.

These pin the arithmetic that `compute_pd` is trusted for. The validate-time
preview reuses these same helpers, so a regression here would move both the
preview and the computed ECL at once.
"""

import numpy as np
import pandas as pd

from app.engine.pd_engine import (
    MAX_PD_MONTH,
    build_monthly_matrix,
    compute_pd,
    cure_rate_from_proportions,
    derive_next_month_staging,
    find_month_collisions,
    is_pd_data_sheet,
    normalize_matrix,
    prepare_pd_frame,
    row_proportions,
)


def _frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["Reporting Month"] = pd.to_datetime(df["Reporting Month"])
    return df


def test_compute_pd_basic() -> None:
    df = pd.DataFrame(
        {
            "Loan ID": ["L1", "L1", "L2", "L2"],
            "Reporting Month": pd.to_datetime(
                ["2024-01-31", "2024-02-29", "2024-01-31", "2024-02-29"]
            ),
            "Staging": ["Stage 1", "Stage 1", "Stage 2", "Stage 3"],
            "Loan Amount": [100_000.0, 95_000.0, 50_000.0, 48_000.0],
            "SEGMENT": ["Retail", "Retail", "Retail", "Retail"],
        }
    )
    output, cure_rates, _intermediate = compute_pd(df)
    assert not output.empty
    assert "Marginal_PD" in output.columns
    assert "Retail" in cure_rates
    assert output["Month"].max() == MAX_PD_MONTH == 300


def test_month_end_join_survives_short_months() -> None:
    """Jan 31 -> Feb 29 must join. A DateOffset(months=1) shift silently would not.

    Day-preserving month arithmetic lands Feb 29 back on Jan 29, not Jan 31,
    which used to mark every loan active in a short month as Offbooks and
    discard roughly five months of transitions per year.
    """
    df = _frame(
        [
            {
                "Loan ID": "L1",
                "Reporting Month": month,
                "Staging": "Stage 1",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            }
            for month in ["2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30"]
        ]
    )
    joined = derive_next_month_staging(prepare_pd_frame(df))
    by_month = dict(
        zip(joined["Reporting Month"], joined["Next Month Staging"], strict=True)
    )
    assert by_month[pd.Timestamp("2024-01-31")] == "Stage 1"
    assert by_month[pd.Timestamp("2024-02-29")] == "Stage 1"
    assert by_month[pd.Timestamp("2024-03-31")] == "Stage 1"
    # Only the final month has no successor.
    assert by_month[pd.Timestamp("2024-04-30")] == "Offbooks"


def test_stage3_is_absorbing_across_the_horizon() -> None:
    df = _frame(
        [
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-01-31",
                "Staging": "Stage 1",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-02-29",
                "Staging": "Stage 3",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
        ]
    )
    output, _cure, _inter = compute_pd(df)
    from_stage3 = output[output["Transition"] == "Stage 3"]
    assert np.allclose(from_stage3["Stage_3_prob"].to_numpy(), 1.0)
    assert len(from_stage3) == MAX_PD_MONTH


def test_cure_rate_uses_pre_absorbing_proportions() -> None:
    """Cure rate must be measured before Stage 3 is forced absorbing.

    Normalizing Stage 3 to [0, 0, 1] first would make every cure rate zero.
    """
    aggregate = np.array(
        [[80.0, 20.0, 0.0], [10.0, 70.0, 20.0], [15.0, 5.0, 80.0]], dtype=np.float64
    )
    proportions = row_proportions(aggregate)
    assert cure_rate_from_proportions(proportions) == 0.20

    normalized = normalize_matrix(proportions)
    assert list(normalized[2]) == [0.0, 0.0, 1.0]
    # The Stage 1 row is left as raw empirical proportions, not renormalized.
    assert np.allclose(normalized[0], proportions[0])


def test_zero_exposure_row_stays_zero() -> None:
    aggregate = np.array([[0.0, 0.0, 0.0], [10.0, 10.0, 0.0], [0.0, 0.0, 5.0]])
    proportions = row_proportions(aggregate)
    assert np.allclose(proportions[0], 0.0)
    assert np.allclose(proportions[1], [0.5, 0.5, 0.0])


def test_build_monthly_matrix_returns_full_3x4() -> None:
    df = _frame(
        [
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-01-31",
                "Staging": "Stage 1",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
            {
                "Loan ID": "L2",
                "Reporting Month": "2024-01-31",
                "Staging": "Stage 1",
                "Loan Amount": 400.0,
                "SEGMENT": "Retail",
            },
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-02-29",
                "Staging": "Stage 2",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
        ]
    )
    joined = derive_next_month_staging(prepare_pd_frame(df))
    january = joined[joined["Reporting Month"] == pd.Timestamp("2024-01-31")]
    matrix = build_monthly_matrix(january)
    assert matrix.shape == (3, 4)
    # Money-weighted, not loan-count weighted: L2 (400) exits, L1 (100) moves to Stage 2.
    assert matrix[0, 1] == 100.0
    assert matrix[0, 3] == 400.0


def test_month_collision_does_not_expand_rows() -> None:
    """Two rows for one loan in one month must not multiply the transition rows.

    The next-month join keys on the calendar month, so an unguarded merge would
    produce a cartesian product and inflate the money-weighted matrix.
    """
    df = _frame(
        [
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-01-15",
                "Staging": "Stage 1",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-01-31",
                "Staging": "Stage 2",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
            {
                "Loan ID": "L1",
                "Reporting Month": "2024-02-29",
                "Staging": "Stage 3",
                "Loan Amount": 100.0,
                "SEGMENT": "Retail",
            },
        ]
    )
    prepared = prepare_pd_frame(df)
    assert len(find_month_collisions(prepared)) == 2

    joined = derive_next_month_staging(prepared)
    assert len(joined) == len(prepared)


def test_is_pd_data_sheet_skips_cover_pages() -> None:
    cover = pd.DataFrame({"Notes": ["Prepared by Risk"], "Author": ["A. Analyst"]})
    assert not is_pd_data_sheet(cover)

    data = pd.DataFrame(
        {
            "Loan ID": ["L1"],
            "Staging": ["Stage 1"],
            "Loan Amount": [1.0],
            "SEGMENT": ["Retail"],
            "Reporting Month": ["2024-01-31"],
        }
    )
    assert is_pd_data_sheet(data)
    # The reporting month alias counts as the same column.
    assert is_pd_data_sheet(data.rename(columns={"Reporting Month": 'Reporting Month ("As At")'}))


def test_compute_pd_is_deterministic_regardless_of_input_order() -> None:
    rows = [
        {
            "Loan ID": f"L{i}",
            "Reporting Month": month,
            "Staging": ["Stage 1", "Stage 2", "Stage 3"][(i + j) % 3],
            "Loan Amount": 1000.0 * (i + 1),
            "SEGMENT": "Retail" if i % 2 else "SME",
        }
        for i in range(6)
        for j, month in enumerate(["2024-01-31", "2024-02-29", "2024-03-31"])
    ]
    ordered = _frame(rows)
    shuffled = ordered.sample(frac=1.0, random_state=11)

    first, cure_a, _ = compute_pd(ordered)
    second, cure_b, _ = compute_pd(shuffled)
    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert cure_a == cure_b
