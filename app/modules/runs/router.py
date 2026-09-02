import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, Response

from app.core.exceptions import ECLException
from app.core.limiter import limiter
from app.core.logging import get_logger
from app.dependencies import (
    CurrentUser,
    DbSession,
    get_client_ip,
    get_user_agent,
    require_tenant_admin,
    require_tenant_analyst_or_admin,
    require_tenant_member,
)
from app.modules.results import service as results_service
from app.modules.runs import pd_validation, service
from app.modules.runs.schemas import (
    CreateRunRequest,
    ExecuteRunResponse,
    PdValidationResponse,
    PresignedDownloadResponse,
    RerunRequest,
    RunListResponse,
    RunResponse,
    UpdateRunRequest,
    UploadResponse,
    ValidateRunRequest,
    ValidationResponse,
)
from app.modules.tenants.models import TenantMembership
from app.tasks.compute_tasks import (
    celery_workers_available,
    lgd_task,
    pd_task,
    schedule_inline_compute,
)

_log = get_logger("runs.router")

router = APIRouter(prefix="/tenants", tags=["runs"])

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "static" / "templates"


@router.post("/{tenant_id}/runs", status_code=status.HTTP_201_CREATED)
async def create_run_endpoint(
    tenant_id: str,
    body: CreateRunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> RunResponse:
    _log.info("run_create_requested", tenant_id=tenant_id, user_id=user.id, name=body.name)
    data = await service.create_run(
        db,
        tenant_id,
        user.id,
        body,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    _log.info("run_created", run_id=data.fullId, tenant_id=tenant_id, user_id=user.id)
    return RunResponse(data=data)


@router.patch("/{tenant_id}/runs/{run_id}")
async def update_run_endpoint(
    tenant_id: str,
    run_id: str,
    body: UpdateRunRequest,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> RunResponse:
    data = await service.update_run(db, tenant_id, run_id, body)
    return RunResponse(data=data)


@router.post("/{tenant_id}/runs/{run_id}/uploads", status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def upload_file_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    kind: str = Form(...),
    file: UploadFile = File(...),
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> UploadResponse:
    _log.info(
        "file_upload_received",
        run_id=run_id,
        tenant_id=tenant_id,
        kind=kind.upper(),
        filename=file.filename,
        content_type=file.content_type,
    )
    data = await service.upload_file(
        db,
        tenant_id,
        run_id,
        user.id,
        kind=kind.upper(),
        filename=file.filename or "upload.xlsx",
        file_obj=file.file,
        content_type=file.content_type or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    _log.info(
        "file_upload_complete",
        run_id=run_id,
        kind=kind.upper(),
        filename=data.filename,
        size_bytes=data.size_bytes,
        sha256_prefix=data.sha256[:8],
        sheets=data.sheet_count,
        rows=data.row_count,
    )
    return UploadResponse(data=data)


@router.delete("/{tenant_id}/runs/{run_id}/uploads/{upload_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_upload_endpoint(
    tenant_id: str,
    run_id: str,
    upload_id: str,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> None:
    await service.delete_upload(db, tenant_id, run_id, upload_id, user.id)


@router.post("/{tenant_id}/runs/{run_id}/validate")
@limiter.limit("30/minute")
async def validate_files_endpoint(
    request: Request,
    tenant_id: str,
    run_id: str,
    body: ValidateRunRequest,
    db: DbSession,
    user: CurrentUser,
    scope: str | None = Query(default=None),
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> ValidationResponse | PdValidationResponse:
    # ?scope=pd validates the PD uploads alone and returns the criteria
    # checklist plus preview. Unscoped keeps the original all-three-files
    # behaviour byte for byte.
    if scope and scope.strip().lower() == "pd":
        _log.info("pd_validation_requested", run_id=run_id, tenant_id=tenant_id, user_id=user.id)
        pd_data = await pd_validation.validate_pd_files(
            db,
            tenant_id,
            run_id,
            user.id,
            ip=get_client_ip(request.headers.get("X-Forwarded-For")),
            user_agent=get_user_agent(request.headers.get("User-Agent")),
        )
        _log.info(
            "pd_validation_complete",
            run_id=run_id,
            status=pd_data.status,
            criteria_count=len(pd_data.criteria),
            loan_rows=len(pd_data.pd_preview.loans),
        )
        return PdValidationResponse(data=pd_data)

    _log.info(
        "validation_requested",
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user.id,
        accepted_warning_count=len(body.accepted_warning_ids),
    )
    data = await service.validate_files(
        db,
        tenant_id,
        run_id,
        user.id,
        body,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    _log.info(
        "validation_complete",
        run_id=run_id,
        status=data.status,
        issue_count=len(data.issues),
        detected_segments=data.detected_segments,
    )
    return ValidationResponse(data=data)


@router.post(
    "/{tenant_id}/runs/{run_id}/execute",
    status_code=status.HTTP_202_ACCEPTED,
)
async def execute_run_endpoint(
    tenant_id: str,
    run_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> ExecuteRunResponse:
    _log.info("execute_run_requested", run_id=run_id, tenant_id=tenant_id, user_id=user.id)
    data = await service.execute_run(
        db,
        tenant_id,
        run_id,
        user.id,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    await db.commit()
    if data.dispatch_task:
        workers = await celery_workers_available()
        if workers:
            _log.info("run_queued_dispatching_to_celery", run_id=run_id)
            try:
                await asyncio.gather(
                    asyncio.to_thread(lambda: pd_task.delay(run_id)),
                    asyncio.to_thread(lambda: lgd_task.delay(run_id)),
                )
                _log.info(
                    "compute_tasks_dispatched",
                    run_id=run_id,
                    tasks=["pd_task", "lgd_task"],
                    broker="redis",
                )
            except Exception as exc:
                _log.warning(
                    "compute_dispatch_failed_falling_back_inline",
                    run_id=run_id,
                    exc_info=exc,
                )
                schedule_inline_compute(run_id)
        else:
            _log.info("no_celery_workers_running_inline", run_id=run_id)
            schedule_inline_compute(run_id)
    else:
        _log.info("execute_run_idempotent_skip_dispatch", run_id=run_id, status=data.status)
    return ExecuteRunResponse(data=data)


@router.get("/{tenant_id}/runs/{run_id}")
async def get_run_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
) -> RunResponse:
    data = await service.get_run(db, tenant_id, run_id, user=user, membership=membership)
    return RunResponse(data=data)


@router.get("/{tenant_id}/runs")
async def list_runs_endpoint(
    tenant_id: str,
    db: DbSession,
    user: CurrentUser,
    membership: TenantMembership = Depends(require_tenant_member),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> RunListResponse:
    items, meta = await service.list_runs(
        db,
        tenant_id,
        user=user,
        membership=membership,
        page=page,
        per_page=per_page,
        status=status,
        search=search,
    )
    return RunListResponse(data=items, meta=meta.model_dump())


@router.post("/{tenant_id}/runs/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
async def rerun_run_endpoint(
    tenant_id: str,
    run_id: str,
    body: RerunRequest,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_analyst_or_admin),
) -> RunResponse:
    data = await service.rerun_run(
        db,
        tenant_id,
        run_id,
        user.id,
        body,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )
    return RunResponse(data=data)


@router.delete("/{tenant_id}/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run_endpoint(
    tenant_id: str,
    run_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    _a: TenantMembership = Depends(require_tenant_admin),
) -> None:
    await service.delete_run(
        db,
        tenant_id,
        run_id,
        user.id,
        ip=get_client_ip(request.headers.get("X-Forwarded-For")),
        user_agent=get_user_agent(request.headers.get("User-Agent")),
    )


@router.get("/{tenant_id}/runs/{run_id}/downloads/bundle")
async def download_workbooks_bundle_endpoint(
    tenant_id: str,
    run_id: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await results_service.build_workbooks_bundle(db, tenant_id, run_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tenant_id}/runs/{run_id}/downloads/{kind}/file")
async def download_artifact_file_endpoint(
    tenant_id: str,
    run_id: str,
    kind: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> Response:
    content, filename = await results_service.get_artifact_bytes(
        db, tenant_id, run_id, kind.upper()
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{tenant_id}/runs/{run_id}/downloads/{kind}")
async def download_artifact_endpoint(
    tenant_id: str,
    run_id: str,
    kind: str,
    db: DbSession,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> PresignedDownloadResponse:
    url, expires_at = await results_service.get_presigned_download(
        db, tenant_id, run_id, kind.upper()
    )
    from app.modules.runs.schemas import PresignedDownloadOut

    return PresignedDownloadResponse(
        data=PresignedDownloadOut(url=url, expires_at=expires_at)
    )


@router.get("/{tenant_id}/templates/{kind}")
async def get_template_endpoint(
    tenant_id: str,
    kind: str,
    user: CurrentUser,
    _m: TenantMembership = Depends(require_tenant_member),
) -> FileResponse:
    kind_upper = kind.upper()
    if kind_upper not in {"PD", "LGD", "EAD"}:
        from app.core.exceptions import ECLException

        raise ECLException("INVALID_TEMPLATE_KIND", "Template kind must be PD, LGD, or EAD.", 400)
    path = _TEMPLATES_DIR / f"{kind_upper}.xlsx"
    if not path.is_file():
        from app.core.exceptions import ECLException

        raise ECLException("TEMPLATE_NOT_FOUND", f"Template {kind_upper} not found.", 404)
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"ECL_{kind_upper}_template.xlsx",
    )
