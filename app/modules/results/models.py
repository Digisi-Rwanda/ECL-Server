from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, Index, Integer, JSON, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PdResult(Base):
    __tablename__ = "pd_results"
    __table_args__ = (
        Index("pd_results_run_segment_idx", "run_id", "segment"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    segment: Mapped[str] = mapped_column(Text, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    transition: Mapped[str] = mapped_column(Text, nullable=False)
    s1_prob: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    s2_prob: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    s3_prob: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    marginal_pd: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    cure_rate: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)


class PdObservationSnapshot(Base):
    """Observed PD preview persisted at compute — same payload validation shows.

    Validation is preview-only. Confirm + compute writes this snapshot so the
    results explorer reads the identical loan rows and 3x4 matrix instead of
    reconstructing a different 3x3 from pd_results month 1.
    """

    __tablename__ = "pd_observation_snapshots"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False, unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    engine_version: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LgdResult(Base):
    __tablename__ = "lgd_results"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    loan_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    eir: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    sum_discounted_collat: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


class EadResult(Base):
    __tablename__ = "ead_results"
    __table_args__ = (
        Index("ead_results_run_loan_idx", "run_id", "loan_id"),
        Index("ead_results_run_segment_idx", "run_id", "segment"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    loan_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    segment: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_since_orig: Mapped[int] = mapped_column(Integer, nullable=False)
    period_to_discount: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_instalment: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    bal_after_repayment: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    bal_after_missed: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    marginal_pd: Mapped[Decimal | None] = mapped_column(Numeric(10, 8), nullable=True)
    lgw: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    lgd: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    credit_loss: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    discounted_ecl: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)


class OutputArtifact(Base):
    __tablename__ = "output_artifacts"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(26), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
