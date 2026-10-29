#!/usr/bin/env python3
"""Write a realistic large PD / LGD / EAD book for load testing.

    python scripts/generate_big_book.py --loans 20000 --months 48 --out /tmp/book

Shapes follow the spec templates: PD history (loans x months), one Exposure row
and one Collateral row per loan, customer-level collateral repeated per loan.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

SEGMENTS = ("Retail", "SME", "Corporate", "Mortgage")
COLLATERAL = ("Real Estate", "Motor Vehicle", "Cash Deposit", "Corporate Guarantee")


def build(loans: int, months: int, seed: int = 7) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    ids = np.array([f"LN-{i:07d}" for i in range(loans)])
    customers = np.array([f"CU-{i // 2:07d}" for i in range(loans)])  # two loans per customer
    segment = rng.choice(SEGMENTS, loans)
    amount = rng.integers(5_000, 2_000_000, loans).astype(float)
    report_months = pd.date_range(end="2025-12-31", periods=months, freq="ME")

    # PD history: a stage walk per loan.
    steps = rng.choice([-1, 0, 0, 0, 0, 0, 0, 1], size=(loans, months))
    stage = np.clip(1 + np.cumsum(steps, axis=1), 1, 3)
    pd_frame = pd.DataFrame({
        "Loan ID": np.repeat(ids, months),
        "SEGMENT": np.repeat(segment, months),
        "Reporting Month": np.tile(report_months, loans),
        "Staging": [f"Stage {s}" for s in stage.ravel()],
        "Loan Amount": np.repeat(amount, months),
    })

    last_stage = stage[:, -1]
    rep = pd.Timestamp("2025-12-31")
    first_pay = rep - pd.to_timedelta(rng.integers(30, 2_500, loans), unit="D")
    maturity = rep + pd.to_timedelta(rng.integers(-60, 7_000, loans), unit="D")
    eir = rng.uniform(0.05, 0.25, loans).round(4)
    ead = pd.DataFrame({
        "Loan ID": ids, "Customer ID": customers, "SEGMENT": segment, "Reporting Date": rep,
        "First Payment Date": first_pay.normalize(), "Maturity Date": maturity.normalize(),
        "Stage": [f"Stage {s}" for s in last_stage], "Outstanding Amount": amount,
        "Effective Interest Rate (EIR)": eir,
    })
    customer_collateral = {c: rng.integers(0, 3_000_000, loans // 2 + 1).astype(float) for c in COLLATERAL}
    cust_idx = np.arange(loans) // 2
    lgd = pd.DataFrame({
        "Customer ID": customers, "Loan ID": ids, "Outstanding Amount": amount,
        "Effective Interest Rate (EIR)": eir,
        **{c: customer_collateral[c][cust_idx] for c in COLLATERAL},
    })
    return pd_frame, lgd, ead


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loans", type=int, default=20_000)
    ap.add_argument("--months", type=int, default=48)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    pd_frame, lgd, ead = build(args.loans, args.months)
    for name, frame in (("PD", pd_frame), ("LGD", lgd), ("EAD", ead)):
        path = args.out / f"{name}_{args.loans}.xlsx"
        frame.to_excel(path, index=False, engine="xlsxwriter")
        print(f"{name}: {len(frame):,} rows -> {path}")


if __name__ == "__main__":
    main()
