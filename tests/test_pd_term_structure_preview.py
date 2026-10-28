"""Validate-time PD term structure must equal what compute_pd feeds into ECL."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.engine.pd_engine import (
    MAX_PD_MONTH,
    STAGES,
    compute_pd,
    marginal_pd_from_cumulative,
    normalize_matrix,
    project_term_structure,
    row_proportions,
)
from app.engine.pd_preview import build_pd_preview, prepare_preview_frame


def _frame() -> pd.DataFrame:
    rows = []
    stages = {
        "L1": ["Stage 1", "Stage 1", "Stage 2", "Stage 2"],
        "L2": ["Stage 2", "Stage 3", "Stage 3", "Stage 2"],
        "L3": ["Stage 1", "Stage 2", "Stage 1", "Stage 1"],
        "L4": ["Stage 3", "Stage 2", "Stage 3", "Stage 3"],
    }
    for loan, path in stages.items():
        for m, stage in enumerate(path):
            rows.append(
                {
                    "Loan ID": loan,
                    "Reporting Month": pd.Timestamp("2025-01-31") + pd.offsets.MonthEnd(m),
                    "Staging": stage,
                    "Loan Amount": 1000.0 * (m + 1),
                    "SEGMENT": "ST",
                }
            )
    return pd.DataFrame(rows)


def test_preview_term_structure_matches_compute_pd_exactly() -> None:
    df = _frame()
    pd_df, cure, inter = compute_pd(df)
    preview = build_pd_preview(prepare_preview_frame(df), engine_version="test")
    seg = preview.segments[0]

    for i, stage in enumerate(STAGES):
        assert seg.normalized[i] == inter["normalized"]["ST"][stage]
        assert seg.proportions[i] == inter["proportion"]["ST"][stage]
        group = pd_df[(pd_df["SEGMENT"] == "ST") & (pd_df["Transition"] == stage)]
        assert len(seg.term_structure[i]) == MAX_PD_MONTH == len(group)
        expected = group[["Stage_1_prob", "Stage_2_prob", "Stage_3_prob", "Marginal_PD"]]
        assert seg.term_structure[i] == expected.to_numpy().tolist()
    assert seg.stats.cure_rate == cure["ST"]


def test_spec_segment_st_golden_numbers() -> None:
    # Table 1 of "Lifetime PD Term Structure" spec.
    balances = np.array(
        [
            [51_947_744_289, 1_294_413_908, 0],
            [15_546_083_098, 246_732_943_694, 779_919_373],
            [0, 119_881_683, 3_610_362_855],
        ],
        dtype=np.float64,
    )
    p = row_proportions(balances)
    np.testing.assert_allclose(p.sum(axis=1), 1.0)
    assert round(p[2, 0] + p[2, 1], 6) == 0.032138
    n = normalize_matrix(p)
    assert n[2].tolist() == [0.0, 0.0, 1.0] and n[0, 2] == 0.0
    term = project_term_structure(n)
    assert term.shape == (3, MAX_PD_MONTH, 3)
    s3 = term[0, :, 2]
    assert s3[0] == 0.0  # Stage 1 cannot default in one month after normalization
    assert round(s3[1], 9) == round(7.207989600858068e-05, 9)
    m = marginal_pd_from_cumulative(s3)
    assert m[0] == s3[0]
    assert np.isclose(m[2], (s3[2] - s3[1]) / (1 - s3[1]))
    # Stage 3 absorbing: marginal PD is 1 then 0 (denominator zero rule).
    assert marginal_pd_from_cumulative(term[2, :, 2])[:3].tolist() == [1.0, 0.0, 0.0]
