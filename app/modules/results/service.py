"""Results service — dashboard and results explorer aggregations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ECLException
from app.core.run_enums import OutputArtifactKind, RunStatus
from app.engine.format_utils import (
    format_amount,
    format_compact_amount,
    format_coverage,
    map_run_status_to_api,
    short_ulid,
)
from app.modules.auth.models import User
from app.modules.auth.utils import user_initials
from app.modules.results.models import EadResult, EclLoanSummary, LgdResult, PdResult
from app.modules.results.pd_observation import (
    load_pd_observation,
    persist_pd_observation_payload,
    rebuild_pd_observation,
    segment_from_preview,
)
from app.modules.results.schemas import (
    ActiveRunOut,
    DashboardOut,
    KpiDashboardOut,
    LoanRowOut,
    LoanViewOut,
    MonthlyRundownOut,
    PortfolioTotalsOut,
    PortfolioViewOut,
    RunContextOut,
    RunSummaryOut,
    SegmentBarDashboardOut,
    SegmentDataOut,
    SegmentRateStatsOut,
    SegmentViewOut,
    StageSliceOut,
    TrendPointOut,
)
from app.modules.runs.models import Run
from app.modules.runs.schemas import (
    EclReportOut,
    EclTotalsOut,
    ExceptionOut,
    ExceptionsPageOut,
    ReconciliationOut,
    SummaryRowOut,
)
from app.modules.runs.service import assert_run_visible, get_run_scope_filter
from app.modules.tenants.models import Tenant, TenantMembership

_STAGE_MAP = {
    "Stage 1": 1,
    "Stage 2": 2,
    "Stage 3": 3,
}

_STAGE_COLORS = {"Stage 1": 1, "Stage 2": 5, "Stage 3": 7}


def _stage_num(stage: str) -> Literal[1, 2, 3]:
    return _STAGE_MAP.get(stage, 1)  # type: ignore[return-value]


def _format_dt(dt: datetime | None, tz_name: str = "UTC") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%d %b %Y")


def _delta_dir(delta: float | None) -> Literal["up", "down", "flat"] | None:
    if delta is None:
        return None
    if delta > 0.05:
        return "up"
    if delta < -0.05:
        return "down"
    return "flat"


def _pct_delta(current: float, previous: float | None) -> float | None:
    if previous is None or previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 1)


async def _get_tenant(db: AsyncSession, tenant_id: str) -> Tenant:
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
    )
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise ECLException("RESOURCE_NOT_FOUND", "Workspace not found.", 404)
    return tenant


async def _latest_completed_run(
    db: AsyncSession,
    tenant_id: str,
    run_id: str | None = None,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> Run:
    if run_id:
        if user is not None:
            run = await assert_run_visible(db, user, tenant_id, membership, run_id)
        else:
            result = await db.execute(
                select(Run).where(
                    Run.id == run_id,
                    Run.tenant_id == tenant_id,
                    Run.deleted_at.is_(None),
                )
            )
            run = result.scalar_one_or_none()
        if not run:
            raise ECLException("RESOURCE_NOT_FOUND", "Run not found.", 404)
        if run.status != RunStatus.COMPLETE.value:
            raise ECLException(
                "RUN_NOT_COMPLETE",
                f"This run has not finished computing yet (current status: {run.status}).",
                409,
            )
        return run

    if user is not None:
        scope = get_run_scope_filter(user, tenant_id, membership)
        where_clause = (scope, Run.status == RunStatus.COMPLETE.value, Run.deleted_at.is_(None))
    else:
        where_clause = (
            Run.tenant_id == tenant_id,
            Run.status == RunStatus.COMPLETE.value,
            Run.deleted_at.is_(None),
        )

    result = await db.execute(
        select(Run)
        .where(*where_clause)
        .order_by(Run.finished_at.desc().nullslast(), Run.created_at.desc())
        .limit(1)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise ECLException("NO_COMPLETED_RUNS", "No completed runs found.", 404)
    return run


async def _previous_completed_run(
    db: AsyncSession,
    tenant_id: str,
    before: datetime | None,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> Run | None:
    if before is None:
        return None
    if user is not None:
        scope = get_run_scope_filter(user, tenant_id, membership)
        where_clause = (
            scope,
            Run.status == RunStatus.COMPLETE.value,
            Run.deleted_at.is_(None),
            Run.finished_at < before,
        )
    else:
        where_clause = (
            Run.tenant_id == tenant_id,
            Run.status == RunStatus.COMPLETE.value,
            Run.deleted_at.is_(None),
            Run.finished_at < before,
        )
    result = await db.execute(
        select(Run)
        .where(*where_clause)
        .order_by(Run.finished_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _loan_count(db: AsyncSession, run_id: str, tenant_id: str) -> int:
    result = await db.execute(
        select(func.count()).where(
            EclLoanSummary.run_id == run_id,
            EclLoanSummary.tenant_id == tenant_id,
        )
    )
    return int(result.scalar_one() or 0)


def _summary_filter(
    run_id: str, tenant_id: str, *, stage: str | None = None, min_ecl: float | None = None
) -> list:
    # Every ECL figure reads the capped loan-level Total ECL (spec Step 11),
    # never a sum of ead_results rows, which are uncapped.
    base = [EclLoanSummary.run_id == run_id, EclLoanSummary.tenant_id == tenant_id]
    if stage:
        base.append(EclLoanSummary.stage == stage)
    if min_ecl is not None:
        base.append(EclLoanSummary.total_ecl >= min_ecl)
    return base


async def _segment_aggregates(
    db: AsyncSession,
    run_id: str,
    tenant_id: str,
    *,
    prior_run_id: str | None = None,
    stage: str | None = None,
    min_ecl: float | None = None,
) -> list[SegmentDataOut]:
    base_filter = _summary_filter(run_id, tenant_id, stage=stage, min_ecl=min_ecl)
    segments_result = await db.execute(
        select(
            EclLoanSummary.segment,
            func.sum(EclLoanSummary.total_ecl).label("ecl"),
            func.sum(EclLoanSummary.outstanding).label("outstanding"),
            func.count().label("loans"),
        )
        .where(*base_filter)
        .group_by(EclLoanSummary.segment)
        .order_by(func.sum(EclLoanSummary.total_ecl).desc())
    )
    stage_result = await db.execute(
        select(EclLoanSummary.segment, EclLoanSummary.stage, func.count())
        .where(*base_filter)
        .group_by(EclLoanSummary.segment, EclLoanSummary.stage)
    )
    stage_counts: dict[str, dict[str, int]] = {}
    for seg, stg, cnt in stage_result.all():
        stage_counts.setdefault(seg, {})[stg] = int(cnt)

    prior_ecl: dict[str, float] = {}
    if prior_run_id:
        prior_result = await db.execute(
            select(EclLoanSummary.segment, func.sum(EclLoanSummary.total_ecl))
            .where(*_summary_filter(prior_run_id, tenant_id))
            .group_by(EclLoanSummary.segment)
        )
        prior_ecl = {row[0]: float(row[1] or 0) for row in prior_result.all()}

    segments: list[SegmentDataOut] = []
    for row in segments_result.all():
        ecl = float(row.ecl or 0)
        outstanding = float(row.outstanding or 0)
        coverage = format_coverage(ecl / outstanding) if outstanding else "0.00%"
        counts = stage_counts.get(row.segment, {})
        total = sum(counts.values()) or 1
        mix = (
            round(counts.get("Stage 1", 0) / total * 100, 1),
            round(counts.get("Stage 2", 0) / total * 100, 1),
            round(counts.get("Stage 3", 0) / total * 100, 1),
        )
        segments.append(
            SegmentDataOut(
                name=row.segment,
                mix=mix,
                ecl=ecl,
                outstanding=outstanding,
                coverage=coverage,
                loans=int(row.loans or 0),
                delta=_pct_delta(ecl, prior_ecl.get(row.segment)) or 0.0,
            )
        )
    return segments


def _pd_12m_subquery(run_id: str, tenant_id: str):
    # Months 1-12 after the Reporting Date; the reporting-date row carries no PD.
    return (
        select(
            EadResult.loan_id,
            func.coalesce(func.sum(EadResult.marginal_pd), 0).label("pd_12m"),
            func.avg(EadResult.lgd).label("lgd"),
        )
        .where(
            EadResult.run_id == run_id,
            EadResult.tenant_id == tenant_id,
            EadResult.period_to_discount <= 12,
        )
        .group_by(EadResult.loan_id)
        .subquery()
    )


async def _loan_rows_for_segment(
    db: AsyncSession,
    run_id: str,
    tenant_id: str,
    segment_name: str,
    *,
    page: int = 1,
    per_page: int = 50,
    stage: str | None = None,
    min_ecl: float | None = None,
) -> tuple[list[LoanRowOut], int]:
    seg_filter = [
        *_summary_filter(run_id, tenant_id, stage=stage, min_ecl=min_ecl),
        EclLoanSummary.segment == segment_name,
    ]
    total = (await db.execute(select(func.count()).where(*seg_filter))).scalar_one()

    rates = _pd_12m_subquery(run_id, tenant_id)
    result = await db.execute(
        select(
            EclLoanSummary.loan_id,
            EclLoanSummary.customer_id,
            EclLoanSummary.stage,
            EclLoanSummary.total_ecl,
            EclLoanSummary.outstanding,
            rates.c.pd_12m,
            rates.c.lgd,
        )
        .outerjoin(rates, EclLoanSummary.loan_id == rates.c.loan_id)
        .where(*seg_filter)
        .order_by(EclLoanSummary.total_ecl.desc(), EclLoanSummary.loan_id)
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    loans = [
        LoanRowOut(
            id=row.loan_id,
            customer=row.customer_id,
            stage=_stage_num(row.stage),
            pd=round(float(row.pd_12m or 0) * 100, 2),
            lgd=round(float(row.lgd or 0) * 100, 2),
            ead=round(float(row.outstanding or 0)),
            ecl=round(float(row.total_ecl or 0)),
        )
        for row in result.all()
    ]
    return loans, total


async def _segment_rate_avgs(
    db: AsyncSession, run_id: str, tenant_id: str, segment_name: str
) -> tuple[float | None, float | None]:
    lgd_result = await db.execute(
        select(func.avg(EadResult.lgd)).where(
            EadResult.run_id == run_id,
            EadResult.tenant_id == tenant_id,
            EadResult.segment == segment_name,
        )
    )
    pd_result = await db.execute(
        select(func.avg(EadResult.marginal_pd)).where(
            EadResult.run_id == run_id,
            EadResult.tenant_id == tenant_id,
            EadResult.segment == segment_name,
            EadResult.period_to_discount <= 12,
        )
    )
    avg_lgd = lgd_result.scalar_one_or_none()
    avg_pd = pd_result.scalar_one_or_none()
    return (
        float(avg_lgd) if avg_lgd is not None else None,
        float(avg_pd) if avg_pd is not None else None,
    )


async def _pd_matrix(
    db: AsyncSession, run_id: str, tenant_id: str, segment_name: str
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    result = await db.execute(
        select(PdResult)
        .where(
            PdResult.run_id == run_id,
            PdResult.tenant_id == tenant_id,
            PdResult.segment == segment_name,
            PdResult.month == 1,
        )
        .order_by(PdResult.transition)
    )
    rows = result.scalars().all()
    matrix: list[tuple[float, float, float]] = []
    for pd_row in rows:
        matrix.append(
            (
                round(float(pd_row.s1_prob), 3),
                round(float(pd_row.s2_prob), 3),
                round(float(pd_row.s3_prob), 3),
            )
        )
    while len(matrix) < 3:
        matrix.append((0.0, 0.0, 0.0))
    return (matrix[0], matrix[1], matrix[2])


async def get_dashboard(
    db: AsyncSession,
    tenant_id: str,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> DashboardOut:
    tenant = await _get_tenant(db, tenant_id)

    try:
        latest = await _latest_completed_run(db, tenant_id, user=user, membership=membership)
    except ECLException:
        return DashboardOut(kpis=[], segments=[], stages=[], trend=[], runs=[])

    previous = await _previous_completed_run(
        db, tenant_id, latest.finished_at, user=user, membership=membership
    )
    loan_count = await _loan_count(db, latest.id, tenant_id)
    prev_loans = (
        await _loan_count(db, previous.id, tenant_id) if previous else None
    )

    total_ecl = float(latest.total_ecl or 0)
    prev_ecl = float(previous.total_ecl) if previous and previous.total_ecl else None
    outstanding = float(latest.total_outstanding or 0)
    prev_outstanding = (
        float(previous.total_outstanding)
        if previous and previous.total_outstanding
        else None
    )
    coverage = float(latest.coverage_ratio or 0)
    prev_coverage = (
        float(previous.coverage_ratio) if previous and previous.coverage_ratio else None
    )

    kpis = [
        KpiDashboardOut(
            id="total-ecl",
            label="Total ECL",
            currencyPrefix=tenant.currency,
            value=format_amount(total_ecl),
            delta=_pct_delta(total_ecl, prev_ecl),
            deltaDir=_delta_dir(_pct_delta(total_ecl, prev_ecl)),
            subNote="vs previous run",
        ),
        KpiDashboardOut(
            id="coverage-ratio",
            label="Coverage ratio",
            helpText="ECL ÷ total outstanding",
            value=format_coverage(coverage),
            delta=_pct_delta(coverage * 100, prev_coverage * 100 if prev_coverage else None),
            deltaDir=_delta_dir(
                _pct_delta(coverage * 100, prev_coverage * 100 if prev_coverage else None)
            ),
            subNote="vs previous run",
        ),
        KpiDashboardOut(
            id="total-outstanding",
            label="Total outstanding",
            currencyPrefix=tenant.currency,
            value=format_compact_amount(outstanding),
            deltaDir="flat",
            subNote=f"{loan_count:,} loans",
        ),
        KpiDashboardOut(
            id="loans-analysed",
            label="Loans analysed",
            value=f"{loan_count:,}",
            delta=_pct_delta(float(loan_count), float(prev_loans) if prev_loans else None),
            deltaDir=_delta_dir(
                _pct_delta(float(loan_count), float(prev_loans) if prev_loans else None)
            ),
            subNote="vs previous run",
        ),
    ]

    segment_data = await _segment_aggregates(db, latest.id, tenant_id)
    segments = [
        SegmentBarDashboardOut(name=s.name, value=s.ecl) for s in segment_data[:6]
    ]

    stage_result = await db.execute(
        select(EclLoanSummary.stage, func.count())
        .where(*_summary_filter(latest.id, tenant_id))
        .group_by(EclLoanSummary.stage)
    )
    stage_counts = {stage: int(cnt) for stage, cnt in stage_result.all()}
    total_stages = sum(stage_counts.values()) or 1
    stages = [
        StageSliceOut(
            stage=stage,  # type: ignore[arg-type]
            percent=round(stage_counts.get(stage, 0) / total_stages * 100, 1),
            colorVar=_STAGE_COLORS[stage],
        )
        for stage in ("Stage 1", "Stage 2", "Stage 3")
    ]

    trend_result = await db.execute(
        select(Run.reporting_period, Run.total_ecl, Run.finished_at)
        .where(
            Run.tenant_id == tenant_id,
            Run.status == RunStatus.COMPLETE.value,
            Run.deleted_at.is_(None),
            Run.total_ecl.is_not(None),
        )
        .order_by(Run.finished_at.desc())
        .limit(12)
    )
    trend_rows = list(reversed(trend_result.all()))
    trend = [
        TrendPointOut(
            label=row.reporting_period or _format_dt(row.finished_at, tenant.timezone)[:6],
            value=float(row.total_ecl or 0),
        )
        for row in trend_rows
    ]

    runs_result = await db.execute(
        select(Run, User)
        .join(User, User.id == Run.created_by_user_id)
        .where(Run.tenant_id == tenant_id, Run.deleted_at.is_(None))
        .order_by(Run.created_at.desc())
        .limit(10)
    )
    runs = [
        RunSummaryOut(
            id=short_ulid(run.id),
            fullId=run.id,
            period=run.reporting_period or run.name,
            byInitials=user_initials(user.name),
            byName=user.name,
            status=map_run_status_to_api(run.status),
            eclAmount=float(run.total_ecl) if run.total_ecl is not None else None,
            currency=tenant.currency,
        )
        for run, user in runs_result.all()
    ]

    active_run: ActiveRunOut | None = None
    active_result = await db.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.deleted_at.is_(None),
            Run.status.in_(
                [
                    RunStatus.QUEUED.value,
                    RunStatus.PD_RUNNING.value,
                    RunStatus.LGD_RUNNING.value,
                    RunStatus.EAD_RUNNING.value,
                ]
            ),
        )
        .order_by(Run.updated_at.desc())
        .limit(1)
    )
    active = active_result.scalar_one_or_none()
    if active:
        progress_pct = 0.0
        stage_label: str | None = None
        if active.engine_progress:
            stages_done = sum(
                1
                for key in ("pd", "lgd", "ead", "ecl")
                if active.engine_progress.get(key, {}).get("status") == "complete"
            )
            progress_pct = stages_done * 25.0
            for key in ("pd", "lgd", "ead", "ecl"):
                if active.engine_progress.get(key, {}).get("status") == "running":
                    stage_label = key.upper()
                    break
        active_run = ActiveRunOut(
            runId=short_ulid(active.id),
            name=active.name,
            status=map_run_status_to_api(active.status),
            progress=progress_pct,
            stage=stage_label,
        )

    return DashboardOut(
        kpis=kpis,
        segments=segments,
        stages=stages,
        trend=trend,
        runs=runs,
        activeRun=active_run,
    )


_VALID_STAGES = frozenset({"Stage 1", "Stage 2", "Stage 3"})


def _validate_stage(stage: str | None) -> str | None:
    if stage is None:
        return None
    if stage not in _VALID_STAGES:
        from app.core.exceptions import ECLException
        raise ECLException(
            "INVALID_STAGE",
            f"stage must be one of: {', '.join(sorted(_VALID_STAGES))}.",
            400,
        )
    return stage


async def get_portfolio(
    db: AsyncSession,
    tenant_id: str,
    run_id: str | None = None,
    user: User | None = None,
    membership: TenantMembership | None = None,
    stage: str | None = None,
    min_ecl: float | None = None,
) -> PortfolioViewOut:
    stage = _validate_stage(stage)
    tenant = await _get_tenant(db, tenant_id)
    run = await _latest_completed_run(db, tenant_id, run_id, user=user, membership=membership)
    previous = await _previous_completed_run(
        db, tenant_id, run.finished_at, user=user, membership=membership
    )
    segments = await _segment_aggregates(
        db,
        run.id,
        tenant_id,
        prior_run_id=previous.id if previous else None,
        stage=stage,
        min_ecl=min_ecl,
    )
    totals = PortfolioTotalsOut(
        ecl=sum(s.ecl for s in segments),
        outstanding=sum(s.outstanding for s in segments),
        loans=sum(s.loans for s in segments),
        coverage=format_coverage(
            sum(s.ecl for s in segments) / sum(s.outstanding for s in segments)
            if sum(s.outstanding for s in segments)
            else 0
        ),
    )
    return PortfolioViewOut(
        runContext=RunContextOut(
            runId=short_ulid(run.id),
            period=run.reporting_period or run.name,
            computedAt=_format_dt(run.finished_at, tenant.timezone),
            engineVersion=run.engine_version,
            currency=tenant.currency,
        ),
        segments=segments,
        totals=totals,
    )


async def get_segment(
    db: AsyncSession,
    tenant_id: str,
    segment_name: str,
    *,
    run_id: str | None = None,
    stage: str | None = None,
    min_ecl: float | None = None,
    page: int = 1,
    per_page: int = 50,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> SegmentViewOut:
    stage = _validate_stage(stage)
    tenant = await _get_tenant(db, tenant_id)
    run = await _latest_completed_run(db, tenant_id, run_id, user=user, membership=membership)
    previous = await _previous_completed_run(
        db, tenant_id, run.finished_at, user=user, membership=membership
    )
    segments = await _segment_aggregates(
        db,
        run.id,
        tenant_id,
        prior_run_id=previous.id if previous else None,
        stage=stage,
        min_ecl=min_ecl,
    )
    segment = next((s for s in segments if s.name == segment_name), None)
    if not segment:
        raise ECLException("RESOURCE_NOT_FOUND", f"Segment '{segment_name}' not found.", 404)

    loans, total = await _loan_rows_for_segment(
        db,
        run.id,
        tenant_id,
        segment_name,
        page=page,
        per_page=per_page,
        stage=stage,
        min_ecl=min_ecl,
    )
    pd_matrix = await _pd_matrix(db, run.id, tenant_id, segment_name)
    pages = max(1, (total + per_page - 1) // per_page)

    preview = await load_pd_observation(db, run.id)
    if preview is None:
        preview = await rebuild_pd_observation(
            db,
            tenant_id=tenant_id,
            run_id=run.id,
            combine_pd_files=bool(run.combine_pd_files),
        )
        if preview is not None:
            try:
                await persist_pd_observation_payload(
                    db,
                    run_id=run.id,
                    tenant_id=tenant_id,
                    engine_version=preview.engine_version,
                    payload=preview.model_dump(),
                )
            except ProgrammingError:
                await db.rollback()
    observation, pd_loans = (
        segment_from_preview(preview, segment_name) if preview is not None else (None, [])
    )
    avg_lgd, avg_pd = await _segment_rate_avgs(db, run.id, tenant_id, segment_name)
    cure_rate = observation.stats.cure_rate if observation is not None else 0.0

    return SegmentViewOut(
        segment=segment,
        pdMatrix=pd_matrix,
        pdObservation=observation,
        pdLoans=pd_loans,
        rateStats=SegmentRateStatsOut(
            cureRate=cure_rate,
            avgLgd=avg_lgd,
            avgPd12m=avg_pd,
        ),
        loans=loans,
        meta={
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "has_next": page < pages,
            "has_prev": page > 1,
        },
    )


async def get_loan(
    db: AsyncSession,
    tenant_id: str,
    loan_id: str,
    *,
    run_id: str | None = None,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> LoanViewOut:
    tenant = await _get_tenant(db, tenant_id)
    run = await _latest_completed_run(db, tenant_id, run_id, user=user, membership=membership)

    summary = (
        await db.execute(
            select(EclLoanSummary).where(
                *_summary_filter(run.id, tenant_id), EclLoanSummary.loan_id == loan_id
            )
        )
    ).scalar_one_or_none()
    if summary is None:
        raise ECLException("RESOURCE_NOT_FOUND", f"Loan '{loan_id}' not found.", 404)
    rates = _pd_12m_subquery(run.id, tenant_id)
    rates_row = (await db.execute(select(rates).where(rates.c.loan_id == loan_id))).one_or_none()
    pd_12m = float(rates_row.pd_12m or 0) if rates_row else 0.0
    avg_lgd = float(rates_row.lgd or 0) if rates_row else 0.0

    lgd_result = await db.execute(
        select(func.coalesce(func.sum(LgdResult.sum_discounted_collat), 0)).where(
            LgdResult.run_id == run.id,
            LgdResult.tenant_id == tenant_id,
            LgdResult.loan_id == loan_id,
        )
    )
    collateral = float(lgd_result.scalar_one() or 0)

    rundown_result = await db.execute(
        select(EadResult)
        .where(
            EadResult.run_id == run.id,
            EadResult.tenant_id == tenant_id,
            EadResult.loan_id == loan_id,
        )
        .order_by(EadResult.period_to_discount.asc())
    )
    rows = rundown_result.scalars().all()
    cumulative = 0.0
    rundown: list[MonthlyRundownOut] = []
    max_period = 0
    last_snapshot: datetime | None = None
    for row in rows:
        marginal = float(row.marginal_pd or 0)
        cumulative += marginal
        rundown.append(
            MonthlyRundownOut(
                month=row.period_to_discount,
                marginalPd=round(marginal * 100, 2),
                cumulativePd=round(cumulative * 100, 2),
                ead=round(float(row.bal_after_missed)),
                ecl=round(float(row.discounted_ecl)),
            )
        )
        max_period = max(max_period, row.period_to_discount)
        last_snapshot = row.snapshot_date  # type: ignore[assignment]

    maturity = f"{max_period} mo"
    if last_snapshot:
        maturity = f"{last_snapshot.strftime('%b %Y')} · {max_period} mo"

    ead_val = round(float(summary.outstanding))
    return LoanViewOut(
        id=loan_id,
        customer=summary.customer_id,
        stage=_stage_num(summary.stage),
        pd=round(pd_12m * 100, 2),
        lgd=round(avg_lgd * 100, 2),
        ead=ead_val,
        ecl=round(float(summary.total_ecl)),
        segment=summary.segment,
        outstanding=ead_val,
        collateralValue=round(collateral),
        maturity=maturity,
        rundown=rundown,
    )


ARTIFACT_FILENAMES: dict[str, str] = {
    OutputArtifactKind.PD_CALCS.value: "PD Calcs.xlsx",
    OutputArtifactKind.LOAN_DETAIL.value: "ECL Loan Detail.xlsx",
    OutputArtifactKind.PORTFOLIO_SUMMARY.value: "ECL Portfolio Summary.xlsx",
    # Runs before engine v1.3.0.
    OutputArtifactKind.LGD.value: "LGD.xlsx",
    OutputArtifactKind.RUNDOWN.value: "Contractual Rundown.xlsx",
    OutputArtifactKind.ECL_SUMMARY.value: "ECL Summary.xlsx",
}


async def _get_run_or_404(db: AsyncSession, tenant_id: str, run_id: str):
    from app.modules.runs.models import Run

    run = (
        await db.execute(
            select(Run).where(
                Run.id == run_id,
                Run.tenant_id == tenant_id,
                Run.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise ECLException("RESOURCE_NOT_FOUND", "Run not found.", 404)
    return run


async def _get_output_artifact(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    kind: str,
):
    from app.modules.results.models import OutputArtifact

    await _get_run_or_404(db, tenant_id, run_id)

    result = await db.execute(
        select(OutputArtifact).where(
            OutputArtifact.run_id == run_id,
            OutputArtifact.tenant_id == tenant_id,
            OutputArtifact.kind == kind,
        )
    )
    artifact = result.scalar_one_or_none()
    if not artifact:
        raise ECLException("ARTIFACT_NOT_FOUND", f"No output artifact of kind {kind}.", 404)
    return artifact


async def get_presigned_download(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    kind: str,
) -> tuple[str, datetime]:
    from datetime import timedelta

    from app.core.storage import presign_download

    artifact = await _get_output_artifact(db, tenant_id, run_id, kind)
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    url = await presign_download(artifact.storage_path, expires_seconds=900)
    return url, expires_at


async def get_artifact_bytes(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    kind: str,
) -> tuple[bytes, str]:
    from app.core.storage import download_bytes

    kind_upper = kind.upper()
    if kind_upper not in ARTIFACT_FILENAMES:
        raise ECLException("INVALID_ARTIFACT_KIND", f"Unknown artifact kind {kind}.", 400)

    artifact = await _get_output_artifact(db, tenant_id, run_id, kind_upper)
    content = await download_bytes(artifact.storage_path)
    return content, ARTIFACT_FILENAMES[kind_upper]


async def build_workbooks_bundle(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
) -> tuple[bytes, str]:
    import io
    import zipfile

    from app.core.storage import download_bytes
    from app.modules.results.models import OutputArtifact

    await _get_run_or_404(db, tenant_id, run_id)
    artifacts = (
        await db.execute(
            select(OutputArtifact)
            .where(OutputArtifact.run_id == run_id, OutputArtifact.tenant_id == tenant_id)
            .order_by(OutputArtifact.created_at)
        )
    ).scalars().all()
    if not artifacts:
        raise ECLException("ARTIFACT_NOT_FOUND", "This run has no output workbooks.", 404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for artifact in artifacts:
            name = ARTIFACT_FILENAMES.get(artifact.kind, f"{artifact.kind}.xlsx")
            archive.writestr(name, await download_bytes(artifact.storage_path))

    return buffer.getvalue(), f"ECL_{run_id}_PD_LGD_EAD_workbooks.zip"


async def get_ecl_report(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    *,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> EclReportOut:
    """Spec Step 11 portfolio view + 12.1 reconciliation for one completed run."""
    from app.engine.ecl_pipeline import SPEC_VERSION
    from app.engine.ecl_rules import STAGES
    from app.modules.results.models import RunException

    run = await _latest_completed_run(db, tenant_id, run_id, user=user, membership=membership)
    base = _summary_filter(run.id, tenant_id)
    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(EclLoanSummary.outstanding), 0),
                func.coalesce(func.sum(EclLoanSummary.total_ecl), 0),
                func.coalesce(func.sum(EclLoanSummary.ecl_before_cap), 0),
                func.count(),
                func.count().filter(EclLoanSummary.ecl_capped.is_(True)),
            ).where(*base)
        )
    ).one()
    outstanding, ecl, before_cap, loans, capped = (
        float(totals[0]), float(totals[1]), float(totals[2]), int(totals[3]), int(totals[4])
    )

    async def _group(column) -> dict[str, tuple[float, float]]:
        rows = await db.execute(
            select(column, func.sum(EclLoanSummary.outstanding), func.sum(EclLoanSummary.total_ecl))
            .where(*base)
            .group_by(column)
        )
        return {key: (float(o or 0), float(e or 0)) for key, o, e in rows.all()}

    def _rows(groups: dict[str, tuple[float, float]], keys: list[str]) -> list[SummaryRowOut]:
        out = [
            SummaryRowOut(key=k, outstanding=o, ecl=e, coverage=e / o if o else 0.0)
            for k in keys
            for o, e in [groups.get(k, (0.0, 0.0))]
        ]
        out.append(
            SummaryRowOut(
                key="Total", outstanding=outstanding, ecl=ecl,
                coverage=ecl / outstanding if outstanding else 0.0,
            )
        )
        return out

    stage_groups = await _group(EclLoanSummary.stage)
    segment_groups = await _group(EclLoanSummary.segment)
    counts = await db.execute(
        select(RunException.category, func.count())
        .where(RunException.run_id == run.id, RunException.tenant_id == tenant_id)
        .group_by(RunException.category)
    )
    recon = [ReconciliationOut(**c) for c in (run.reconciliation or [])]
    loan_count = next((c for c in recon if c.name == "Loan count"), None)
    return EclReportOut(
        runId=run.id,
        reportingDate=run.reporting_date.isoformat() if run.reporting_date else None,
        specVersion=SPEC_VERSION,
        engineVersion=run.engine_version,
        totals=EclTotalsOut(
            outstanding=outstanding,
            ecl=ecl,
            eclBeforeCap=before_cap,
            coverage=ecl / outstanding if outstanding else 0.0,
            loans=loans,
            excludedRows=int(loan_count.expected) - loans if loan_count else 0,
            cappedLoans=capped,
        ),
        stageSummary=_rows(stage_groups, list(STAGES)),
        segmentSummary=_rows(segment_groups, sorted(segment_groups)),
        reconciliation=recon,
        exceptionCounts={cat: int(n) for cat, n in counts.all()},
    )


async def list_run_exceptions(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    *,
    category: str | None = None,
    page: int = 1,
    per_page: int = 50,
    user: User | None = None,
    membership: TenantMembership | None = None,
) -> ExceptionsPageOut:
    from app.modules.results.models import RunException

    run = await _latest_completed_run(db, tenant_id, run_id, user=user, membership=membership)
    where = [RunException.run_id == run.id, RunException.tenant_id == tenant_id]
    if category:
        where.append(RunException.category == category)
    total = (await db.execute(select(func.count()).where(*where))).scalar_one()
    rows = (
        await db.execute(
            select(RunException)
            .where(*where)
            .order_by(RunException.seq)
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
    ).scalars().all()
    return ExceptionsPageOut(
        items=[
            ExceptionOut(
                category=r.category, dataset=r.dataset, loanId=r.loan_id, customerId=r.customer_id,
                field=r.field, value=r.value, issue=r.issue, action=r.action,
            )
            for r in rows
        ],
        total=int(total),
        page=page,
        perPage=per_page,
    )
