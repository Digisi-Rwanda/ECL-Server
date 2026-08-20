import asyncio
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.cache import get_redis_client
from app.core.enums import MemberStatus, TenantStatus, UserRole
from app.core.exceptions import (
    AlreadySuspendedError,
    ECLException,
    NotSuspendedError,
)
from app.core.pagination import PageMeta, PageParams
from app.core.security import hash_password, new_ulid
from app.modules.audit.models import AuditEvent, AuditLog
from app.modules.audit.service import log_event
from app.modules.auth.models import User
from app.modules.auth.utils import unique_slug, user_initials
from app.modules.platform.models import EngineVersion, ImpersonationSession
from app.modules.platform.schemas import (
    AdminSummary,
    CreatePlatformTenantRequest,
    EngineVersionListOut,
    EngineVersionOut,
    PlatformKPIs,
    PlatformOverviewOut,
    PlatformTenantOut,
    PlatformUserOut,
    QueueStats,
    ServiceStatus,
    SystemHealthOut,
    TenantDetailOut,
    TenantsByPlan,
    TrendPoint,
    UpdatePlatformTenantRequest,
    UpdatePlatformUserRequest,
)
from app.modules.runs.models import Run
from app.modules.sessions.models import Session
from app.modules.sessions.service import relative_time
from app.modules.tenants.models import Tenant, TenantMembership


async def get_tenant_or_404(db: AsyncSession, tenant_id: str) -> Tenant:
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
    )
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise ECLException("RESOURCE_NOT_FOUND", "Tenant not found.", 404)
    return tenant


def _first_of_month() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _tenant_runs_this_month(db: AsyncSession, tenant_id: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.finished_at >= _first_of_month(),
            Run.deleted_at.is_(None),
        )
    )
    return result.scalar_one()


async def list_platform_tenants(
    db: AsyncSession, params: PageParams, status_filter: str | None = None
) -> tuple[list[PlatformTenantOut], PageMeta]:
    q = select(Tenant).where(Tenant.deleted_at.is_(None)).order_by(Tenant.created_at.desc())
    if status_filter:
        q = q.where(Tenant.status == status_filter)

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    offset = (params.page - 1) * params.per_page
    result = await db.execute(q.offset(offset).limit(params.per_page))
    tenants = result.scalars().all()

    data = []
    for t in tenants:
        runs_count = await _tenant_runs_this_month(db, t.id)
        data.append(
            PlatformTenantOut(
                id=t.id,
                name=t.name,
                slug=t.slug,
                plan=t.plan,
                status=t.status,
                created_at=t.created_at,
                mrr=t.mrr_cents / 100.0,
                region=t.region,
                runs_count=runs_count,
            )
        )

    pages = max(1, (total + params.per_page - 1) // params.per_page)
    meta = PageMeta(
        total=total,
        page=params.page,
        per_page=params.per_page,
        pages=pages,
        has_next=params.page < pages,
        has_prev=params.page > 1,
    )
    return data, meta


async def create_platform_tenant(
    db: AsyncSession, body: CreatePlatformTenantRequest, actor_user_id: str
) -> dict[str, str]:
    slugs = {r[0] for r in (await db.execute(select(Tenant.slug))).all()}
    slug = await unique_slug(body.name, slugs)
    status = TenantStatus.TRIAL.value if body.start_trial else TenantStatus.ACTIVE.value
    tenant = Tenant(
        id=new_ulid(),
        name=body.name,
        slug=slug,
        plan=body.plan,
        status=status,
        region=body.region,
    )
    user = User(
        id=new_ulid(),
        email=body.admin_email.lower(),
        name=body.admin_name,
        hashed_password=hash_password("ChangeMe123!"),
        is_email_verified=False,
    )
    membership = TenantMembership(
        id=new_ulid(),
        user_id=user.id,
        tenant_id=tenant.id,
        role=UserRole.ADMINISTRATOR.value,
        status=MemberStatus.ACTIVE.value,
    )
    db.add_all([tenant, user, membership])
    await db.flush()

    await log_event(
        db,
        AuditEvent.TENANT_PROVISIONED,
        user_id=actor_user_id,
        tenant_id=None,
        details={"tenant_id": tenant.id, "admin_user_id": user.id},
    )
    return {"tenant_id": tenant.id, "user_id": user.id}


async def patch_platform_tenant(
    db: AsyncSession, tenant_id: str, body: UpdatePlatformTenantRequest
) -> None:
    t = await get_tenant_or_404(db, tenant_id)
    if body.name:
        t.name = body.name
    if body.plan:
        t.plan = body.plan
    if body.status:
        t.status = body.status


async def list_platform_users(
    db: AsyncSession, params: PageParams
) -> tuple[list[PlatformUserOut], PageMeta]:
    q = select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc())
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    offset = (params.page - 1) * params.per_page
    result = await db.execute(q.offset(offset).limit(params.per_page))
    users = result.scalars().all()

    data = []
    for u in users:
        mem_result = await db.execute(
            select(TenantMembership, Tenant)
            .join(Tenant, Tenant.id == TenantMembership.tenant_id)
            .where(TenantMembership.user_id == u.id, TenantMembership.status == MemberStatus.ACTIVE.value)
            .order_by(TenantMembership.joined_at.desc())
            .limit(1)
        )
        row = mem_result.first()
        tenant_name = row[1].name if row else None
        role = row[0].role if row else None

        last_active_result = await db.execute(
            select(func.max(Session.last_active_at)).where(Session.user_id == u.id)
        )
        last_active_at = last_active_result.scalar_one()

        data.append(
            PlatformUserOut(
                id=u.id,
                name=u.name,
                email=u.email,
                is_active=u.is_active,
                is_platform_admin=u.is_platform_admin,
                last_login_at=u.last_login_at,
                tenant_name=tenant_name,
                role=role,
                last_active_at=last_active_at,
            )
        )

    pages = max(1, (total + params.per_page - 1) // params.per_page)
    meta = PageMeta(
        total=total,
        page=params.page,
        per_page=params.per_page,
        pages=pages,
        has_next=params.page < pages,
        has_prev=params.page > 1,
    )
    return data, meta


async def patch_platform_user(
    db: AsyncSession, user_id: str, body: UpdatePlatformUserRequest
) -> None:
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    if not u:
        raise ECLException("RESOURCE_NOT_FOUND", "User not found.", 404)
    if body.is_active is not None:
        u.is_active = body.is_active


async def get_platform_overview(db: AsyncSession) -> PlatformOverviewOut:
    tenant_stats = await db.execute(
        select(
            func.count().label("total"),
            func.sum(case((Tenant.status == "active", 1), else_=0)).label("active"),
            func.sum(case((Tenant.status == "trial", 1), else_=0)).label("trial"),
            func.sum(case((Tenant.status == "suspended", 1), else_=0)).label("suspended"),
            func.coalesce(func.sum(Tenant.mrr_cents), 0).label("mrr_cents"),
        ).where(Tenant.deleted_at.is_(None))
    )
    ts = tenant_stats.one()
    first_of_month = _first_of_month()
    today = date.today()

    runs_month = (
        await db.execute(
            select(func.count())
            .select_from(Run)
            .where(Run.created_at >= first_of_month, Run.deleted_at.is_(None))
        )
    ).scalar_one()

    runs_today = (
        await db.execute(
            select(func.count())
            .select_from(Run)
            .where(func.date(Run.created_at) == today, Run.deleted_at.is_(None))
        )
    ).scalar_one()

    trend_result = await db.execute(
        select(func.date(Run.finished_at).label("day"), func.count().label("count"))
        .where(
            Run.status == "complete",
            Run.finished_at >= datetime.now(UTC) - timedelta(days=14),
            Run.deleted_at.is_(None),
        )
        .group_by(func.date(Run.finished_at))
        .order_by(func.date(Run.finished_at))
    )
    trend_map = {str(row.day): row.count for row in trend_result.all()}
    trend_14d = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        trend_14d.append(TrendPoint(date=d, runs=trend_map.get(d, 0)))

    plan_result = await db.execute(
        select(Tenant.plan, func.count())
        .where(Tenant.deleted_at.is_(None))
        .group_by(Tenant.plan)
    )
    plan_counts = {row[0]: row[1] for row in plan_result.all()}
    tenants_by_plan = TenantsByPlan(
        trial=plan_counts.get("trial", 0),
        starter=plan_counts.get("starter", 0),
        growth=plan_counts.get("growth", 0),
        enterprise=plan_counts.get("enterprise", 0),
    )

    audit_result = await db.execute(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(4)
    )
    recent_audit = [
        {
            "event_type": e.event_type,
            "user_id": e.user_id,
            "tenant_id": e.tenant_id,
            "created_at": e.created_at.isoformat(),
            "details": e.details,
        }
        for e in audit_result.scalars().all()
    ]

    return PlatformOverviewOut(
        kpis=PlatformKPIs(
            tenants_total=ts.total or 0,
            tenants_active=ts.active or 0,
            tenants_trial=ts.trial or 0,
            tenants_suspended=ts.suspended or 0,
            runs_this_month=runs_month,
            runs_today=runs_today,
            mrr_total_usd=(ts.mrr_cents or 0) / 100.0,
        ),
        uptime_30d="99.98%",
        trend_14d=trend_14d,
        tenants_by_plan=tenants_by_plan,
        recent_audit=recent_audit,
    )


async def get_tenant_detail(db: AsyncSession, tenant_id: str) -> TenantDetailOut:
    tenant = await get_tenant_or_404(db, tenant_id)

    users_count = (
        await db.execute(
            select(func.count())
            .select_from(TenantMembership)
            .where(
                TenantMembership.tenant_id == tenant_id,
                TenantMembership.status == MemberStatus.ACTIVE.value,
            )
        )
    ).scalar_one()

    runs_this_month = await _tenant_runs_this_month(db, tenant_id)

    admins_result = await db.execute(
        select(TenantMembership, User)
        .join(User, User.id == TenantMembership.user_id)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.role == UserRole.ADMINISTRATOR.value,
            TenantMembership.status == MemberStatus.ACTIVE.value,
        )
        .limit(5)
    )
    admins = [
        AdminSummary(
            id=u.id,
            name=u.name,
            email=u.email,
            initials=user_initials(u.name),
        )
        for _, u in admins_result.all()
    ]

    last_run_result = await db.execute(
        select(Run.finished_at)
        .where(Run.tenant_id == tenant_id, Run.status == "complete", Run.deleted_at.is_(None))
        .order_by(Run.finished_at.desc())
        .limit(1)
    )
    last_run = last_run_result.scalar_one_or_none()

    if tenant.status == TenantStatus.TRIAL.value:
        days_since = (date.today() - tenant.created_at.date()).days
        status_note = f"Trial ends in {max(0, 14 - days_since)} days."
    elif tenant.status == TenantStatus.ACTIVE.value:
        last_label = relative_time(last_run) if last_run else "never"
        status_note = f"Healthy. Last run {last_label}."
    elif tenant.status == TenantStatus.SUSPENDED.value:
        suspended_on = (
            tenant.close_requested_at.strftime("%b %d, %Y")
            if tenant.close_requested_at
            else "unknown date"
        )
        status_note = f"Suspended on {suspended_on}. Data retained per policy."
    elif tenant.status == TenantStatus.CLOSING.value:
        closed_on = (
            tenant.close_requested_at.strftime("%b %d, %Y")
            if tenant.close_requested_at
            else "unknown date"
        )
        status_note = f"Closure requested on {closed_on}."
    else:
        status_note = f"Status: {tenant.status}."

    return TenantDetailOut(
        id=tenant.id,
        name=tenant.name,
        mark=tenant.name[0].upper() if tenant.name else "?",
        plan=tenant.plan,
        status=tenant.status,
        created_at=tenant.created_at,
        users_count=users_count,
        runs_this_month=runs_this_month,
        mrr=tenant.mrr_cents / 100.0,
        engine_version_pin=tenant.engine_version_pin,
        region=tenant.region,
        admins=admins,
        status_note=status_note,
    )


async def suspend_tenant(
    db: AsyncSession, tenant_id: str, actor_user_id: str, reason: str | None = None
) -> None:
    tenant = await get_tenant_or_404(db, tenant_id)
    if tenant.status == TenantStatus.SUSPENDED.value:
        raise AlreadySuspendedError()
    tenant.status = TenantStatus.SUSPENDED.value
    await db.flush()
    await log_event(
        db,
        AuditEvent.TENANT_SUSPENDED,
        user_id=actor_user_id,
        tenant_id=None,
        details={"tenant_id": tenant_id, "reason": reason},
    )


async def reactivate_tenant(db: AsyncSession, tenant_id: str, actor_user_id: str) -> None:
    tenant = await get_tenant_or_404(db, tenant_id)
    if tenant.status != TenantStatus.SUSPENDED.value:
        raise NotSuspendedError()
    tenant.status = TenantStatus.ACTIVE.value
    await db.flush()
    await log_event(
        db,
        AuditEvent.TENANT_REACTIVATED,
        user_id=actor_user_id,
        tenant_id=None,
        details={"tenant_id": tenant_id},
    )


async def extend_trial(db: AsyncSession, tenant_id: str, actor_user_id: str, days: int) -> None:
    if not 1 <= days <= 90:
        raise ECLException("INVALID_DAYS", "Trial extension must be between 1 and 90 days.", 400)
    await get_tenant_or_404(db, tenant_id)
    await log_event(
        db,
        AuditEvent.TENANT_TRIAL_EXTENDED,
        user_id=actor_user_id,
        tenant_id=None,
        details={"tenant_id": tenant_id, "extended_days": days},
    )


async def start_impersonation(
    db: AsyncSession, platform_user_id: str, target_tenant_id: str
) -> ImpersonationSession:
    await get_tenant_or_404(db, target_tenant_id)

    existing = (
        await db.execute(
            select(ImpersonationSession).where(
                ImpersonationSession.platform_user_id == platform_user_id,
                ImpersonationSession.is_active.is_(True),
            )
        )
    ).scalars().all()
    for session in existing:
        session.is_active = False
        session.ended_at = datetime.now(UTC)

    imp = ImpersonationSession(
        platform_user_id=platform_user_id,
        target_tenant_id=target_tenant_id,
    )
    db.add(imp)
    await db.flush()
    await log_event(
        db,
        AuditEvent.IMPERSONATION_STARTED,
        user_id=platform_user_id,
        tenant_id=None,
        details={"target_tenant_id": target_tenant_id, "impersonation_id": imp.id},
    )
    return imp


async def end_impersonation(
    db: AsyncSession, platform_user_id: str, target_tenant_id: str
) -> None:
    imp = (
        await db.execute(
            select(ImpersonationSession).where(
                ImpersonationSession.platform_user_id == platform_user_id,
                ImpersonationSession.target_tenant_id == target_tenant_id,
                ImpersonationSession.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if imp:
        imp.is_active = False
        imp.ended_at = datetime.now(UTC)
        await db.flush()
    await log_event(
        db,
        AuditEvent.IMPERSONATION_ENDED,
        user_id=platform_user_id,
        tenant_id=None,
        details={"target_tenant_id": target_tenant_id},
    )


async def get_system_health(db: AsyncSession) -> SystemHealthOut:
    redis = await get_redis_client()
    try:
        cached = await redis.get("platform:health:v1")
        if cached:
            return SystemHealthOut.model_validate_json(cached)
    except Exception:
        pass  # cache unavailable — fall through and compute fresh

    settings = get_settings()
    services: list[ServiceStatus] = []
    running, queued, workers = 0, 0, 0

    services.append(ServiceStatus(name="API Gateway", state="ok", value="operational"))

    try:
        from app.tasks.celery_app import celery_app

        def _celery_check():
            celery_inspect = celery_app.control.inspect()
            active = celery_inspect.active() or {}
            reserved = celery_inspect.reserved() or {}
            running_count = sum(len(v) for v in active.values())
            queued_count = sum(len(v) for v in reserved.values())
            worker_count = len(active)
            return running_count, queued_count, worker_count

        running, queued, workers = await asyncio.wait_for(
            asyncio.to_thread(_celery_check),
            timeout=3.0,
        )
        worker_state = "ok" if workers > 0 else "warn"
        services.append(
            ServiceStatus(
                name="Engine Workers",
                state=worker_state,
                value=f"{running} running / {workers} worker{'s' if workers != 1 else ''}",
            )
        )
    except (asyncio.TimeoutError, Exception):
        services.append(
            ServiceStatus(name="Engine Workers", state="warn", value="unreachable")
        )

    try:
        from app.core.storage import get_storage_client

        client = await get_storage_client()
        await asyncio.wait_for(
            client.head_bucket(Bucket=settings.storage_bucket_name),
            timeout=2.0,
        )
        services.append(ServiceStatus(name="Object Storage", state="ok", value="available"))
    except Exception:
        services.append(
            ServiceStatus(name="Object Storage", state="down", value="unreachable")
        )

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(settings.smtp_host, settings.smtp_port),
            timeout=2.0,
        )
        writer.close()
        await writer.wait_closed()
        services.append(ServiceStatus(name="Email Delivery", state="ok", value="operational"))
    except Exception:
        services.append(
            ServiceStatus(name="Email Delivery", state="warn", value="SMTP unreachable")
        )

    avg_wait = queued * 60
    queue_stats = QueueStats(running=running, queued=queued, avg_wait_seconds=avg_wait)
    degraded_states = {"warn", "down"}
    overall = "degraded" if any(s.state in degraded_states for s in services) else "ok"

    recent_errors_rows = await db.execute(
        select(AuditLog)
        .where(AuditLog.status == "error")
        .order_by(AuditLog.created_at.desc())
        .limit(5)
    )
    recent_errors = [
        {
            "event_type": r.event_type,
            "created_at": r.created_at.isoformat(),
            "details": r.details,
        }
        for r in recent_errors_rows.scalars().all()
    ]

    result = SystemHealthOut(
        services=services,
        queue_stats=queue_stats,
        overall_state=overall,
        recent_errors=recent_errors,
    )
    try:
        await redis.setex("platform:health:v1", 30, result.model_dump_json())
    except Exception:
        pass  # cache unavailable — the endpoint still returns freshly computed data
    return result


async def list_engine_versions(db: AsyncSession) -> EngineVersionListOut:
    result = await db.execute(
        select(EngineVersion).order_by(EngineVersion.created_at.desc())
    )
    versions = result.scalars().all()

    pin_result = await db.execute(
        select(Tenant.engine_version_pin, func.count())
        .where(Tenant.engine_version_pin.isnot(None), Tenant.deleted_at.is_(None))
        .group_by(Tenant.engine_version_pin)
    )
    pin_counts = {row[0]: row[1] for row in pin_result.all()}

    out = [
        EngineVersionOut(
            id=v.id,
            version=v.version,
            is_current=v.is_current,
            release_date=v.release_date,
            changelog=v.changelog if isinstance(v.changelog, list) else [],
            tenants_pinned=pin_counts.get(v.version, 0),
            created_at=v.created_at,
        )
        for v in versions
    ]
    return EngineVersionListOut(versions=out, total=len(out))


async def promote_engine_version(
    db: AsyncSession, version: str, actor_user_id: str
) -> None:
    result = await db.execute(select(EngineVersion).where(EngineVersion.version == version))
    target = result.scalar_one_or_none()
    if not target:
        raise ECLException("ENGINE_VERSION_NOT_FOUND", "Engine version not found.", 404)

    current_result = await db.execute(
        select(EngineVersion).where(EngineVersion.is_current.is_(True))
    )
    old_current = current_result.scalar_one_or_none()
    old_version_str = old_current.version if old_current else None

    if old_current and old_current.id != target.id:
        old_current.is_current = False
    target.is_current = True
    await db.flush()

    await log_event(
        db,
        AuditEvent.ENGINE_PROMOTED,
        user_id=actor_user_id,
        tenant_id=None,
        details={"old_version": old_version_str, "new_version": version},
    )
