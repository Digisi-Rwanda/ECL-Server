"""Bulk insert helpers for compute result tables."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.security import new_ulid

BATCH_SIZE = 50_000
_INSERT_SHAPE = re.compile(r"INSERT INTO (\w+) \(([^)]*)\)", re.S)

_INSERT_PD = """
INSERT INTO pd_results (
    id, run_id, tenant_id, segment, month, transition,
    s1_prob, s2_prob, s3_prob, marginal_pd, cure_rate
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
"""

_INSERT_LGD = """
INSERT INTO lgd_results (
    id, run_id, tenant_id, loan_id, customer_id, eir, sum_discounted_collat
) VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_INSERT_EAD = """
INSERT INTO ead_results (
    id, run_id, tenant_id, loan_id, customer_id, segment, stage,
    snapshot_date, period_since_orig, period_to_discount,
    monthly_instalment, bal_after_repayment, bal_after_missed,
    marginal_pd, cure_rate, treatment, pd_rule, lgw, lgd, credit_loss, discounted_ecl
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10,
    $11, $12, $13,
    $14, $15, $16, $17, $18, $19, $20, $21
)
"""

_INSERT_LOAN_SUMMARY = """
INSERT INTO ecl_loan_summaries (
    id, run_id, tenant_id, loan_id, customer_id, segment, stage, reporting_date,
    outstanding, discounted_collateral, ecl_before_cap, ecl_capped, total_ecl
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""

_INSERT_EXCEPTION = """
INSERT INTO run_exceptions (
    id, run_id, tenant_id, seq, category, dataset, loan_id, customer_id, field, value, issue, action
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
"""

_LGD_SUM_COL = "Discounted Collateral"


def _chunks(records: list[tuple[Any, ...]], size: int) -> list[list[tuple[Any, ...]]]:
    return [records[i : i + size] for i in range(0, len(records), size)]


async def _executemany(conn: AsyncConnection, query: str, records: list[tuple[Any, ...]]) -> int:
    if not records:
        return 0

    raw = await conn.get_raw_connection()
    pg_conn = raw.driver_connection
    # COPY is Postgres' bulk-load path: one round trip per batch instead of one
    # statement per row, roughly 10x faster for hundreds of thousands of rows.
    match = _INSERT_SHAPE.search(query)
    table, columns = match.group(1), [c.strip() for c in match.group(2).split(",")]
    inserted = 0
    for batch in _chunks(records, BATCH_SIZE):
        await pg_conn.copy_records_to_table(table, records=batch, columns=columns)
        inserted += len(batch)
    return inserted


def _to_decimal(value: Any) -> Decimal:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return Decimal("0")
    return Decimal(str(value))


def _to_date(value: Any) -> date:
    return pd.Timestamp(value).date()


def _pd_records(run_id: str, tenant_id: str, df: pd.DataFrame) -> list[tuple[Any, ...]]:
    return [
        (
            new_ulid(),
            run_id,
            tenant_id,
            str(row["SEGMENT"]),
            int(row["Month"]),
            str(row["Transition"]),
            float(row["Stage_1_prob"]),
            float(row["Stage_2_prob"]),
            float(row["Stage_3_prob"]),
            float(row["Marginal_PD"]),
            float(row["Cure_Rate"]),
        )
        for row in df.to_dict(orient="records")
    ]


def _lgd_records(run_id: str, tenant_id: str, df: pd.DataFrame) -> list[tuple[Any, ...]]:
    return [
        (
            new_ulid(),
            run_id,
            tenant_id,
            str(row["Loan ID"]),
            str(row["Customer ID"]),
            _to_decimal(row["Collateral EIR"]),
            _to_decimal(row[_LGD_SUM_COL]),
        )
        for row in df.to_dict(orient="records")
    ]


def _num(df: pd.DataFrame, col: str) -> list[float]:
    """Required numeric column as Python floats; a missing value is stored as 0."""
    return pd.to_numeric(df[col], errors="coerce").fillna(0.0).astype(float).tolist()


def _opt(df: pd.DataFrame, col: str) -> list[float | None]:
    values = pd.to_numeric(df[col], errors="coerce").astype(object)
    return values.where(values.notna(), None).tolist()


def _text(df: pd.DataFrame, col: str) -> list[str]:
    return df[col].astype(str).tolist()


def _dates(df: pd.DataFrame, col: str) -> list[date]:
    return pd.to_datetime(df[col]).dt.date.tolist()


# Built column by column (no per-cell Decimal) and loaded with COPY, which
# stores floats into NUMERIC columns at the column's scale.
def _ead_records(run_id: str, tenant_id: str, df: pd.DataFrame) -> list[tuple[Any, ...]]:
    n = len(df)
    return list(zip(
        [new_ulid() for _ in range(n)], [run_id] * n, [tenant_id] * n,
        _text(df, "Loan ID"), _text(df, "Customer ID"), _text(df, "SEGMENT"), _text(df, "Stage"),
        _dates(df, "Snapshot Date"),
        df["Period Since Origination"].astype(int).tolist(), df["Period to Be Discounted"].astype(int).tolist(),
        _num(df, "Monthly Instalment"), _num(df, "Balance After Repayment"), _num(df, "Balance After Missed Payment"),
        _opt(df, "Marginal PD"), _opt(df, "Cure Rate"), _text(df, "Treatment"), _text(df, "PD Rule"),
        _num(df, "LGW"), _num(df, "LGD"), _num(df, "Credit Loss"), _num(df, "Discounted ECL"),
        strict=True,
    ))


def _loan_summary_records(run_id: str, tenant_id: str, df: pd.DataFrame) -> list[tuple[Any, ...]]:
    n = len(df)
    return list(zip(
        [new_ulid() for _ in range(n)], [run_id] * n, [tenant_id] * n,
        _text(df, "Loan ID"), _text(df, "Customer ID"), _text(df, "SEGMENT"), _text(df, "Stage"),
        _dates(df, "Reporting Date"),
        _num(df, "Outstanding Amount"), _num(df, "Discounted Collateral"), _num(df, "ECL Before Cap"),
        (df["ECL Capped"] == "Yes").tolist(), _num(df, "Total ECL"),
        strict=True,
    ))


async def bulk_insert_pd_results(
    conn: AsyncConnection,
    run_id: str,
    tenant_id: str,
    df: pd.DataFrame,
) -> int:
    """Insert PD result rows in batches of 5,000."""
    return await _executemany(conn, _INSERT_PD, _pd_records(run_id, tenant_id, df))


async def bulk_insert_lgd_results(
    conn: AsyncConnection,
    run_id: str,
    tenant_id: str,
    df: pd.DataFrame,
) -> int:
    """Insert LGD result rows in batches of 5,000."""
    return await _executemany(conn, _INSERT_LGD, _lgd_records(run_id, tenant_id, df))


async def bulk_insert_ead_results(
    conn: AsyncConnection,
    run_id: str,
    tenant_id: str,
    df: pd.DataFrame,
) -> int:
    """Insert EAD result rows in batches of 5,000."""
    return await _executemany(conn, _INSERT_EAD, _ead_records(run_id, tenant_id, df))


async def bulk_insert_loan_summaries(
    conn: AsyncConnection, run_id: str, tenant_id: str, df: pd.DataFrame
) -> int:
    return await _executemany(conn, _INSERT_LOAN_SUMMARY, _loan_summary_records(run_id, tenant_id, df))


async def bulk_insert_exceptions(
    conn: AsyncConnection, run_id: str, tenant_id: str, exceptions: list[Any]
) -> int:
    records = [
        (new_ulid(), run_id, tenant_id, seq, e.category, e.dataset, e.loan_id or "", e.customer_id or "",
         e.field, e.value or "", e.issue, e.action)
        for seq, e in enumerate(exceptions)
    ]
    return await _executemany(conn, _INSERT_EXCEPTION, records)
