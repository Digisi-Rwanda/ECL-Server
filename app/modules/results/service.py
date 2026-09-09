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
from app.modules.results.models import EadResult, LgdResult, PdResult
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
        select(func.count(func.distinct(EadResult.loan_id))).where(
            EadResult.run_id == run_id,
            EadResult.tenant_id == tenant_id,
        )
    )
    return int(result.scalar_one() or 0)


def _apply_min_ecl_filter(
    base_filter: list,
    min_ecl: float | None,
) -> list:
    if min_ecl is None:
        return base_filter
    eligible = (
        select(EadResult.loan_id)
        .where(*base_filter)
        .group_by(EadResult.loan_id)
        .having(func.sum(EadResult.discounted_ecl) >= min_ecl)
    )
    return [*base_filter, EadResult.loan_id.in_(eligible)]


async def _segment_aggregates(
    db: AsyncSession,
    run_id: str,
    tenant_id: str,
    *,
    prior_run_id: str | None = None,
    stage: str | None = None,
    min_ecl: float | None = None,
) -> list[SegmentDataOut]:
    base_filter = [EadResult.run_id == run_id, EadResult.tenant_id == tenant_id]
    if stage:
        base_filter.append(EadResult.stage == stage)
    base_filter = _apply_min_ecl_filter(base_filter, min_ecl)

    ecl_sub = (
        select(
            EadResult.segment.label("segment"),
            func.sum(EadResult.discounted_ecl).label("ecl"),
        )
        .where(*base_filter)
        .group_by(EadResult.segment)
        .subquery()
    )

    outstanding_sub = (
        select(
            EadResult.segment.label("segment"),
            EadResult.loan_id.label("loan_id"),
            func.max(EadResult.bal_after_missed).label("ead_max"),
        )
        .where(*base_filter)
        .group_by(EadResult.segment, EadResult.loan_id)
        .subquery()
    )

    outstanding_agg = (
        select(
            outstanding_sub.c.segment,
            func.sum(outstanding_sub.c.ead_max).label("outstanding"),
            func.count(outstanding_sub.c.loan_id).label("loans"),
        )
        .group_by(outstanding_sub.c.segment)
        .subquery()
    )

    stage_sub = (
        select(
            EadResult.segment.label("segment"),
            EadResult.loan_id.label("loan_id"),
            func.min(EadResult.period_to_discount).label("min_period"),
        )
        .where(*base_filter)
        .group_by(EadResult.segment, EadResult.loan_id)
        .subquery()
    )

    initial_stage = (
        select(
            EadResult.segment.label("segment"),
            EadResult.stage.label("stage"),
            func.count(func.distinct(EadResult.loan_id)).label("cnt"),
        )
        .join(
            stage_sub,
            (EadResult.segment == stage_sub.c.segment)
            & (EadResult.loan_id == stage_sub.c.loan_id)
            & (EadResult.period_to_discount == stage_sub.c.min_period),
        )
        .where(*base_filter)
        .group_by(EadResult.segment, EadResult.stage)
        .subquery()
    )

    prior_ecl: dict[str, float] = {}
    if prior_run_id:
        prior_result = await db.execute(
            select(EadResult.segment, func.sum(EadResult.discounted_ecl))
            .where(EadResult.run_id == prior_run_id, EadResult.tenant_id == tenant_id)
            .group_by(EadResult.segment)
        )
        prior_ecl = {row[0]: float(row[1] or 0) for row in prior_result.all()}

    segments_result = await db.execute(
        select(
            ecl_sub.c.segment,
            ecl_sub.c.ecl,
            outstanding_agg.c.outstanding,
            outstanding_agg.c.loans,
        )
        .join(outstanding_agg, ecl_sub.c.segment == outstanding_agg.c.segment)
        .order_by(ecl_sub.c.ecl.desc())
    )

    stage_result = await db.execute(
        select(initial_stage.c.segment, initial_stage.c.stage, initial_stage.c.cnt)
    )
    stage_counts: dict[str, dict[str, int]] = {}
    for seg, stage, cnt in stage_result.all():
        stage_counts.setdefault(seg, {})[stage] = int(cnt)

    segments: list[SegmentDataOut] = []
    for row in segments_result.all():
        ecl = float(row.ecl or 0)
        outstanding = float(row.outstanding or 0)
        loans = int(row.loans or 0)
        coverage = format_coverage(ecl / outstanding) if outstanding else "0.00%"
        counts = stage_counts.get(row.segment, {})
        total = sum(counts.values()) or 1
        mix = (
            round(counts.get("Stage 1", 0) / total * 100, 1),
            round(counts.get("Stage 2", 0) / total * 100, 1),
            round(counts.get("Stage 3", 0) / total * 100, 1),
        )
        prior = prior_ecl.get(row.segment)
        delta = _pct_delta(ecl, prior) or 0.0
        segments.append(
            SegmentDataOut(
                name=row.segment,
                mix=mix,
                ecl=ecl,
                outstanding=outstanding,
                coverage=coverage,
                loans=loans,
                delta=delta,
            )
        )
    return segments


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
        EadResult.run_id == run_id,
        EadResult.tenant_id == tenant_id,
        EadResult.segment == segment_name,
    ]
    if stage:
        seg_filter.append(EadResult.stage == stage)
    seg_filter = _apply_min_ecl_filter(seg_filter, min_ecl)

    ecl_group = (
        select(
            EadResult.loan_id,
            EadResult.customer_id,
            func.sum(EadResult.discounted_ecl).label("ecl"),
            func.max(EadResult.bal_after_missed).label("ead"),
            func.avg(EadResult.lgd).label("lgd"),
        )
        .where(*seg_filter)
        .group_by(EadResult.loan_id, EadResult.customer_id)
    )
    if min_ecl is not None:
        ecl_group = ecl_group.having(func.sum(EadResult.discounted_ecl) >= min_ecl)
    ecl_sub = ecl_group.subquery()

    stage_sub = (
        select(
            EadResult.loan_id,
            func.min(EadResult.period_to_discount).label("min_period"),
        )
        .where(*seg_filter)
        .group_by(EadResult.loan_id)
        .subquery()
    )

    initial_stage = (
        select(EadResult.loan_id, EadResult.stage)
        .join(
            stage_sub,
            (EadResult.loan_id == stage_sub.c.loan_id)
            & (EadResult.period_to_discount == stage_sub.c.min_period),
        )
        .where(*seg_filter)
        .subquery()
    )

    pd_sub = (
        select(
            EadResult.loan_id,
            func.coalesce(func.sum(EadResult.marginal_pd), 0).label("pd_12m"),
        )
        .where(
            *seg_filter,
            EadResult.period_to_discount <= 12,
        )
        .group_by(EadResult.loan_id)
        .subquery()
    )

    count_q = select(func.count()).select_from(ecl_sub)
    total = (await db.execute(count_q)).scalar_one()

    offset = (page - 1) * per_page
    result = await db.execute(
        select(
            ecl_sub.c.loan_id,
            ecl_sub.c.customer_id,
            ecl_sub.c.ecl,
            ecl_sub.c.ead,
            ecl_sub.c.lgd,
            initial_stage.c.stage,
            pd_sub.c.pd_12m,
        )
        .outerjoin(initial_stage, ecl_sub.c.loan_id == initial_stage.c.loan_id)
        .outerjoin(pd_sub, ecl_sub.c.loan_id == pd_sub.c.loan_id)
        .order_by(ecl_sub.c.ecl.desc())
        .offset(offset)
        .limit(per_page)
    )

    loans: list[LoanRowOut] = []
    for row in result.all():
        loans.append(
            LoanRowOut(
                id=row.loan_id,
                customer=row.customer_id,
                stage=_stage_num(row.stage or "Stage 1"),
                pd=round(float(row.pd_12m or 0) * 100, 2),
                lgd=round(float(row.lgd or 0) * 100, 2),
                ead=round(float(row.ead or 0)),
                ecl=round(float(row.ecl or 0)),
            )
        )
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

    stage_sub = (
        select(
            EadResult.loan_id.label("loan_id"),
            func.min(EadResult.period_to_discount).label("min_period"),
        )
        .where(EadResult.run_id == latest.id, EadResult.tenant_id == tenant_id)
        .group_by(EadResult.loan_id)
        .subquery()
    )
    initial_stage = (
        select(EadResult.stage)
        .join(
            stage_sub,
            (EadResult.loan_id == stage_sub.c.loan_id)
            & (EadResult.period_to_discount == stage_sub.c.min_period),
        )
        .where(EadResult.run_id == latest.id, EadResult.tenant_id == tenant_id)
        .subquery()
    )
    stage_result = await db.execute(
        select(initial_stage.c.stage, func.count()).group_by(initial_stage.c.stage)
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

    agg_result = await db.execute(
        select(
            func.sum(EadResult.discounted_ecl).label("ecl"),
            func.max(EadResult.bal_after_missed).label("ead"),
            func.avg(EadResult.lgd).label("lgd"),
            func.max(EadResult.segment).label("segment"),
            func.max(EadResult.customer_id).label("customer"),
        ).where(
            EadResult.run_id == run.id,
            EadResult.tenant_id == tenant_id,
            EadResult.loan_id == loan_id,
        )
    )
    agg = agg_result.one_or_none()
    if not agg or agg.ecl is None:
        raise ECLException("RESOURCE_NOT_FOUND", f"Loan '{loan_id}' not found.", 404)

    stage_result = await db.execute(
        select(EadResult.stage)
        .where(
            EadResult.run_id == run.id,
            EadResult.tenant_id == tenant_id,
            EadResult.loan_id == loan_id,
        )
        .order_by(EadResult.period_to_discount.asc())
        .limit(1)
    )
    initial_stage = stage_result.scalar_one_or_none() or "Stage 1"

    pd_result = await db.execute(
        select(func.coalesce(func.sum(EadResult.marginal_pd), 0)).where(
            EadResult.run_id == run.id,
            EadResult.tenant_id == tenant_id,
            EadResult.loan_id == loan_id,
            EadResult.period_to_discount <= 12,
        )
    )
    pd_12m = float(pd_result.scalar_one() or 0)

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

    ead_val = round(float(agg.ead or 0))
    return LoanViewOut(
        id=loan_id,
        customer=agg.customer or "",
        stage=_stage_num(initial_stage),
        pd=round(pd_12m * 100, 2),
        lgd=round(float(agg.lgd or 0) * 100, 2),
        ead=ead_val,
        ecl=round(float(agg.ecl or 0)),
        segment=agg.segment or "",
        outstanding=ead_val,
        collateralValue=round(collateral),
        maturity=maturity,
        rundown=rundown,
    )


ARTIFACT_FILENAMES: dict[str, str] = {
    OutputArtifactKind.PD_CALCS.value: "PD Calcs.xlsx",
    OutputArtifactKind.LGD.value: "LGD.xlsx",
    OutputArtifactKind.RUNDOWN.value: "Contractual Rundown.xlsx",
    OutputArtifactKind.ECL_SUMMARY.value: "ECL Summary.xlsx",
}

WORKBOOK_BUNDLE_KINDS: tuple[str, ...] = (
    OutputArtifactKind.PD_CALCS.value,
    OutputArtifactKind.LGD.value,
    OutputArtifactKind.RUNDOWN.value,
)


async def _get_output_artifact(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    kind: str,
):
    from app.modules.results.models import OutputArtifact
    from app.modules.runs.models import Run

    run_result = await db.execute(
        select(Run.id).where(
            Run.id == run_id,
            Run.tenant_id == tenant_id,
            Run.deleted_at.is_(None),
        )
    )
    if not run_result.scalar_one_or_none():
        raise ECLException("RESOURCE_NOT_FOUND", "Run not found.", 404)

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

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for kind in WORKBOOK_BUNDLE_KINDS:
            artifact = await _get_output_artifact(db, tenant_id, run_id, kind)
            content = await download_bytes(artifact.storage_path)
            archive.writestr(ARTIFACT_FILENAMES[kind], content)

    return buffer.getvalue(), f"ECL_{run_id}_PD_LGD_EAD_workbooks.zip"
