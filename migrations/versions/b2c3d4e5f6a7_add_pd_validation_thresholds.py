"""add pd_validation_thresholds

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-02

Per-tenant bounds for the PD validation checklist (?scope=pd). Defaults match
app.engine.validators.pd_criteria.PdThresholds, so a tenant with no row behaves
identically to one auto-provisioned with defaults.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pd_validation_thresholds",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("tenant_id", sa.String(26), nullable=False),
        sa.Column("max_month_gap", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("min_transitions", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column(
            "stage3_share_min", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "stage3_share_max", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0.4")
        ),
        sa.Column(
            "cure_rate_min", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0.02")
        ),
        sa.Column(
            "cure_rate_max", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0.9")
        ),
        sa.Column(
            "offbooks_abs_band", sa.Numeric(6, 4), nullable=False, server_default=sa.text("0.1")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pd_validation_thresholds_tenant_id", "pd_validation_thresholds", ["tenant_id"]
    )
    # Partial unique index: one live configuration per tenant, while still
    # allowing a soft-deleted row to sit alongside its replacement.
    op.create_index(
        "pd_validation_thresholds_tenant_uniq",
        "pd_validation_thresholds",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(
        "CREATE TRIGGER pd_validation_thresholds_updated_at BEFORE UPDATE ON "
        "pd_validation_thresholds FOR EACH ROW EXECUTE FUNCTION set_updated_at();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS pd_validation_thresholds_updated_at ON pd_validation_thresholds"
    )
    op.drop_index(
        "pd_validation_thresholds_tenant_uniq", table_name="pd_validation_thresholds"
    )
    op.drop_index(
        "ix_pd_validation_thresholds_tenant_id", table_name="pd_validation_thresholds"
    )
    op.drop_table("pd_validation_thresholds")
