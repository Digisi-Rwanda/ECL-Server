"""ECL spec v1.0: run reporting date, reconciliation, loan summaries, exceptions

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("reporting_date", sa.Date(), nullable=True))
    op.add_column("runs", sa.Column("reconciliation", sa.JSON(), nullable=True))
    op.add_column("ead_results", sa.Column("cure_rate", sa.Numeric(10, 8), nullable=True))
    op.add_column("ead_results", sa.Column("treatment", sa.Text(), nullable=True))
    op.add_column("ead_results", sa.Column("pd_rule", sa.Text(), nullable=True))

    op.create_table(
        "ecl_loan_summaries",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("tenant_id", sa.String(26), nullable=False),
        sa.Column("loan_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("segment", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("reporting_date", sa.Date(), nullable=False),
        sa.Column("outstanding", sa.Numeric(20, 6), nullable=False),
        sa.Column("discounted_collateral", sa.Numeric(20, 6), nullable=False),
        sa.Column("ecl_before_cap", sa.Numeric(20, 6), nullable=False),
        sa.Column("ecl_capped", sa.Boolean(), nullable=False),
        sa.Column("total_ecl", sa.Numeric(20, 6), nullable=False),
    )
    op.create_index("ix_ecl_loan_summaries_run_id", "ecl_loan_summaries", ["run_id"])
    op.create_index("ix_ecl_loan_summaries_tenant_id", "ecl_loan_summaries", ["tenant_id"])
    op.create_index("ecl_loan_summaries_run_segment_idx", "ecl_loan_summaries", ["run_id", "segment"])

    op.create_table(
        "run_exceptions",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("tenant_id", sa.String(26), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("loan_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("customer_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("issue", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
    )
    op.create_index("ix_run_exceptions_run_id", "run_exceptions", ["run_id"])
    op.create_index("ix_run_exceptions_tenant_id", "run_exceptions", ["tenant_id"])

    op.execute("UPDATE engine_versions SET is_current = false")
    op.execute(
        """
        INSERT INTO engine_versions (id, version, is_current, release_date, changelog, created_by_user_id, created_at, updated_at)
        VALUES (
            '01J00000000000000000000013',
            'v1.3.0',
            true,
            DATE '2026-10-29',
            '["ECL Calculation Specification v1.0: input rules exclude or adjust loans and report them", "Stage 3 Marginal PD = 100%; reporting-date row carries no PD; last PD month used beyond the curve", "Balance after repayment starts at Outstanding; up to 3 missed payments compound", "Matured and zero-rate loans are single-row; Adjusted Maturity derived as month end", "Collateral split by exposure share; lowest customer value; time to realize in years", "Total ECL capped at Outstanding; Exception Report, Run Details and 7 reconciliation checks"]'::jsonb,
            'PLATFORM_SYSTEM',
            NOW(),
            NOW()
        )
        ON CONFLICT (version) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM engine_versions WHERE version = 'v1.3.0'")
    op.drop_index("ix_run_exceptions_tenant_id", table_name="run_exceptions")
    op.drop_index("ix_run_exceptions_run_id", table_name="run_exceptions")
    op.drop_table("run_exceptions")
    op.drop_index("ecl_loan_summaries_run_segment_idx", table_name="ecl_loan_summaries")
    op.drop_index("ix_ecl_loan_summaries_tenant_id", table_name="ecl_loan_summaries")
    op.drop_index("ix_ecl_loan_summaries_run_id", table_name="ecl_loan_summaries")
    op.drop_table("ecl_loan_summaries")
    op.drop_column("ead_results", "pd_rule")
    op.drop_column("ead_results", "treatment")
    op.drop_column("ead_results", "cure_rate")
    op.drop_column("runs", "reconciliation")
    op.drop_column("runs", "reporting_date")
