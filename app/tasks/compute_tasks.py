"""Celery compute pipeline: PD, LGD, and EAD/ECL tasks."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import io
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.logging import get_logger
from app.tasks.celery_app import celery_app

_log = get_logger(__name__)

STAGE_KEYS = ("pd", "lgd", "ead", "ecl")

# When True, PD/LGD skip Celery EAD dispatch — the inline pipeline runs EAD itself.
_INLINE_COMPUTE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "inline_compute", default=False
)
_inline_jobs: set[asyncio.Task[None]] = set()


def _run(coro):  # type: ignore[no-untyped-def]
    """Run an async coroutine from a synchronous Celery task."""
    return asyncio.run(coro)


@asynccontextmanager
async def _fresh_session():
    """
    Fresh engine + session per task using NullPool.

    asyncio.run() creates a new event loop each invocation; the shared engine
    holds asyncpg connections bound to the previous loop, causing
    'Future attached to a different loop'.  NullPool means no connection is
    reused across asyncio.run() calls.
    """
    from app.database import _connect_args, _db_url

    task_engine = create_async_engine(_db_url, poolclass=NullPool, connect_args=_connect_args)
    maker = async_sessionmaker(task_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as session:
            yield session, task_engine
    finally:
        await task_engine.dispose()


async def _prepare_task() -> None:
    """Reset aiobotocore singleton so it binds to this task's event loop.

    The storage client singleton is created once per process.  Each call to
    asyncio.run() creates a fresh event loop, so any existing aiohttp/asyncpg
    connections are invalid.  Closing and reinitialising before storage I/O
    ensures the client belongs to the current loop.
    """
    from app.core.storage import close_storage, init_storage

    await close_storage()
    await init_storage()


def _initial_engine_progress() -> dict[str, dict[str, Any]]:
    return {
        stage: {
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "elapsed_ms": None,
        }
        for stage in STAGE_KEYS
    }


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


async def _load_run(db, run_id: str):
    from app.modules.runs.models import Run

    result = await db.execute(select(Run).where(Run.id == run_id, Run.deleted_at.is_(None)))
    run = result.scalar_one_or_none()
    if run is None:
        msg = f"Run {run_id} not found"
        raise ValueError(msg)
    return run


import json as _json


async def _atomic_stage_update(
    db,
    run_id: str,
    stage: str,
    stage_data: dict[str, Any],
    **run_values: Any,
) -> None:
    """Atomically merge one stage key into engine_progress using jsonb_set.

    Using jsonb_set instead of read-modify-write prevents the race condition
    where concurrent PD and LGD stages (each in their own DB session) overwrite
    each other's status update.
    """
    set_clauses = [
        "engine_progress = jsonb_set("
        "COALESCE(engine_progress::jsonb, '{}'::jsonb), "
        f"'{{{stage}}}', "
        "CAST(:stage_data AS jsonb)"
        ")::json"
    ]
    params: dict[str, Any] = {"run_id": run_id, "stage_data": _json.dumps(stage_data)}
    for col, val in run_values.items():
        if isinstance(val, (list, dict)):
            set_clauses.append(f"{col} = CAST(:{col} AS json)")
            params[col] = _json.dumps(val)
        else:
            set_clauses.append(f"{col} = :{col}")
            params[col] = val

    sql = f"UPDATE runs SET {', '.join(set_clauses)} WHERE id = :run_id"
    await db.execute(text(sql), params)
    await db.commit()


async def _get_engine_progress(db, run_id: str) -> dict[str, Any]:
    """Read the current engine_progress for display/logging only (not for atomic merge)."""
    from app.modules.runs.models import Run

    result = await db.execute(select(Run.engine_progress).where(Run.id == run_id))
    current = result.scalar_one_or_none()
    if not current:
        return _initial_engine_progress()
    merged = _initial_engine_progress()
    merged.update(current)
    for stage in STAGE_KEYS:
        merged[stage] = {**_initial_engine_progress()[stage], **merged.get(stage, {})}
    return merged


async def _mark_stage_running(db, run_id: str, stage: str, run_status: str) -> str:
    started_at = _iso_now()
    stage_data = {"status": "running", "started_at": started_at, "finished_at": None, "elapsed_ms": None}
    await _atomic_stage_update(db, run_id, stage, stage_data, status=run_status)
    return started_at


async def _mark_stage_complete(db, run_id: str, stage: str, started_at: str) -> dict[str, Any]:
    finished_at = _iso_now()
    elapsed_ms = int((datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds() * 1000)
    stage_data = {"status": "complete", "started_at": started_at, "finished_at": finished_at, "elapsed_ms": elapsed_ms}
    await _atomic_stage_update(db, run_id, stage, stage_data)
    return await _get_engine_progress(db, run_id)


async def _mark_run_failed(db, run_id: str, stage: str, exc: Exception) -> None:
    failure_ref = str(uuid4())
    stage_data = {"status": "error", "started_at": None, "finished_at": _iso_now(), "elapsed_ms": None}
    await _atomic_stage_update(
        db,
        run_id,
        stage,
        stage_data,
        status="failed",
        failure_stage=stage,
        failure_message=str(exc)[:2000],
        failure_ref=failure_ref,
        finished_at=datetime.now(UTC),
    )
    from app.modules.runs.models import Run
    from app.modules.notifications.service import notify_run_failed
    from app.modules.audit.models import AuditEvent
    from app.modules.audit.service import log_event

    result = await db.execute(select(Run).where(Run.id == run_id))
    run = result.scalar_one_or_none()
    if run and run.created_by_user_id:
        await notify_run_failed(
            db,
            user_id=run.created_by_user_id,
            tenant_id=run.tenant_id,
            run_name=run.name,
            run_id=run.id,
        )
        await log_event(
            db,
            AuditEvent.RUN_FAILED,
            user_id=run.created_by_user_id,
            tenant_id=run.tenant_id,
            details={"run_id": run.id, "stage": stage, "message": str(exc)[:500]},
        )


async def _mark_run_failed_standalone(run_id: str, stage: str, exc: Exception) -> None:
    """Mark run as failed without requiring an existing DB session.

    Used when a task fails before _fresh_session() succeeds, so the
    normal _mark_run_failed inside _pd_main etc. can never execute.
    If this also fails (e.g. DB is down), recover_stuck_runs handles it.
    """
    try:
        async with _fresh_session() as (db, _):
            await _mark_run_failed(db, run_id, stage, exc)
    except Exception as db_exc:
        _log.error(
            "mark_run_failed_standalone_error",
            run_id=run_id,
            stage=stage,
            exc_info=db_exc,
        )


async def _load_uploads(db, run_id: str, kind: str):
    from app.modules.runs.models import Upload

    result = await db.execute(
        select(Upload)
        .where(Upload.run_id == run_id, Upload.kind == kind)
        .order_by(Upload.created_at.asc())
    )
    return list(result.scalars().all())


async def _download_uploads(uploads) -> list[bytes]:
    from app.core.storage import download_bytes

    return [await download_bytes(upload.storage_path) for upload in uploads]


def _combine_excel_bytes(
    files: list[bytes],
    keep_sheet: Callable[[pd.DataFrame], bool] | None = None,
) -> pd.DataFrame:
    """Concatenate kept sheets. Delegates to the shared workbook reader."""
    from app.engine.workbook import frames_from_workbooks

    return frames_from_workbooks(files, keep_sheet=keep_sheet)


async def _input_hashes(db, run_id: str) -> str:
    from app.modules.runs.models import Upload

    result = await db.execute(
        select(Upload.sha256).where(Upload.run_id == run_id).order_by(Upload.kind.asc(), Upload.created_at.asc())
    )
    return ",".join(result.scalars().all())


async def _load_collateral_config(db, tenant_id: str) -> list[dict[str, Any]]:
    from app.modules.runs.ecl_validation import collateral_parameters

    return await collateral_parameters(db, tenant_id)


async def _prepare_ecl_inputs(db, run):
    """Download EAD + LGD, apply the spec input rules. Shared by the LGD and ECL stages."""
    from app.core.run_enums import UploadKind
    from app.engine.ecl_rules import is_collateral_sheet, is_exposure_sheet, prepare_inputs

    ead_uploads = await _load_uploads(db, run.id, UploadKind.EAD.value)
    lgd_uploads = await _load_uploads(db, run.id, UploadKind.LGD.value)
    if not ead_uploads:
        raise ValueError("No EAD uploads found for run")
    if not lgd_uploads:
        raise ValueError("No LGD uploads found for run")
    exposure = _combine_excel_bytes(await _download_uploads(ead_uploads), keep_sheet=is_exposure_sheet)
    collateral = _combine_excel_bytes(await _download_uploads(lgd_uploads), keep_sheet=is_collateral_sheet)
    config = await _load_collateral_config(db, run.tenant_id)
    prepared = prepare_inputs(exposure, collateral, config, run.reporting_date)
    return prepared, ead_uploads, lgd_uploads, config


async def _upload_workbooks(
    db,
    run,
    workbooks: dict[str, bytes],
    artifact_kinds: dict[str, str],
) -> None:
    from app.core.security import new_ulid
    from app.core.storage import build_storage_path, upload_stream
    from app.modules.results.models import OutputArtifact

    for filename, content in workbooks.items():
        storage_path = build_storage_path(run.tenant_id, run.id, "outputs", filename)
        sha256, file_size = await upload_stream(
            storage_path,
            io.BytesIO(content),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        db.add(
            OutputArtifact(
                id=new_ulid(),
                run_id=run.id,
                tenant_id=run.tenant_id,
                kind=artifact_kinds[filename],
                storage_path=storage_path,
                sha256=sha256,
                file_size_bytes=file_size,
            )
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Standalone async stage functions (run in the calling event loop)
# ---------------------------------------------------------------------------


async def _is_stage_complete(db, run_id: str, stage: str) -> bool:
    progress = await _get_engine_progress(db, run_id)
    return progress.get(stage, {}).get("status") == "complete"


async def _try_dispatch_ead(db, run_id: str) -> bool:
    """Dispatch ead_ecl_task if and only if both PD and LGD stages are complete.

    Uses a single atomic SQL UPDATE to claim the dispatch slot — only the first
    caller wins. This replaces the Celery chord Redis counter, which is lost
    when the worker process is killed mid-task.

    Returns True if ead_ecl_task was dispatched, False if the other stage is not
    yet complete or ead was already dispatched by the parallel task.
    """
    if _INLINE_COMPUTE.get():
        return False

    if not (await _is_stage_complete(db, run_id, "pd") and await _is_stage_complete(db, run_id, "lgd")):
        return False

    # Atomically set ead.status = 'queued' only if it is still 'pending'.
    # The UPDATE affects exactly one row when this caller wins the race;
    # zero rows when the parallel task already claimed the slot.
    result = await db.execute(
        text(
            """
            UPDATE runs
            SET engine_progress = jsonb_set(
                COALESCE(engine_progress::jsonb, '{}'::jsonb),
                '{ead}',
                '{"status": "queued", "started_at": null, "finished_at": null, "elapsed_ms": null}'::jsonb
            )::json
            WHERE id = :run_id
              AND (
                engine_progress IS NULL
                OR engine_progress::jsonb -> 'ead' IS NULL
                OR engine_progress::jsonb -> 'ead' ->> 'status' = 'pending'
              )
            """
        ),
        {"run_id": run_id},
    )
    await db.commit()

    if result.rowcount == 0:
        return False  # parallel task already dispatched ead_ecl_task

    ead_ecl_task.delay(run_id)
    _log.info("ead_ecl_task_dispatched", run_id=run_id)
    return True


async def _pd_main(run_id: str) -> dict[str, str]:
    """PD stage — runs in the calling event loop."""
    from app.core.run_enums import RunStatus, UploadKind
    from app.engine.bulk_insert import bulk_insert_pd_results
    from app.engine.pd_engine import compute_pd, is_pd_data_sheet

    async with _fresh_session() as (db, task_engine):
        run = await _load_run(db, run_id)
        if await _is_stage_complete(db, run.id, "pd"):
            _log.info("pd_stage_already_complete", run_id=run_id)
            return {"run_id": run_id, "stage": "pd"}
        started_at = await _mark_stage_running(db, run.id, "pd", RunStatus.PD_RUNNING.value)
        _log.info("compute_stage_started", run_id=run_id, stage="pd", pct=0)

        try:
            _log.info("pd_loading_uploads", run_id=run_id, pct=5)
            uploads = await _load_uploads(db, run_id, UploadKind.PD.value)
            if not uploads:
                msg = "No PD uploads found for run"
                raise ValueError(msg)
            _log.info("pd_uploads_found", run_id=run_id, file_count=len(uploads), filenames=[u.original_filename for u in uploads], pct=10)

            _log.info("pd_downloading_files", run_id=run_id, pct=15)
            files = await _download_uploads(uploads)
            if not run.combine_pd_files and len(files) > 1:
                files = files[:1]
                _log.info("pd_using_first_file_only", run_id=run_id, combine_pd_files=False)
            total_bytes = sum(len(f) for f in files)
            _log.info("pd_files_downloaded", run_id=run_id, file_count=len(files), total_bytes=total_bytes, pct=25)

            _log.info("pd_combining_sheets", run_id=run_id, pct=30)
            pd_input = _combine_excel_bytes(files, keep_sheet=is_pd_data_sheet)
            del files
            gc.collect()
            _log.info("pd_data_combined", run_id=run_id, row_count=len(pd_input), columns=list(pd_input.columns), pct=35)

            _log.info("pd_computing_transition_matrices", run_id=run_id, pct=40)
            pd_output, _cure_rates, _intermediates = compute_pd(pd_input)
            from app.modules.results.pd_observation import persist_pd_observation

            preview = _intermediates.get("preview")
            if preview is not None:
                await persist_pd_observation(
                    db, run_id=run.id, tenant_id=run.tenant_id, preview=preview
                )
            segments = sorted(pd_output["SEGMENT"].unique().tolist()) if "SEGMENT" in pd_output.columns else []
            _log.info(
                "pd_matrices_computed",
                run_id=run_id,
                output_rows=len(pd_output),
                segments=segments,
                segment_count=len(segments),
                cure_rates={k: round(float(v), 6) for k, v in _cure_rates.items()} if _cure_rates else {},
                pct=80,
            )

            _log.info("pd_inserting_results", run_id=run_id, rows=len(pd_output), pct=85)
            async with task_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM pd_results WHERE run_id = :run_id"),
                    {"run_id": run.id},
                )
                await bulk_insert_pd_results(conn, run.id, run.tenant_id, pd_output)
            _log.info("pd_results_inserted", run_id=run_id, rows=len(pd_output), pct=95)

            progress = await _mark_stage_complete(db, run.id, "pd", started_at)
            _log.info(
                "compute_stage_completed",
                run_id=run_id,
                stage="pd",
                elapsed_ms=progress.get("pd", {}).get("elapsed_ms"),
                pct=100,
            )
            await _try_dispatch_ead(db, run_id)
            return {"run_id": run_id, "stage": "pd"}
        except Exception as exc:
            _log.error("compute_stage_failed", run_id=run_id, stage="pd", exc_info=exc)
            await _mark_run_failed(db, run.id, "pd", exc)
            raise


async def _lgd_main(run_id: str) -> dict[str, str]:
    """LGD stage — runs in the calling event loop."""
    from app.core.run_enums import RunStatus, UploadKind
    from app.engine.bulk_insert import bulk_insert_lgd_results
    from app.engine.lgd_engine import compute_lgd

    async with _fresh_session() as (db, task_engine):
        run = await _load_run(db, run_id)
        if await _is_stage_complete(db, run.id, "lgd"):
            _log.info("lgd_stage_already_complete", run_id=run_id)
            return {"run_id": run_id, "stage": "lgd"}
        started_at = await _mark_stage_running(db, run.id, "lgd", RunStatus.LGD_RUNNING.value)
        _log.info("compute_stage_started", run_id=run_id, stage="lgd", pct=0)

        try:
            _log.info("lgd_loading_inputs", run_id=run_id, pct=5)
            # Step 1 needs Total Customer Exposure from the Exposure (EAD) file,
            # so LGD reads both files and applies the same input rules as ECL.
            prepared, _ead_uploads, lgd_uploads, collateral_config = await _prepare_ecl_inputs(db, run)
            _log.info(
                "lgd_inputs_prepared",
                run_id=run_id,
                filenames=[u.original_filename for u in lgd_uploads],
                loans=len(prepared.exposure),
                excluded=len(prepared.excluded),
                collateral_types=prepared.collateral_types,
                configured_types=[c["name"] for c in collateral_config],
                pct=40,
            )
            lgd_output = compute_lgd(prepared)
            _log.info("lgd_computed", run_id=run_id, output_rows=len(lgd_output), pct=80)

            _log.info("lgd_inserting_results", run_id=run_id, rows=len(lgd_output), pct=85)
            async with task_engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM lgd_results WHERE run_id = :run_id"),
                    {"run_id": run.id},
                )
                await bulk_insert_lgd_results(conn, run.id, run.tenant_id, lgd_output)
            _log.info("lgd_results_inserted", run_id=run_id, rows=len(lgd_output), pct=95)

            progress = await _mark_stage_complete(db, run.id, "lgd", started_at)
            _log.info(
                "compute_stage_completed",
                run_id=run_id,
                stage="lgd",
                elapsed_ms=progress.get("lgd", {}).get("elapsed_ms"),
                pct=100,
            )
            await _try_dispatch_ead(db, run_id)
            return {"run_id": run_id, "stage": "lgd"}
        except Exception as exc:
            _log.error("compute_stage_failed", run_id=run_id, stage="lgd", exc_info=exc)
            await _mark_run_failed(db, run.id, "lgd", exc)
            raise


async def _ead_ecl_main(run_id: str) -> dict[str, str]:
    """EAD + ECL stage: spec Steps 2-11 and the 12.1 reconciliation."""
    from app.core.run_enums import OutputArtifactKind, RunStatus, UploadKind
    from app.engine import ENGINE_VERSION
    from app.engine.bulk_insert import (
        bulk_insert_ead_results,
        bulk_insert_exceptions,
        bulk_insert_loan_summaries,
    )
    from app.engine.ecl_pipeline import run_ecl
    from app.engine.ecl_workbooks import (
        LOAN_DETAIL_FILENAME,
        PORTFOLIO_FILENAME,
        build_loan_detail_workbook,
        build_portfolio_workbook,
    )
    from app.engine.excel_writer import _build_pd_calcs_workbook
    from app.engine.pd_engine import compute_pd, is_pd_data_sheet

    async with _fresh_session() as (db, task_engine):
        run = await _load_run(db, run_id)
        if run.status == RunStatus.FAILED.value:
            _log.warning("ead_skipped_run_already_failed", run_id=run_id)
            return {"run_id": run_id, "stage": "ead", "skipped": True}
        started_at = await _mark_stage_running(db, run.id, "ead", RunStatus.EAD_RUNNING.value)
        _log.info("compute_stage_started", run_id=run_id, stage="ead", pct=0)
        active_stage = "ead"

        try:
            _log.info("ead_loading_inputs", run_id=run_id, pct=5)
            prepared, ead_uploads, lgd_uploads, collateral_config = await _prepare_ecl_inputs(db, run)
            # Recompute the PD curve from the PD files rather than reading pd_results,
            # which are rounded to 8 decimals: the exact curve validation previewed.
            pd_uploads = await _load_uploads(db, run_id, UploadKind.PD.value)
            pd_files = await _download_uploads(pd_uploads)
            if not run.combine_pd_files and len(pd_files) > 1:
                pd_files = pd_files[:1]
            pd_input = _combine_excel_bytes(pd_files, keep_sheet=is_pd_data_sheet)
            pd_output, cure_rates, intermediates = compute_pd(pd_input)
            pd_df = pd_output
            _log.info(
                "ead_inputs_prepared",
                run_id=run_id,
                loans=len(prepared.exposure),
                excluded=len(prepared.excluded),
                reporting_date=str(prepared.reporting_date),
                pd_rows=len(pd_df),
                pct=20,
            )

            ecl = run_ecl(prepared, pd_df)
            _log.info(
                "ead_rows_computed",
                run_id=run_id,
                snapshot_rows=len(ecl.rows),
                exceptions=len(ecl.exceptions),
                reconciliation_passed=ecl.reconciliation_passed,
                pct=55,
            )

            async with task_engine.begin() as conn:
                for table in ("ead_results", "ecl_loan_summaries", "run_exceptions"):
                    await conn.execute(text(f"DELETE FROM {table} WHERE run_id = :run_id"), {"run_id": run.id})
                await bulk_insert_ead_results(conn, run.id, run.tenant_id, ecl.rows)
                await bulk_insert_loan_summaries(conn, run.id, run.tenant_id, ecl.loan_summary)
                await bulk_insert_exceptions(conn, run.id, run.tenant_id, ecl.exceptions)
            _log.info("ead_results_inserted", run_id=run_id, rows=len(ecl.rows), pct=70)

            ecl_started_at = _iso_now()
            active_stage = "ecl"
            await _atomic_stage_update(
                db,
                run.id,
                "ecl",
                {"status": "running", "started_at": ecl_started_at, "finished_at": None, "elapsed_ms": None},
            )
            total_ecl = Decimal(str(ecl.total_ecl))
            total_outstanding = Decimal(str(ecl.total_outstanding))
            coverage_ratio = Decimal(str(ecl.coverage_ratio))
            run_warnings = [
                f"{e.category} · {e.loan_id or e.customer_id or e.dataset} · {e.issue} → {e.action}"
                for e in ecl.exceptions[:50]
            ]

            from app.modules.auth.models import User

            run_by = await db.scalar(select(User.email).where(User.id == run.created_by_user_id))
            timestamp_utc = _iso_now()
            engine_version = run.engine_version or ENGINE_VERSION

            def _input(dataset: str, uploads, rows: int) -> dict[str, Any]:
                return {
                    "dataset": dataset,
                    "file": ", ".join(u.original_filename for u in uploads),
                    "modified": max(u.created_at for u in uploads).strftime("%Y-%m-%d %H:%M UTC"),
                    "rows": rows,
                }

            meta = {
                "run_at": timestamp_utc,
                "run_by": run_by or run.created_by_user_id,
                "run_id": run.id,
                "engine_version": engine_version,
                "inputs": [
                    _input("Exposure Dataset (EAD)", ead_uploads, prepared.exposure_rows_in),
                    _input("Collateral Dataset (LGD)", lgd_uploads, sum(u.row_count or 0 for u in lgd_uploads)),
                    {
                        "dataset": "Collateral Parameters",
                        "file": "Workspace collateral types",
                        "modified": "current settings",
                        "rows": len(collateral_config),
                    },
                    _input("PD Dataset (from PD files)", pd_uploads, len(pd_df)),
                ],
            }
            run_meta = {
                "run_id": run.id,
                "tenant_id": run.tenant_id,
                "timestamp_utc": timestamp_utc,
                "engine_version": engine_version,
                "input_hashes": await _input_hashes(db, run_id),
            }
            workbooks = {
                "PD Calcs.xlsx": _build_pd_calcs_workbook(
                    run_meta, {"pd_df": pd_output, "cure_rates": cure_rates, "intermediates": intermediates}
                ),
                LOAN_DETAIL_FILENAME: build_loan_detail_workbook(ecl, meta),
                PORTFOLIO_FILENAME: build_portfolio_workbook(ecl, meta),
            }
            artifact_kinds = {
                "PD Calcs.xlsx": OutputArtifactKind.PD_CALCS.value,
                LOAN_DETAIL_FILENAME: OutputArtifactKind.LOAN_DETAIL.value,
                PORTFOLIO_FILENAME: OutputArtifactKind.PORTFOLIO_SUMMARY.value,
            }
            _log.info("ecl_workbooks_generated", run_id=run_id, sizes={k: len(v) for k, v in workbooks.items()}, pct=90)
            await _upload_workbooks(db, run, workbooks, artifact_kinds)
            _log.info("ecl_workbooks_uploaded", run_id=run_id, pct=96)

            run.reporting_date = prepared.reporting_date.date() if prepared.reporting_date is not None else None
            run.reconciliation = [
                {
                    "name": c.name,
                    "expected": c.expected,
                    "actual": c.actual,
                    "difference": c.difference,
                    "passed": c.passed,
                    "detail": c.detail,
                }
                for c in ecl.reconciliation
            ]
            await db.commit()

            del pd_input, pd_output, workbooks, ecl
            gc.collect()

            ead_progress = await _mark_stage_complete(db, run.id, "ead", started_at)
            _log.info(
                "compute_stage_completed",
                run_id=run_id,
                stage="ead",
                elapsed_ms=ead_progress.get("ead", {}).get("elapsed_ms"),
                pct=97,
            )
            ecl_finished_at = _iso_now()
            ecl_elapsed_ms = int(
                (datetime.fromisoformat(ecl_finished_at) - datetime.fromisoformat(ecl_started_at)).total_seconds()
                * 1000
            )
            await _atomic_stage_update(
                db,
                run.id,
                "ecl",
                {"status": "complete", "started_at": ecl_started_at, "finished_at": ecl_finished_at, "elapsed_ms": ecl_elapsed_ms},
                status=RunStatus.COMPLETE.value,
                total_ecl=total_ecl,
                total_outstanding=total_outstanding,
                coverage_ratio=coverage_ratio,
                run_warnings=run_warnings or None,
                finished_at=datetime.now(UTC),
            )
            _log.info(
                "compute_stage_completed",
                run_id=run_id,
                stage="ecl",
                elapsed_ms=ecl_elapsed_ms,
                total_ecl=float(total_ecl),
                total_outstanding=float(total_outstanding),
                coverage_ratio=float(coverage_ratio),
                run_warnings=run_warnings or [],
                pct=100,
            )
            _log.info(
                "run_complete",
                run_id=run_id,
                total_ecl=float(total_ecl),
                total_outstanding=float(total_outstanding),
                coverage_ratio=float(coverage_ratio),
                pct=100,
            )
            if run.created_by_user_id:
                from app.modules.notifications.service import notify_run_completed
                from app.modules.audit.models import AuditEvent
                from app.modules.audit.service import log_event

                await notify_run_completed(
                    db,
                    user_id=run.created_by_user_id,
                    tenant_id=run.tenant_id,
                    run_name=run.name,
                    run_id=run.id,
                )
                await log_event(
                    db,
                    AuditEvent.RUN_COMPLETED,
                    user_id=run.created_by_user_id,
                    tenant_id=run.tenant_id,
                    details={"run_id": run.id},
                )
            return {"run_id": run_id, "stage": "ead_ecl"}
        except Exception as exc:
            _log.error("compute_stage_failed", run_id=run_id, stage=active_stage, exc_info=exc)
            await _mark_run_failed(db, run.id, active_stage, exc)
            raise


# ---------------------------------------------------------------------------
# Celery tasks — thin wrappers that add _prepare_task for worker event loops
# ---------------------------------------------------------------------------


@celery_app.task(name="pd_task", bind=True)
def pd_task(self, run_id: str) -> dict[str, str]:  # type: ignore[misc]
    _log.info("task_received", run_id=run_id, stage="pd")
    async def _with_prepare():
        await _prepare_task()
        return await _pd_main(run_id)
    try:
        return _run(_with_prepare())
    except Exception as exc:
        _log.error("task_outer_failed", run_id=run_id, stage="pd", exc_info=exc)
        try:
            _run(_mark_run_failed_standalone(run_id, "pd", exc))
        except Exception:
            pass
        raise


@celery_app.task(name="lgd_task", bind=True)
def lgd_task(self, run_id: str) -> dict[str, str]:  # type: ignore[misc]
    _log.info("task_received", run_id=run_id, stage="lgd")
    async def _with_prepare():
        await _prepare_task()
        return await _lgd_main(run_id)
    try:
        return _run(_with_prepare())
    except Exception as exc:
        _log.error("task_outer_failed", run_id=run_id, stage="lgd", exc_info=exc)
        try:
            _run(_mark_run_failed_standalone(run_id, "lgd", exc))
        except Exception:
            pass
        raise


@celery_app.task(name="ead_ecl_task", bind=True)
def ead_ecl_task(self, run_id: str) -> dict[str, str]:  # type: ignore[misc]
    _log.info("task_received", run_id=run_id, stage="ead")
    async def _with_prepare():
        await _prepare_task()
        return await _ead_ecl_main(run_id)
    try:
        return _run(_with_prepare())
    except Exception as exc:
        _log.error("task_outer_failed", run_id=run_id, stage="ead", exc_info=exc)
        try:
            _run(_mark_run_failed_standalone(run_id, "ead", exc))
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Inline pipeline — runs in FastAPI's event loop (no worker needed)
# ---------------------------------------------------------------------------


async def celery_workers_available() -> bool:
    """True when at least one Celery worker answers ping."""
    try:

        def _ping() -> dict[str, Any] | None:
            return celery_app.control.inspect(timeout=0.8).ping()

        pong = await asyncio.wait_for(asyncio.to_thread(_ping), timeout=1.6)
        return bool(pong)
    except Exception as exc:
        _log.info("celery_worker_probe_failed", error=str(exc))
        return False


def schedule_inline_compute(run_id: str) -> None:
    """Start PD → LGD → EAD/ECL in this API process when no worker is listening."""
    task = asyncio.create_task(
        run_compute_pipeline_async(run_id),
        name=f"inline-compute-{run_id}",
    )
    _inline_jobs.add(task)
    task.add_done_callback(_inline_jobs.discard)
    _log.info("compute_pipeline_scheduled_inline", run_id=run_id)


async def run_compute_pipeline_async(run_id: str) -> None:
    """Run the full compute pipeline in the calling event loop.

    PD and LGD execute concurrently; EAD/ECL runs after both succeed.
    Stage failures are persisted to the database; this coroutine never raises.
    """
    token = _INLINE_COMPUTE.set(True)
    _log.info("compute_pipeline_inline_started", run_id=run_id)
    try:
        results = await asyncio.gather(
            _pd_main(run_id),
            _lgd_main(run_id),
            return_exceptions=True,
        )
        if any(isinstance(r, BaseException) for r in results):
            _log.warning("compute_pipeline_aborted", run_id=run_id, reason="pd_or_lgd_failed")
            return
        await _ead_ecl_main(run_id)
        _log.info("compute_pipeline_inline_finished", run_id=run_id)
    except Exception as exc:
        _log.error("compute_pipeline_unhandled_error", run_id=run_id, exc_info=exc)
    finally:
        _INLINE_COMPUTE.reset(token)
