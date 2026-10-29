"""PD-scoped validation and preview — the `?scope=pd` path.

Async and DB-aware. Loads only the PD uploads, resolves tenant thresholds, hands
everything to the pure engine, and maps the result onto the wire schema.

PD is the first of three validation steps (PD, then LGD, then EAD; see
`ecl_validation`), so it never needs the other two files. Shared download and
parse helpers are imported from `service` rather than duplicated.

Nothing is persisted beyond the PD uploads' validation status and two audit
events. Reproducibility comes from the inputs plus the engine version plus the
thresholds, all of which are recorded — a stronger guarantee than storing a
snapshot of the numbers.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ECLException
from app.core.logging import get_logger
from app.core.run_enums import UploadKind, ValidationStatus
from app.core.security import new_ulid
from app.core.storage import download_bytes
from app.engine import ENGINE_VERSION, PD_METHOD
from app.engine.pd_preview import PdLoanRow, PdPreview, PdSegmentPreview
from app.engine.validators.base import ValidationIssue
from app.engine.validators.pd_criteria import (
    CriterionResult,
    PdSourceSheet,
    PdThresholds,
    PdValidationOutcome,
    classify_sheets,
    evaluate_pd_validation,
)
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import log_event
from app.modules.runs.models import PdValidationThresholds, Upload
from app.modules.runs.schemas import (
    PdCriterionOut,
    PdLoanPreviewOut,
    PdPreviewOut,
    PdSegmentPreviewOut,
    PdSegmentStatsOut,
    PdValidationIssueOut,
    PdValidationResultOut,
)

log = get_logger("runs.pd_validation")

_STATUS_TO_UPLOAD_STATUS = {
    "ok": ValidationStatus.OK.value,
    "warn": ValidationStatus.WARN.value,
    "blocking": ValidationStatus.ERROR.value,
}


async def resolve_pd_thresholds(
    db: AsyncSession, tenant_id: str
) -> tuple[PdThresholds, PdValidationThresholds]:
    """Load this tenant's thresholds, provisioning platform defaults on first use.

    Mirrors `_auto_provision_segments`: a workspace should not have to configure
    anything before it can validate, but the values it validated against must be
    inspectable and editable afterwards.
    """
    defaults = PdThresholds()
    try:
        result = await db.execute(
            select(PdValidationThresholds).where(
                PdValidationThresholds.tenant_id == tenant_id,
                PdValidationThresholds.deleted_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
    except ProgrammingError:
        # Table not migrated yet. Rollback so the rest of validation can run
        # against platform defaults instead of returning a 500.
        await db.rollback()
        log.warning("pd_thresholds_table_missing", tenant_id=tenant_id)
        row = PdValidationThresholds(
            id=new_ulid(),
            tenant_id=tenant_id,
            max_month_gap=defaults.max_month_gap,
            min_transitions=defaults.min_transitions,
            stage3_share_min=Decimal(str(defaults.stage3_share_min)),
            stage3_share_max=Decimal(str(defaults.stage3_share_max)),
            cure_rate_min=Decimal(str(defaults.cure_rate_min)),
            cure_rate_max=Decimal(str(defaults.cure_rate_max)),
            offbooks_abs_band=Decimal(str(defaults.offbooks_abs_band)),
        )
        return (
            defaults,
            row,
        )

    if row is None:
        row = PdValidationThresholds(
            id=new_ulid(),
            tenant_id=tenant_id,
            max_month_gap=defaults.max_month_gap,
            min_transitions=defaults.min_transitions,
            stage3_share_min=Decimal(str(defaults.stage3_share_min)),
            stage3_share_max=Decimal(str(defaults.stage3_share_max)),
            cure_rate_min=Decimal(str(defaults.cure_rate_min)),
            cure_rate_max=Decimal(str(defaults.cure_rate_max)),
            offbooks_abs_band=Decimal(str(defaults.offbooks_abs_band)),
        )
        db.add(row)
        await db.flush()
        log.info("pd_thresholds_provisioned", tenant_id=tenant_id)

    return (
        PdThresholds(
            max_month_gap=int(row.max_month_gap),
            min_transitions=int(row.min_transitions),
            stage3_share_min=float(row.stage3_share_min),
            stage3_share_max=float(row.stage3_share_max),
            cure_rate_min=float(row.cure_rate_min),
            cure_rate_max=float(row.cure_rate_max),
            offbooks_abs_band=float(row.offbooks_abs_band),
        ),
        row,
    )


async def _load_pd_sources(uploads: list[Upload]) -> list[PdSourceSheet]:
    """Download and parse every PD upload, in `created_at` order.

    Sheet order within a file and file order across files decide which row wins
    deduplication downstream, so both are preserved exactly.
    """
    from app.modules.runs.service import _parse_excel

    sources: list[PdSourceSheet] = []
    for upload in uploads:
        content = await download_bytes(upload.storage_path)
        try:
            sheets, _sheet_count, _row_count = _parse_excel(content)
        except Exception as exc:
            raise ECLException(
                "INVALID_FILE",
                f"'{upload.original_filename}' could not be read as an .xlsx workbook.",
                422,
            ) from exc
        for sheet_name, frame in sheets.items():
            sources.append(
                PdSourceSheet(
                    filename=upload.original_filename, sheet_name=sheet_name, frame=frame
                )
            )
    return sources


def _issue_id(issue: ValidationIssue) -> str:
    digest = hashlib.sha256(f"{issue.title}:{issue.location}".encode()).hexdigest()
    return digest[:12]


def _criterion_out(criterion: CriterionResult) -> PdCriterionOut:
    return PdCriterionOut(
        id=criterion.id,
        code=criterion.code,
        name=criterion.name,
        category=criterion.category,
        outcome=criterion.outcome,
        rationale=criterion.rationale,
    )


def _loan_out(row: PdLoanRow) -> PdLoanPreviewOut:
    return PdLoanPreviewOut(
        loan_id=row.loan_id,
        segment=row.segment,
        reporting_month=row.reporting_month,
        reporting_month_key=row.reporting_month_key,
        staging=row.staging,
        next_staging=row.next_staging,
        is_final_month=row.is_final_month,
    )


def _segment_out(segment: PdSegmentPreview) -> PdSegmentPreviewOut:
    return PdSegmentPreviewOut(
        segment=segment.segment,
        matrix=segment.matrix,
        amounts=segment.amounts,
        dest_labels=segment.dest_labels,
        stats=PdSegmentStatsOut(
            cure_rate=segment.stats.cure_rate,
            loans_observed=segment.stats.loans_observed,
            months_of_history=segment.stats.months_of_history,
        ),
        proportions=segment.proportions,
        observed_3=segment.observed_3,
        normalized=segment.normalized,
        term_structure=segment.term_structure,
    )


def _preview_out(preview: PdPreview) -> PdPreviewOut:
    return PdPreviewOut(
        loans=[_loan_out(row) for row in preview.loans],
        segments=[_segment_out(segment) for segment in preview.segments],
        engine_version=preview.engine_version,
    )


def _result_out(outcome: PdValidationOutcome) -> PdValidationResultOut:
    return PdValidationResultOut(
        status=outcome.status,
        criteria=[_criterion_out(c) for c in outcome.criteria],
        pd_preview=_preview_out(outcome.preview),
        issues=[
            PdValidationIssueOut(
                id=_issue_id(issue),
                level=issue.level,
                title=issue.title,
                location=issue.location,
                fix=issue.fix,
                category=issue.category,
            )
            for issue in outcome.issues
        ],
    )


def _prescan_segments(sources: list[PdSourceSheet]) -> set[str]:
    data_sheets, _malformed, _skipped = classify_sheets(sources)
    found: set[str] = set()
    for source in data_sheets:
        values = source.frame["SEGMENT"].dropna().astype(str).str.strip()
        found.update(value for value in values if value)
    return found


async def validate_pd_files(
    db: AsyncSession,
    tenant_id: str,
    run_id: str,
    user_id: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> PdValidationResultOut:
    """Validate the run's PD uploads on their own and return the preview.

    Always returns a fully populated payload with a 200 when the PD data was
    evaluated at all, including when it blocks. The client treats a 4xx as
    "endpoint not ready" and substitutes sample data, so a 4xx for a real
    rejection would show the user a fabricated pass. Errors are reserved for
    cases where there was nothing to evaluate: no PD file, an unreadable
    workbook, or a run the caller cannot see.
    """
    from app.modules.runs.service import _allowed_segments, _auto_provision_segments, _get_run

    run = await _get_run(db, tenant_id, run_id)

    result = await db.execute(
        select(Upload)
        .where(Upload.run_id == run_id, Upload.kind == UploadKind.PD.value)
        .order_by(Upload.created_at)
    )
    pd_uploads = list(result.scalars().all())
    if not pd_uploads:
        raise ECLException(
            "NO_PD_UPLOADS",
            "Upload at least one PD file before validating PD.",
            400,
        )

    log.info(
        "pd_validate_started",
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        file_count=len(pd_uploads),
        filenames=[u.original_filename for u in pd_uploads],
    )
    await log_event(
        db,
        AuditEvent.VALIDATION_TRIGGERED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={"run_id": run_id, "description": "PD validation started", "scope": "pd"},
    )

    sources = await _load_pd_sources(pd_uploads)

    # Match the unscoped validate path: segments seen in the data are provisioned
    # so PD-07 does not block a first-time workspace on its own vocabulary.
    allowed_segments = await _allowed_segments(db, tenant_id)
    new_segments = _prescan_segments(sources) - allowed_segments
    if new_segments:
        try:
            await _auto_provision_segments(db, tenant_id, user_id, new_segments)
            allowed_segments |= new_segments
            log.info(
                "pd_validate_segments_provisioned",
                run_id=run_id,
                new_segments=sorted(new_segments),
            )
        except Exception as exc:
            log.warning("pd_validate_provision_failed", tenant_id=tenant_id, error=str(exc))

    thresholds, _row = await resolve_pd_thresholds(db, tenant_id)

    outcome = evaluate_pd_validation(
        sources,
        allowed_segments=allowed_segments,
        thresholds=thresholds,
        engine_version=ENGINE_VERSION,
        engine_method=PD_METHOD,
        file_count=len(pd_uploads),
        # PD-scoped validate always previews every uploaded PD file. The
        # combine flag is a compute-time choice; EC-18 states the divergence
        # rather than quietly dropping files from what the reviewer inspects.
        combine_pd_files=run.combine_pd_files,
    )

    upload_status = _STATUS_TO_UPLOAD_STATUS[outcome.status]
    for upload in pd_uploads:
        upload.validation_status = upload_status

    counts = {
        key: sum(1 for c in outcome.criteria if c.outcome == key)
        for key in ("pass", "review", "block", "info")
    }
    log.info(
        "pd_validate_complete",
        run_id=run_id,
        status=outcome.status,
        criteria=counts,
        issue_count=len(outcome.issues),
        issues_truncated=outcome.issues_truncated,
        loan_rows=len(outcome.preview.loans),
        segments=[s.segment for s in outcome.preview.segments],
        data_sheets=outcome.data_sheet_count,
        skipped_sheets=outcome.skipped_sheet_count,
        rows=outcome.row_count,
    )
    await log_event(
        db,
        AuditEvent.VALIDATION_COMPLETED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={
            "run_id": run_id,
            "description": (
                f"PD validation {outcome.status} · {counts['pass']} passed, "
                f"{counts['review']} to review, {counts['block']} blocking"
            ),
            "scope": "pd",
            "status": outcome.status,
            "engine_version": ENGINE_VERSION,
            "criteria_counts": counts,
        },
    )
    await db.flush()
    return _result_out(outcome)


__all__ = ["resolve_pd_thresholds", "validate_pd_files"]


async def build_pd_export(db: AsyncSession, tenant_id: str, run_id: str) -> tuple[bytes, str]:
    """PD Calcs.xlsx from the run's PD uploads alone — no EAD or LGD needed.

    Mirrors compute's PD input exactly (upload order, combine flag, PD-sheet
    predicate) and runs the same compute_pd + workbook writer, so this file
    matches the PD Calcs.xlsx a full run would produce.
    """
    import asyncio
    from datetime import UTC, datetime

    from app.engine.excel_writer import _build_pd_calcs_workbook
    from app.engine.pd_engine import compute_pd, is_pd_data_sheet
    from app.engine.workbook import frames_from_workbooks
    from app.modules.runs.service import _get_run

    run = await _get_run(db, tenant_id, run_id)
    result = await db.execute(
        select(Upload)
        .where(Upload.run_id == run_id, Upload.kind == UploadKind.PD.value)
        .order_by(Upload.created_at)
    )
    uploads = list(result.scalars().all())
    if not uploads:
        raise ECLException("NO_PD_UPLOADS", "Upload at least one PD file before exporting PD.", 400)
    if not run.combine_pd_files:
        uploads = uploads[:1]

    files = [await download_bytes(u.storage_path) for u in uploads]

    def _build() -> bytes:
        pd_input = frames_from_workbooks(files, keep_sheet=is_pd_data_sheet)
        pd_df, _cure, intermediates = compute_pd(pd_input)
        run_meta = {
            "run_id": run.id,
            "tenant_id": run.tenant_id,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "engine_version": run.engine_version or ENGINE_VERSION,
            "input_hashes": ",".join(hashlib.sha256(f).hexdigest()[:12] for f in files),
        }
        return _build_pd_calcs_workbook(run_meta, {"pd_df": pd_df, "intermediates": intermediates})

    content = await asyncio.to_thread(_build)
    log.info("pd_export_built", run_id=run_id, tenant_id=tenant_id, bytes=len(content))
    return content, "PD Calcs.xlsx"
