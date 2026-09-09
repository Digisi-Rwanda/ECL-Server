"""add pd_observation_snapshots

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-09-03

Stores the observed PD preview (loan-month rows + 3x4 matrices) written at
compute time so results and validation share one extract.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pd_observation_snapshots",
        sa.Column("id", sa.String(26), primary_key=True),
        sa.Column("run_id", sa.String(26), nullable=False),
        sa.Column("tenant_id", sa.String(26), nullable=False),
        sa.Column("engine_version", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_pd_observation_snapshots_run_id",
        "pd_observation_snapshots",
        ["run_id"],
        unique=True,
    )
    op.create_index(
        "ix_pd_observation_snapshots_tenant_id",
        "pd_observation_snapshots",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pd_observation_snapshots_tenant_id", table_name="pd_observation_snapshots")
    op.drop_index("ix_pd_observation_snapshots_run_id", table_name="pd_observation_snapshots")
    op.drop_table("pd_observation_snapshots")
