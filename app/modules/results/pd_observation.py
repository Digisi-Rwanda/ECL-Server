"""Persist and load the observed PD preview that validation and results share."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import new_ulid
from app.engine.pd_preview import PdPreview
from app.modules.results.models import PdObservationSnapshot
from app.modules.runs.pd_validation import _preview_out
from app.modules.runs.schemas import PdLoanPreviewOut, PdPreviewOut, PdSegmentPreviewOut


def preview_to_payload(preview: PdPreview) -> dict:
    return _preview_out(preview).model_dump()


async def persist_pd_observation(
    db: AsyncSession,
    *,
    run_id: str,
    tenant_id: str,
    preview: PdPreview,
) -> PdObservationSnapshot:
    return await persist_pd_observation_payload(
        db,
        run_id=run_id,
        tenant_id=tenant_id,
        engine_version=preview.engine_version,
        payload=preview_to_payload(preview),
    )


async def persist_pd_observation_payload(
    db: AsyncSession,
    *,
    run_id: str,
    tenant_id: str,
    engine_version: str,
    payload: dict,
) -> PdObservationSnapshot:
    try:
        await db.execute(delete(PdObservationSnapshot).where(PdObservationSnapshot.run_id == run_id))
    except ProgrammingError:
        await db.rollback()
        raise
    row = PdObservationSnapshot(
        id=new_ulid(),
        run_id=run_id,
        tenant_id=tenant_id,
        engine_version=engine_version,
        payload=payload,
    )
    db.add(row)
    await db.flush()
    return row


async def load_pd_observation(db: AsyncSession, run_id: str) -> PdPreviewOut | None:
    try:
        result = await db.execute(
            select(PdObservationSnapshot).where(PdObservationSnapshot.run_id == run_id)
        )
        row = result.scalar_one_or_none()
    except ProgrammingError:
        await db.rollback()
        return None
    if row is None:
        return None
    return PdPreviewOut.model_validate(row.payload)


async def rebuild_pd_observation(
    db: AsyncSession,
    *,
    tenant_id: str,
    run_id: str,
    combine_pd_files: bool,
) -> PdPreviewOut | None:
    """Rebuild the observed preview from the stored PD workbooks.

    Used for runs computed before snapshots existed. Same `compute_pd` path
    as validation, so the numbers still match the live engine.
    """
    from app.core.run_enums import UploadKind
    from app.engine.pd_engine import compute_pd
    from app.engine.validators.pd_criteria import (
        classify_sheets,
        combine_pd_sheets,
        strip_provenance,
    )
    from app.modules.runs.models import Upload
    from app.modules.runs.pd_validation import _load_pd_sources

    result = await db.execute(
        select(Upload)
        .where(Upload.run_id == run_id, Upload.kind == UploadKind.PD.value)
        .order_by(Upload.created_at)
    )
    uploads = list(result.scalars().all())
    if not uploads:
        return None
    if not combine_pd_files:
        uploads = uploads[:1]
    try:
        sources = await _load_pd_sources(uploads)
        data_sheets, _malformed, _skipped = classify_sheets(sources)
        if not data_sheets:
            return None
        engine_frame = strip_provenance(combine_pd_sheets(data_sheets))
        _output, _cure_rates, intermediates = compute_pd(engine_frame)
        preview = intermediates.get("preview")
    except Exception:
        return None
    if preview is None:
        return None
    return _preview_out(preview)


def segment_from_preview(
    preview: PdPreviewOut, segment_name: str
) -> tuple[PdSegmentPreviewOut | None, list[PdLoanPreviewOut]]:
    segment = next((s for s in preview.segments if s.segment == segment_name), None)
    loans = [row for row in preview.loans if row.segment == segment_name]
    return segment, loans
