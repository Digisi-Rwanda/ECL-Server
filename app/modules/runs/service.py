"""Runs service — create, upload, validate, execute, list, detail, delete."""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.enums import UserRole
from app.core.exceptions import ECLException
from app.core.pagination import PageMeta
from app.core.run_enums import RunStatus, UploadKind, ValidationStatus
from app.core.security import new_ulid
from app.core.storage import build_storage_path, delete_object, download_bytes, upload_stream
from app.engine import (
    EAD_METHOD,
    ENGINE_RELEASED_DATE,
    ENGINE_VERSION,
    LGD_METHOD,
    PD_METHOD,
)
from app.engine.format_utils import (
    format_amount,
    format_compact_amount,
    format_coverage,
    map_run_status_to_api,
    short_ulid,
)
from app.engine.validators.base import ValidationIssue
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import log_event
from app.modules.auth.models import User
from app.modules.auth.utils import user_initials
from app.modules.runs.models import Run, Upload
from app.modules.tenants.models import TenantMembership


import re as _re

from app.core.logging import get_logger

log = get_logger("runs.service")


_REDISPATCH_AFTER = timedelta(minutes=2)


def _compute_has_started(progress: object) -> bool:
    """True once any engine stage has left pending — used to retry a stuck queue."""
    if not isinstance(progress, dict):
        return False
    for stage in ("pd", "lgd", "ead", "ecl"):
        status = (progress.get(stage) or {}).get("status")
        if status in {"running", "complete", "failed"}:
            return True
        if stage == "ead" and status == "queued":
            return True
    return False


_ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED.value,
        RunStatus.PD_RUNNING.value,
        RunStatus.LGD_RUNNING.value,
        RunStatus.EAD_RUNNING.value,
    }
)

_COLLATERAL_DEFAULTS: dict[str, dict] = {
    "real estate":          {"haircut": 15.0, "time_to_realize": 18},
    "residential property": {"haircut": 15.0, "time_to_realize": 18},
    "commercial property":  {"haircut": 20.0, "time_to_realize": 24},
    "land":                 {"haircut": 20.0, "time_to_realize": 24},
    "motor vehicle":        {"haircut": 25.0, "time_to_realize": 6},
    "vehicle":              {"haircut": 25.0, "time_to_realize": 6},
    "cash deposit":         {"haircut": 0.0,  "time_to_realize": 0},
    "cash":                 {"haircut": 0.0,  "time_to_realize": 0},
    "deposit":              {"haircut": 0.0,  "time_to_realize": 1},
    "corporate guarantee":  {"haircut": 20.0, "time_to_realize": 12},
    "personal guarantee":   {"haircut": 30.0, "time_to_realize": 12},
    "guarantee":            {"haircut": 25.0, "time_to_realize": 12},
    "equipment":            {"haircut": 30.0, "time_to_realize": 12},
    "machinery":            {"haircut": 35.0, "time_to_realize": 12},
    "government bond":      {"haircut": 5.0,  "time_to_realize": 1},
    "bond":                 {"haircut": 10.0, "time_to_realize": 3},
    "stock":                {"haircut": 30.0, "time_to_realize": 5},
    "shares":               {"haircut": 30.0, "time_to_realize": 5},
    "insurance":            {"haircut": 20.0, "time_to_realize": 6},
    "inventory":            {"haircut": 50.0, "time_to_realize": 6},
    "receivables":          {"haircut": 40.0, "time_to_realize": 3},
}
_COLLATERAL_FALLBACK: dict = {"haircut": 50.0, "time_to_realize": 24}


def _collateral_default(name: str) -> dict:
    return _COLLATERAL_DEFAULTS.get(name.strip().lower(), _COLLATERAL_FALLBACK)


def _segment_code(name: str) -> str:
    return _re.sub(r"[^A-Z0-9_]", "_", name.strip().upper())[:20]


async def _auto_provision_segments(
    db: AsyncSession, tenant_id: str, user_id: str, names: set[str]
) -> None:
    from app.modules.segments.models import Segment
    from app.modules.segments.service import batch_create_segments
    from app.modules.segments.schemas import BatchCreateSegmentsRequest, CreateSegmentRequest

    # Check including soft-deleted rows to avoid UniqueViolation on the (tenant_id, name) index
    result = await db.execute(
        select(Segment.name).where(
            Segment.tenant_id == tenant_id,
            Segment.name.in_(list(names)),
        )
    )
    already_exist = {row[0] for row in result.all()}
    truly_new = names - already_exist
    if not truly_new:
        return

    req = BatchCreateSegmentsRequest(segments=[
        CreateSegmentRequest(name=n, code=_segment_code(n)) for n in sorted(truly_new)
    ])
    # A concurrent validation of the same files may insert these first. A
    # savepoint keeps that unique-violation from aborting this request's session;
    # either way the segments exist afterwards.
    try:
        async with db.begin_nested():
            await batch_create_segments(db, tenant_id, user_id, req)
    except IntegrityError:
        log.info("segments_provisioned_concurrently", tenant_id=tenant_id, names=sorted(truly_new))


def get_run_scope_filter(
    user: User,
    tenant_id: str,
    membership: TenantMembership | None,
):
    """Returns a SQLAlchemy WHERE clause fragment that scopes run visibility."""
    if user.is_platform_admin:
        return Run.tenant_id == tenant_id

    if membership is None:
        raise ECLException("NO_MEMBERSHIP", "User has no membership in this tenant.", 403)

    if membership.role in (UserRole.ADMINISTRATOR.value, UserRole.REVIEWER.value):
        return Run.tenant_id == tenant_id

    return and_(Run.tenant_id == tenant_id, Run.created_by_user_id == user.id)


async def assert_run_visible(
    db: AsyncSession,
    user: User,
    tenant_id: str,
    membership: TenantMembership | None,
    run_id: str,
) -> Run:
    result = await db.execute(
        select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id, Run.deleted_at.is_(None))
    )
    run = result.scalar_one_or_none()
    if not run:
        raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)

    if not user.is_platform_admin:
        if membership is None:
            raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)
        if membership.role == UserRole.ANALYST.value:
            if run.created_by_user_id != user.id:
                raise ECLException("RUN_NOT_FOUND", "Run not found.", 404)

    return run
from app.modules.runs.schemas import (
    CreateRunRequest,
    EngineInfoOut,
    EngineProgressOut,
    EngineStageProgressOut,
    ExecuteRunOut,
    FailureDetailsOut,
    KpiOut,
    RerunRequest,
    RunAuditEventOut,
    RunDetailOut,
    RunListItemOut,
    SegmentBarOut,
    UpdateRunRequest,
    UploadOut,
    hash_display,
    upload_to_input_file,
)
from app.modules.segments.models import Segment
from app.modules.tenants.models import Tenant

_AUDIT_DISPLAY: dict[str, tuple[str, str, str]] = {
    AuditEvent.RUN_CREATED.value: ("accent", "Plus", "Run created"),
    AuditEvent.RUN_RERUN_CREATED.value: ("accent", "RefreshCcw", "Re-run created"),
    AuditEvent.FILE_UPLOADED.value: ("default", "Upload", "Files uploaded"),
    AuditEvent.VALIDATION_TRIGGERED.value: ("default", "FileSearch", "Validation started"),
    AuditEvent.VALIDATION_COMPLETED.value: ("ok", "Check", "Validation passed"),
    AuditEvent.RUN_QUEUED.value: ("accent", "Zap", "Computation started"),
    AuditEvent.RUN_COMPLETED.value: ("ok", "Check", "Run completed"),
    AuditEvent.RUN_FAILED.value: ("err", "X", "Run failed"),
    AuditEvent.RUN_DELETED.value: ("default", "Trash2", "Run soft-deleted"),
}


def _default_engine_progress() -> dict[str, Any]:
    stage = {
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "elapsed_ms": None,
    }
    return {"pd": dict(stage), "lgd": dict(stage), "ead": dict(stage), "ecl": dict(stage)}


def _issue_id(issue: ValidationIssue) -> str:
    digest = hashlib.sha256(f"{issue.title}:{issue.location}".encode()).hexdigest()
    return digest[:12]


@dataclass(frozen=True, slots=True)
class TaggedValidationIssue:
    kind: str
    issue: ValidationIssue
    upload_id: str | None = None
    filename: str | None = None


def _format_dt(dt: datetime | None, tz_name: str = "UTC") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%d %b %H:%M")


def _format_date(dt: datetime | None, tz_name: str = "UTC") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%d %b %Y")


def _format_time(dt: datetime | None, tz_name: str = "UTC") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(ZoneInfo(tz_name))
    return local.strftime("%H:%M:%S")


def _format_elapsed(started_at: datetime | None, finished_at: datetime | None) -> str | None:
    if not started_at or not finished_at:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=UTC)
    seconds = int((finished_at - started_at).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m {secs}s" if secs else f"{minutes}m"


def _stage_out(data: dict[str, Any] | None) -> EngineStageProgressOut:
    data = data or {}
    return EngineStageProgressOut(
        status=data.get("status", "pending"),
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        elapsed_ms=data.get("elapsed_ms"),
    )


def _engine_progress_out(progress: dict[str, Any] | None) -> EngineProgressOut | None:
    if not progress:
        return None
    return EngineProgressOut(
        pd=_stage_out(progress.get("pd")),
        lgd=_stage_out(progress.get("lgd")),
        ead=_stage_out(progress.get("ead")),
        ecl=_stage_out(progress.get("ecl")),
    )


async def _get_tenant(db: AsyncSession, tenant_id: str) -> Tenant:
    result = await db.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
    )
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise ECLException("RESOURCE_NOT_FOUND", "Workspace not found.", 404)
    return tenant


async def _get_run(db: AsyncSession, tenant_id: str, run_id: str) -> Run:
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
    return run


async def _get_run_for_update(db: AsyncSession, tenant_id: str, run_id: str) -> Run:
    result = await db.execute(
        select(Run)
        .where(
            Run.id == run_id,
            Run.tenant_id == tenant_id,
            Run.deleted_at.is_(None),
        )
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if not run:
        raise ECLException("RESOURCE_NOT_FOUND", "Run not found.", 404)
    return run


async def _clear_run_results(db: AsyncSession, run_id: str, tenant_id: str) -> None:
    from app.modules.results.models import (
        EadResult,
        EclLoanSummary,
        LgdResult,
        OutputArtifact,
        PdObservationSnapshot,
        PdResult,
        RunException,
    )

    await db.execute(
        delete(PdObservationSnapshot).where(
            PdObservationSnapshot.run_id == run_id,
            PdObservationSnapshot.tenant_id == tenant_id,
        )
    )
    await db.execute(
        delete(PdResult).where(PdResult.run_id == run_id, PdResult.tenant_id == tenant_id)
    )
    await db.execute(
        delete(LgdResult).where(LgdResult.run_id == run_id, LgdResult.tenant_id == tenant_id)
    )
    for model in (EadResult, EclLoanSummary, RunException):
        await db.execute(delete(model).where(model.run_id == run_id, model.tenant_id == tenant_id))
    await db.execute(
        delete(OutputArtifact).where(
            OutputArtifact.run_id == run_id,
            OutputArtifact.tenant_id == tenant_id,
        )
    )


async def _get_user(db: AsyncSession, user_id: str) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise ECLException("RESOURCE_NOT_FOUND", "User not found.", 404)
    return user


async def _allowed_segments(db: AsyncSession, tenant_id: str) -> set[str]:
    result = await db.execute(
        select(Segment.name).where(
            Segment.tenant_id == tenant_id,
            Segment.deleted_at.is_(None),
        )
    )
    return {row[0] for row in result.all()}


_ALLOWED_CONTENT_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/octet-stream",
    "application/vnd.ms-excel",
}


def _assert_upload_safe(content: bytes, content_type: str) -> None:
    """Reject non-xlsx uploads and files containing VBA macros."""
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise ECLException(
            "UNSUPPORTED_MEDIA_TYPE",
            f"Uploaded file has unsupported content type '{content_type}'. "
            "Only .xlsx workbooks are accepted.",
            415,
        )
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            if any("vbaProject" in name for name in zf.namelist()):
                raise ECLException(
                    "UNSAFE_FILE",
                    "Uploaded file contains VBA macros, which are not permitted.",
                    415,
                )
    except zipfile.BadZipFile:
        raise ECLException(
            "INVALID_FILE",
            "Uploaded file is not a valid .xlsx workbook.",
            415,
        )


def _parse_excel(content: bytes) -> tuple[dict[str, pd.DataFrame], int, int]:
    from app.engine.workbook import read_workbook_sheets, sheets_row_count

    sheets = read_workbook_sheets(content)
    sheet_count, row_count = sheets_row_count(sheets)
    return sheets, sheet_count, row_count


def _combine_sheets(sheets: dict[str, pd.DataFrame]) -> pd.DataFrame:
    from app.engine.workbook import concat_sheets

    return concat_sheets(sheets.values())


async def _load_uploads(db: AsyncSession, run_id: str) -> list[Upload]:
    result = await db.execute(
        select(Upload).where(Upload.run_id == run_id).order_by(Upload.created_at)
    )
    return list(result.scalars().all())


async def _audit_events_for_run(
    db: AsyncSession,
    run_id: str,
    tz_name: str,
) -> list[RunAuditEventOut]:
    from app.modules.audit.models import AuditLog

    result = await db.execute(
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.details["run_id"].as_string() == run_id)
        .order_by(AuditLog.created_at)
    )
    events: list[RunAuditEventOut] = []
    for log, user in result.all():
        kind, icon, title = _AUDIT_DISPLAY.get(
            log.event_type, ("default", "Clock", log.event_type)
        )
        details = log.details or {}
        description = str(details.get("description", details.get("message", "")))
        who = user.name if user else "system"
        events.append(
            RunAuditEventOut(
                id=log.id,
                kind=kind,  # type: ignore[arg-type]
                iconName=icon,
                title=title,
                description=description,
                who=who,
                time=_format_time(log.created_at, tz_name),
            )
        )
    return events


def _build_kpis(run: Run, loan_count: int | None = None) -> list[KpiOut]:
    if run.status != RunStatus.COMPLETE.value:
        return [
            KpiOut(id="total-ecl", label="Total ECL", currencyPrefix=None, value="—"),
            KpiOut(id="coverage", label="Coverage ratio", value="—"),
            KpiOut(id="outstanding", label="Total outstanding", currencyPrefix=None, value="—"),
            KpiOut(id="loans", label="Loans analysed", value="—"),
        ]
    total_ecl = float(run.total_ecl or 0)
    outstanding = float(run.total_outstanding or 0)
    coverage = format_coverage(float(run.coverage_ratio or 0)) if run.coverage_ratio else "—"
    loans_note = f"{loan_count:,} loans" if loan_count else None
    return [
        KpiOut(
            id="total-ecl",
            label="Total ECL",
            currencyPrefix=None,
            value=format_amount(total_ecl),
        ),
        KpiOut(
            id="coverage",
            label="Coverage ratio",
            helpText="ECL ÷ total outstanding",
            value=coverage,
        ),
        KpiOut(
            id="outstanding",
            label="Total outstanding",
            currencyPrefix=None,
            value=format_compact_amount(outstanding),
            subNote=loans_note,
        ),
        KpiOut(
            id="loans",
            label="Loans analysed",
            value=f"{loan_count:,}" if loan_count else "—",
        ),
    ]


async def _segment_bars(db: AsyncSession, run_id: str, tenant_id: str) -> list[SegmentBarOut]:
    from app.modules.results.models import EclLoanSummary as S

    # Capped loan-level Total ECL (spec Step 11), not the uncapped row sum.
    result = await db.execute(
        select(S.segment, func.sum(S.total_ecl).label("ecl"))
        .where(S.run_id == run_id, S.tenant_id == tenant_id)
        .group_by(S.segment)
        .order_by(func.sum(S.total_ecl).desc())
        .limit(6)
    )
    return [
        SegmentBarOut(name=row.segment, value=float(row.ecl or 0)) for row in result.all()
    ]


def _engine_info(run: Run) -> EngineInfoOut:
    return EngineInfoOut(
        version=run.engine_version,
        releasedDate=ENGINE_RELEASED_DATE,
        pdMethod=PD_METHOD,
        lgdMethod=LGD_METHOD,
        eadMethod=EAD_METHOD,
        pdFilesCombined=run.combine_pd_files,
        deterministic=True,
    )


async def _input_files(
    db: AsyncSession, run_id: str, uploads: list[Upload] | None = None
) -> list[RunInputFileOut]:
    uploads = uploads or await _load_uploads(db, run_id)
    files: list[RunInputFileOut] = []
    for upload in uploads:
        warning_count = 0
        if upload.validation_status == ValidationStatus.WARN.value:
            warning_count = 1
        files.append(
            upload_to_input_file(
                kind=upload.kind,
                name=upload.original_filename,
                file_size_bytes=upload.file_size_bytes,
                sheet_count=upload.sheet_count,
                validation_status=upload.validation_status,
                warning_count=warning_count,
                sha256=upload.sha256,
            )
        )
    return files


async def _latest_uploads_by_kind(db: AsyncSession, tenant_id: str, run_id: str) -> dict[str, Upload]:
    run = (
        await db.execute(
            select(Run.id).where(Run.id == run_id, Run.tenant_id == tenant_id, Run.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not run:
        raise ECLException("RESOURCE_NOT_FOUND", "Run not found.", 404)
    # Ordered oldest-first, so a re-upload of the same kind wins.
    return {u.kind.upper(): u for u in await _load_uploads(db, run_id)}


async def get_input_file_bytes(
    db: AsyncSession, tenant_id: str, run_id: str, kind: str
) -> tuple[bytes, str]:
    upload = (await _latest_uploads_by_kind(db, tenant_id, run_id)).get(kind.upper())
    if upload is None:
        raise ECLException("RESOURCE_NOT_FOUND", f"No {kind.upper()} input for this run.", 404)
    return await download_bytes(upload.storage_path), upload.original_filename


async def build_inputs_bundle(db: AsyncSession, tenant_id: str, run_id: str) -> tuple[bytes, str]:
    uploads = await _latest_uploads_by_kind(db, tenant_id, run_id)
    if not uploads:
        raise ECLException("RESOURCE_NOT_FOUND", "This run has no input files.", 404)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for kind, upload in sorted(uploads.items()):
            # Prefix with the kind so two inputs sharing a filename don't collide.
            archive.writestr(f"{kind}_{upload.original_filename}", await download_bytes(upload.storage_path))
    return buffer.getvalue(), f"ECL_{run_id}_inputs.zip"


async def _run_list_item(
    run: Run,
    user: User,
    tenant: Tenant,
) -> RunListItemOut:
    period = run.reporting_period or run.name
    coverage = (
        format_coverage(float(run.coverage_ratio))
        if run.coverage_ratio is not None and run.status == RunStatus.COMPLETE.value
        else None
    )
    return RunListItemOut(
        id=short_ulid(run.id),
        fullId=run.id,
        name=run.name,
        period=period,
        createdAt=_format_dt(run.created_at, tenant.timezone),
        byInitials=user_initials(user.name),
        byName=user.name,
        status=map_run_status_to_api(run.status),
        eclAmount=float(run.total_ecl) if run.total_ecl is not None else None,
        coverage=coverage,
        currency=tenant.currency,
    )


async def create_run(
    db: AsyncSession,
    tenant_id: str,
    user_id: str,
    req: CreateRunRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> RunDetailOut:
    log.info("run_create_started", tenant_id=tenant_id, user_id=user_id, name=req.name.strip(), engine_version=ENGINE_VERSION)
    await _get_tenant(db, tenant_id)
    user = await _get_user(db, user_id)
    run = Run(
        id=new_ulid(),
        tenant_id=tenant_id,
        created_by_user_id=user_id,
        name=req.name.strip(),
        reporting_period=req.reporting_period.strip() if req.reporting_period else None,
        reporting_date=req.reporting_date,
        status=RunStatus.DRAFT.value,
        engine_version=ENGINE_VERSION,
    )
    db.add(run)
    await db.flush()
    log.info("run_draft_created", run_id=run.id, tenant_id=tenant_id, user_id=user_id, status=RunStatus.DRAFT.value)
    await log_event(
        db,
        AuditEvent.RUN_CREATED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={"run_id": run.id, "description": "Draft initialised"},
    )
    return await get_run(db, tenant_id, run.id)


async def update_run(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    req: UpdateRunRequest,
) -> RunDetailOut:
    run = await _get_run(db, tenant_id, run_id)
    if run.status not in (RunStatus.DRAFT.value, RunStatus.QUEUED.value):
        raise ECLException(
            "RUN_NOT_EDITABLE",
            "Run can only be updated while in draft or queued status.",
            409,
        )
    if req.combine_pd_files is not None and req.combine_pd_files != run.combine_pd_files:
        run.combine_pd_files = req.combine_pd_files
        # LGD and EAD validation read the PD curve built from these files.
        await reset_validation_from(db, run.id, UploadKind.LGD.value)
    if req.name is not None:
        run.name = req.name.strip()
    if req.reporting_period is not None:
        run.reporting_period = req.reporting_period.strip() or None
    if "reporting_date" in req.model_fields_set and run.reporting_date != req.reporting_date:
        run.reporting_date = req.reporting_date
        # The date decides which exposure rows are excluded (spec 1.5).
        await reset_validation_from(db, run.id, UploadKind.EAD.value)
    await db.flush()
    return await get_run(db, tenant_id, run_id)


_VALIDATION_ORDER = (UploadKind.PD.value, UploadKind.LGD.value, UploadKind.EAD.value)


async def reset_validation_from(db: AsyncSession, run_id: str, kind: str) -> None:
    """A changed file invalidates its own validation and every later step's.

    EAD validation reads the PD curve and the collateral file, and LGD reads PD
    loan IDs, so a new PD file must send LGD and EAD back through validation.
    """
    later = _VALIDATION_ORDER[_VALIDATION_ORDER.index(kind):]
    result = await db.execute(select(Upload).where(Upload.run_id == run_id, Upload.kind.in_(later)))
    for upload in result.scalars().all():
        upload.validation_status = None
        upload.warnings_accepted = False


async def upload_file(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    user_id: str,
    *,
    kind: str,
    filename: str,
    file_obj: Any,
    content_type: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UploadOut:
    log.info("upload_file_started", run_id=run_id, tenant_id=tenant_id, kind=kind, filename=filename)
    run = await _get_run(db, tenant_id, run_id)
    if run.status not in (RunStatus.DRAFT.value, RunStatus.FAILED.value):
        raise ECLException(
            "RUN_NOT_UPLOADABLE",
            "Files can only be uploaded to draft or failed runs.",
            409,
        )
    if kind not in {UploadKind.PD.value, UploadKind.LGD.value, UploadKind.EAD.value}:
        raise ECLException("INVALID_UPLOAD_KIND", "kind must be PD, LGD, or EAD.", 400)

    existing = await db.execute(
        select(Upload).where(Upload.run_id == run_id, Upload.kind == kind)
    )
    for old in existing.scalars().all():
        if kind != UploadKind.PD.value:
            log.info("replacing_existing_upload", run_id=run_id, kind=kind, old_filename=old.original_filename)
            await delete_object(old.storage_path)
            await db.delete(old)

    safe_name = filename.replace("/", "_").replace("\\", "_")
    storage_path = build_storage_path(tenant_id, run_id, kind.lower(), safe_name)
    log.info("upload_streaming_to_storage", run_id=run_id, kind=kind, storage_path=storage_path)
    sha256, size = await upload_stream(storage_path, file_obj, content_type)
    max_bytes = get_settings().max_upload_bytes
    if size > max_bytes:
        await delete_object(storage_path)
        raise ECLException(
            "FILE_TOO_LARGE",
            f"File exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB.",
            413,
        )
    log.info("upload_stored", run_id=run_id, kind=kind, size_bytes=size, sha256_prefix=sha256[:8])

    # upload_stream already read the file; read it again from the local spool
    # rather than downloading what was just uploaded.
    file_obj.seek(0)
    content = file_obj.read()
    _assert_upload_safe(content, content_type)
    log.info("upload_parsing_excel", run_id=run_id, kind=kind, filename=filename)
    try:
        # Parsing is CPU-bound; off the event loop so the API keeps answering.
        sheets, sheet_count, row_count = await asyncio.to_thread(_parse_excel, content)
    except Exception as exc:
        await delete_object(storage_path)
        raise ECLException(
            "INVALID_FILE", f"'{filename}' could not be read as an Excel workbook.", 422
        ) from exc
    del sheets
    log.info("upload_parsed", run_id=run_id, kind=kind, sheets=sheet_count, rows=row_count)

    if row_count is not None and row_count > 1_000_000:
        existing_warnings = run.run_warnings or []
        run.run_warnings = existing_warnings + [
            f"EC-12: Upload '{kind}' contains {row_count:,} rows. "
            "Compute time may exceed the 30-minute budget. "
            "Consider splitting the dataset into batches before running."
        ]

    upload = Upload(
        id=new_ulid(),
        run_id=run_id,
        tenant_id=tenant_id,
        kind=kind,
        original_filename=filename,
        sha256=sha256,
        storage_path=storage_path,
        file_size_bytes=size,
        sheet_count=sheet_count,
        row_count=row_count,
    )
    db.add(upload)
    await db.flush()
    await reset_validation_from(db, run_id, kind)
    await log_event(
        db,
        AuditEvent.FILE_UPLOADED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={
            "run_id": run_id,
            "description": f"{filename} {hash_display(sha256)}",
            "kind": kind,
            "filename": filename,
        },
    )
    return UploadOut(
        id=upload.id,
        kind=kind,
        filename=filename,
        size_bytes=size,
        sha256=sha256,
        sheet_count=sheet_count,
        row_count=row_count,
    )


async def delete_upload(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    upload_id: str,
    user_id: str,
) -> None:
    run = await _get_run(db, tenant_id, run_id)
    if run.status not in (RunStatus.DRAFT.value, RunStatus.FAILED.value):
        raise ECLException(
            "RUN_NOT_UPLOADABLE",
            "Files can only be modified on draft or failed runs.",
            409,
        )
    result = await db.execute(
        select(Upload).where(Upload.id == upload_id, Upload.run_id == run_id)
    )
    upload = result.scalar_one_or_none()
    if upload is None:
        raise ECLException("RESOURCE_NOT_FOUND", "Upload not found.", 404)
    await delete_object(upload.storage_path)
    await db.delete(upload)
    await db.flush()
    await reset_validation_from(db, run_id, upload.kind)


async def execute_run(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    user_id: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> ExecuteRunOut:
    log.info("execute_run_started", run_id=run_id, tenant_id=tenant_id, user_id=user_id)
    run = await _get_run_for_update(db, tenant_id, run_id)
    log.info("execute_run_current_status", run_id=run_id, status=run.status, engine_version=run.engine_version)
    if run.status in _ACTIVE_RUN_STATUSES:
        # Re-dispatch only a queue that has sat untouched (a lost dispatch). A
        # second click or a retried request seconds later would otherwise start
        # a parallel pipeline that collides with the first.
        stuck = run.updated_at is not None and datetime.now(UTC) - run.updated_at > _REDISPATCH_AFTER
        if run.status == RunStatus.QUEUED.value and not _compute_has_started(run.engine_progress) and stuck:
            log.info("execute_run_reddispatch_unstarted_queue", run_id=run_id)
            return ExecuteRunOut(run_id=run_id, status=run.status, dispatch_task=True)
        log.info("execute_run_already_active", run_id=run_id, status=run.status)
        return ExecuteRunOut(run_id=run_id, status=run.status, dispatch_task=False)
    if run.status not in (RunStatus.DRAFT.value, RunStatus.FAILED.value):
        raise ECLException("RUN_NOT_EXECUTABLE", "Run is not in an executable state.", 409)

    uploads = await _load_uploads(db, run_id)
    kinds = {u.kind for u in uploads}
    log.info("execute_run_checking_uploads", run_id=run_id, present_kinds=sorted(kinds), upload_count=len(uploads))
    required = {UploadKind.PD.value, UploadKind.LGD.value, UploadKind.EAD.value}
    if not required.issubset(kinds):
        missing = ", ".join(sorted(required - kinds))
        raise ECLException(
            "MISSING_UPLOADS",
            f"Missing required uploads: {missing}.",
            400,
        )

    for upload in uploads:
        if upload.validation_status not in (
            ValidationStatus.OK.value,
            ValidationStatus.WARN.value,
        ):
            raise ECLException(
                "VALIDATION_REQUIRED",
                f"Upload {upload.original_filename} has not passed validation.",
                400,
            )
        if upload.validation_status == ValidationStatus.WARN.value and not upload.warnings_accepted:
            raise ECLException(
                "WARNINGS_NOT_ACCEPTED",
                "Accept validation warnings before executing.",
                400,
            )

    if run.status == RunStatus.FAILED.value:
        await _clear_run_results(db, run_id, tenant_id)

    run.status = RunStatus.QUEUED.value
    run.engine_progress = _default_engine_progress()
    run.failure_stage = None
    run.failure_message = None
    run.failure_ref = None
    run.started_at = None
    run.finished_at = None
    run.total_ecl = None
    run.total_outstanding = None
    run.coverage_ratio = None
    await db.flush()
    log.info(
        "run_status_set_queued",
        run_id=run_id,
        engine_version=run.engine_version,
        uploads=[{"kind": u.kind, "filename": u.original_filename, "rows": u.row_count} for u in uploads],
    )

    await log_event(
        db,
        AuditEvent.RUN_QUEUED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={
            "run_id": run_id,
            "description": f"Engine {run.engine_version}",
        },
    )
    return ExecuteRunOut(run_id=run_id, status="queued", dispatch_task=True)


async def revert_run_after_dispatch_failure(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
) -> None:
    """Reset a run left in queued state when Celery dispatch fails."""
    run = await _get_run_for_update(db, tenant_id, run_id)
    if run.status != RunStatus.QUEUED.value:
        return
    run.status = RunStatus.DRAFT.value
    run.engine_progress = _default_engine_progress()
    run.started_at = None
    await db.flush()
    log.warning("run_reverted_after_dispatch_failure", run_id=run_id, tenant_id=tenant_id)


async def get_run(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    user: User | None = None,
    membership: TenantMembership | None = None,
    view: str = "full",
) -> RunDetailOut:
    tenant = await _get_tenant(db, tenant_id)
    if user is not None:
        run = await assert_run_visible(db, user, tenant_id, membership, run_id)
    else:
        result = await db.execute(
            select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id)
        )
        run = result.scalar_one_or_none()
        if not run:
            raise ECLException("RESOURCE_NOT_FOUND", "Run not found.", 404)

    user = await _get_user(db, run.created_by_user_id)
    base = await _run_list_item(run, user, tenant)

    failure: FailureDetailsOut | None = None
    if run.failure_stage and run.failure_message:
        failure = FailureDetailsOut(
            stage=run.failure_stage,
            message=run.failure_message,
            ref=run.failure_ref or "",
        )

    # Compute-step polling only needs status + engine_progress. The full
    # payload (audit JSON scan, uploads twice, EAD count) is what made the
    # terminal look like a compute loop while the worker never started.
    if view == "progress":
        return RunDetailOut(
            **base.model_dump(),
            reportingDate=run.reporting_date.isoformat() if run.reporting_date else None,
            workbooksStatus=((run.engine_progress or {}).get("workbooks") or {}).get("status"),
            elapsed=_format_elapsed(run.started_at, run.finished_at),
            kpis=_build_kpis(run, None),
            segments=[],
            inputFiles=[],
            auditEvents=[],
            engineInfo=_engine_info(run),
            engineProgress=_engine_progress_out(run.engine_progress),
            failureDetails=failure,
            deletedBy=None,
            deletedAt=None,
            acceptedWarnings=None,
        )

    from app.modules.results.models import EadResult

    loan_count_result = await db.execute(
        select(func.count(func.distinct(EadResult.loan_id))).where(
            EadResult.run_id == run_id,
            EadResult.tenant_id == tenant_id,
        )
    )
    loan_count = loan_count_result.scalar_one_or_none()

    deleted_by_name: str | None = None
    if run.deleted_by_user_id:
        deleter = await _get_user(db, run.deleted_by_user_id)
        deleted_by_name = deleter.name

    accepted_warnings = sum(1 for u in await _load_uploads(db, run_id) if u.warnings_accepted)

    return RunDetailOut(
        **base.model_dump(),
        reportingDate=run.reporting_date.isoformat() if run.reporting_date else None,
        workbooksStatus=((run.engine_progress or {}).get("workbooks") or {}).get("status"),
        elapsed=_format_elapsed(run.started_at, run.finished_at),
        kpis=_build_kpis(run, loan_count),
        segments=await _segment_bars(db, run_id, tenant_id)
        if run.status == RunStatus.COMPLETE.value
        else [],
        inputFiles=await _input_files(db, run_id),
        auditEvents=await _audit_events_for_run(db, run_id, tenant.timezone),
        engineInfo=_engine_info(run),
        engineProgress=_engine_progress_out(run.engine_progress),
        failureDetails=failure,
        deletedBy=deleted_by_name,
        deletedAt=_format_date(run.deleted_at, tenant.timezone) if run.deleted_at else None,
        acceptedWarnings=accepted_warnings or None,
    )


async def list_runs(
    db: AsyncSession,
    tenant_id: str,
    *,
    user: User | None = None,
    membership: TenantMembership | None = None,
    page: int = 1,
    per_page: int = 50,
    status: str | None = None,
    search: str | None = None,
) -> tuple[list[RunListItemOut], PageMeta]:
    tenant = await _get_tenant(db, tenant_id)
    if user is not None:
        scope = get_run_scope_filter(user, tenant_id, membership)
        base = (
            select(Run, User)
            .join(User, User.id == Run.created_by_user_id)
            .where(scope, Run.deleted_at.is_(None))
        )
    else:
        base = (
            select(Run, User)
            .join(User, User.id == Run.created_by_user_id)
            .where(Run.tenant_id == tenant_id, Run.deleted_at.is_(None))
        )

    if status and status != "all":
        status_values: list[str] = []
        for token in status.split(","):
            token = token.strip()
            if token == "running":
                status_values.extend(
                    [
                        RunStatus.PD_RUNNING.value,
                        RunStatus.LGD_RUNNING.value,
                        RunStatus.EAD_RUNNING.value,
                        RunStatus.QUEUED.value,
                    ]
                )
            elif token == "success":
                status_values.append(RunStatus.COMPLETE.value)
            elif token == "draft":
                status_values.append(RunStatus.DRAFT.value)
            elif token == "failed":
                status_values.append(RunStatus.FAILED.value)
            elif token == "queued":
                status_values.append(RunStatus.QUEUED.value)
            elif token == "deleted":
                status_values.append(RunStatus.DELETED.value)
            else:
                status_values.append(token)
        if status_values:
            base = base.where(Run.status.in_(status_values))

    if search:
        pattern = f"%{search.strip()}%"
        base = base.where(
            or_(Run.name.ilike(pattern), Run.reporting_period.ilike(pattern))
        )

    count_q = select(func.count()).select_from(
        base.with_only_columns(Run.id).subquery()
    )
    total = (await db.execute(count_q)).scalar_one()
    offset = (page - 1) * per_page
    result = await db.execute(
        base.order_by(Run.created_at.desc()).offset(offset).limit(per_page)
    )
    pages = max(1, (total + per_page - 1) // per_page)
    meta = PageMeta(
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
        has_next=page < pages,
        has_prev=page > 1,
    )
    items = [
        await _run_list_item(run, user, tenant) for run, user in result.all()
    ]
    return items, meta


async def delete_run(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    user_id: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    run = await _get_run(db, tenant_id, run_id)
    run.deleted_at = datetime.now(UTC)
    run.deleted_by_user_id = user_id
    run.status = RunStatus.DELETED.value
    await db.flush()
    await log_event(
        db,
        AuditEvent.RUN_DELETED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={"run_id": run_id, "description": "Retained for audit"},
    )


async def rerun_run(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    user_id: str,
    req: RerunRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> RunDetailOut:
    """Create a new VALIDATED run seeded with the same input files as a completed run."""
    original = await _get_run(db, tenant_id, run_id)
    if original.status != RunStatus.COMPLETE.value:
        raise ECLException(
            "RUN_NOT_COMPLETED",
            "Only completed runs can be re-run.",
            409,
        )

    tenant = await _get_tenant(db, tenant_id)
    new_name = req.name.strip() if req.name else f"{original.name} (rerun)"

    new_run = Run(
        id=new_ulid(),
        tenant_id=tenant_id,
        created_by_user_id=user_id,
        name=new_name,
        reporting_period=original.reporting_period,
        combine_pd_files=original.combine_pd_files,
        status=RunStatus.DRAFT.value,
        engine_version=ENGINE_VERSION,
    )
    db.add(new_run)
    await db.flush()

    source_uploads = await _load_uploads(db, run_id)
    for src in source_uploads:
        copied = Upload(
            id=new_ulid(),
            run_id=new_run.id,
            tenant_id=tenant_id,
            kind=src.kind,
            original_filename=src.original_filename,
            sha256=src.sha256,
            storage_path=src.storage_path,
            file_size_bytes=src.file_size_bytes,
            sheet_count=src.sheet_count,
            row_count=src.row_count,
            validation_status=ValidationStatus.OK.value,
            warnings_accepted=True,
        )
        db.add(copied)

    new_run.status = RunStatus.DRAFT.value
    await db.flush()

    await log_event(
        db,
        AuditEvent.RUN_RERUN_CREATED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={
            "run_id": new_run.id,
            "source_run_id": run_id,
            "description": f"Re-run of {short_ulid(run_id)}",
        },
    )
    return await get_run(db, tenant_id, new_run.id)
