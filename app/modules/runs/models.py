from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("runs_tenant_status_idx", "tenant_id", "status"),
        Index("runs_tenant_created_idx", "tenant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(26), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    reporting_period: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Spec 1.5: exposure rows with another Reporting Date are excluded. Null until
    # set by the user or defaulted from the EAD file's most common date.
    reporting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Spec 12.1 results: [{name, expected, actual, difference, passed, detail}].
    reconciliation: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    engine_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="v1.0.0")
    engine_progress: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    combine_pd_files: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    failure_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_ecl: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    total_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    coverage_ratio: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    run_warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by_user_id: Mapped[str | None] = mapped_column(String(26), nullable=True)


class PdValidationThresholds(Base):
    """Per-tenant bounds for the PD validation checklist.

    A 40% Stage 3 share is alarming for a mortgage book and unremarkable for a
    distressed-asset portfolio, so these cannot be global constants without
    producing warnings that reviewers learn to ignore. One row per tenant,
    auto-provisioned with platform defaults on first use.
    """

    __tablename__ = "pd_validation_thresholds"
    __table_args__ = (
        Index(
            "pd_validation_thresholds_tenant_uniq",
            "tenant_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    max_month_gap: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    min_transitions: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    stage3_share_min: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default="0"
    )
    stage3_share_max: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default="0.4"
    )
    cure_rate_min: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default="0.02"
    )
    cure_rate_max: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default="0.9"
    )
    offbooks_abs_band: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, server_default="0.1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sheet_count: Mapped[int | None] = mapped_column(nullable=True)
    row_count: Mapped[int | None] = mapped_column(nullable=True)
    validation_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
