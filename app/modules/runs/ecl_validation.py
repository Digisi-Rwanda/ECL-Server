"""LGD- and EAD-scoped validation (`?scope=lgd`, `?scope=ead`) and review acceptance.

Mirrors `pd_validation`: load the run's uploads, hand them to the pure
checklist in `validators.ecl_criteria`, map onto the wire schema, record the
upload status and two audit events. The checklist runs the same
`prepare_inputs` / `run_ecl` that compute runs, so the preview is the result.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ECLException
from app.core.logging import get_logger
from app.core.run_enums import UploadKind, ValidationStatus
from app.core.storage import download_bytes
from app.engine import ENGINE_VERSION
from app.engine.ecl_pipeline import ECL_BEFORE_CAP, ECL_CAPPED, TOTAL_ECL, EclRun
from app.engine.ecl_rules import (
    CUSTOMER_ID,
    ExceptionLine,
    is_collateral_sheet,
    is_exposure_sheet,
)
from app.engine.ecl_workbooks import row_detail_frame
from app.engine.lgd_engine import (
    COLLATERAL_EIR,
    DISCOUNTED_COLLATERAL,
    LOAN_PROPORTION,
    TOTAL_CUSTOMER_EXPOSURE,
    allocated_col,
    discounted_col,
)
from app.engine.pd_engine import compute_pd, is_pd_data_sheet, prepare_pd_frame
from app.engine.validators.ecl_criteria import evaluate_ead, evaluate_lgd
from app.engine.workbook import frames_from_workbooks
from app.modules.audit.models import AuditEvent
from app.modules.audit.service import log_event
from app.modules.runs.models import Upload
from app.modules.runs.pd_validation import _criterion_out
from app.modules.runs.schemas import (
    AcceptReviewOut,
    CollateralParameterOut,
    EadLoanOut,
    EadPreviewOut,
    EadValidationResultOut,
    EclTotalsOut,
    ExceptionOut,
    ExcludedLoanOut,
    LgdLoanOut,
    LgdPreviewOut,
    LgdValidationResultOut,
    ReconciliationOut,
    RowTableOut,
    SummaryRowOut,
)

log = get_logger("runs.ecl_validation")

_STATUS_TO_UPLOAD_STATUS = {
    "ok": ValidationStatus.OK.value,
    "warn": ValidationStatus.WARN.value,
    "blocking": ValidationStatus.ERROR.value,
}
# shortcut: previews send at most this much; the downloads carry everything.
# Upgrade to server-side paging if reviewers need to browse huge books in the UI.
MAX_PREVIEW_LOANS = 5_000
MAX_PREVIEW_ROWS = 20_000
MAX_PREVIEW_EXCEPTIONS = 1_000


async def collateral_parameters(db: AsyncSession, tenant_id: str) -> list[dict[str, Any]]:
    """Workspace collateral types in spec units: haircut as a fraction, time in years."""
    from app.modules.collateral.models import CollateralType

    result = await db.execute(
        select(CollateralType).where(
            CollateralType.tenant_id == tenant_id, CollateralType.deleted_at.is_(None)
        )
    )
    return [
        # Stored as a percentage and in months (the admin form's units).
        {"name": r.name, "haircut": float(r.haircut) / 100.0, "time_to_realize": r.time_to_realize / 12.0}
        for r in result.scalars().all()
    ]


async def _uploads(db: AsyncSession, run_id: str, kind: str) -> list[Upload]:
    result = await db.execute(
        select(Upload).where(Upload.run_id == run_id, Upload.kind == kind).order_by(Upload.created_at)
    )
    return list(result.scalars().all())


async def _frame(uploads: list[Upload], keep) -> pd.DataFrame:
    if not uploads:
        return pd.DataFrame()
    try:
        files = [await download_bytes(u.storage_path) for u in uploads]
        return frames_from_workbooks(files, keep_sheet=keep)
    except Exception as exc:
        raise ECLException(
            "UNREADABLE_WORKBOOK", f"Could not read {uploads[0].original_filename}: {exc}", 400
        ) from exc


async def _pd_inputs(db: AsyncSession, run, *, curve: bool) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """PD loan rows and, when asked, the PD curve exactly as compute builds it."""
    uploads = await _uploads(db, run.id, UploadKind.PD.value)
    if not run.combine_pd_files:
        uploads = uploads[:1]
    frame = await _frame(uploads, is_pd_data_sheet)
    if frame.empty:
        return frame, None
    return frame, (compute_pd(frame)[0] if curve else None)


def _exception_out(e: ExceptionLine) -> ExceptionOut:
    return ExceptionOut(
        category=e.category, dataset=e.dataset, loanId=e.loan_id, customerId=e.customer_id,
        field=e.field, value=e.value, issue=e.issue, action=e.action,
    )


def _f(v: Any) -> float:
    return 0.0 if v is None or pd.isna(v) else float(v)


async def _record(
    db: AsyncSession, uploads: list[Upload], status: str, *, run_id: str, user_id: str,
    scope: str, criteria: list, ip: str | None, user_agent: str | None,
) -> None:
    for upload in uploads:
        upload.validation_status = _STATUS_TO_UPLOAD_STATUS[status]
        upload.warnings_accepted = False
    counts = Counter(c.outcome for c in criteria)
    await log_event(
        db,
        AuditEvent.VALIDATION_COMPLETED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={
            "run_id": run_id,
            "description": (
                f"{scope.upper()} validation {status} · {counts['pass']} passed, "
                f"{counts['review']} to review, {counts['block']} blocking"
            ),
            "scope": scope,
            "status": status,
            "engine_version": ENGINE_VERSION,
            "criteria_counts": dict(counts),
        },
    )
    await db.flush()


async def _started(db, *, run_id, user_id, scope, ip, user_agent) -> None:
    await log_event(
        db,
        AuditEvent.VALIDATION_TRIGGERED.value,
        user_id=user_id,
        ip=ip,
        user_agent=user_agent,
        details={"run_id": run_id, "description": f"{scope.upper()} validation started", "scope": scope},
    )


# ── LGD ──────────────────────────────────────────────────────────────────────


async def validate_lgd_files(
    db: AsyncSession, tenant_id: str, run_id: str, user_id: str,
    *, ip: str | None = None, user_agent: str | None = None,
) -> LgdValidationResultOut:
    from app.modules.runs.service import _get_run

    run = await _get_run(db, tenant_id, run_id)
    lgd_uploads = await _uploads(db, run_id, UploadKind.LGD.value)
    if not lgd_uploads:
        raise ECLException("NO_LGD_UPLOADS", "Upload the LGD (collateral) file before validating it.", 400)
    await _started(db, run_id=run_id, user_id=user_id, scope="lgd", ip=ip, user_agent=user_agent)

    collateral = await _frame(lgd_uploads, is_collateral_sheet)
    ead_uploads = await _uploads(db, run_id, UploadKind.EAD.value)
    exposure = await _frame(ead_uploads, is_exposure_sheet) if ead_uploads else None
    pd_frame, _ = await _pd_inputs(db, run, curve=False)
    pd_ids = set(pd_frame["Loan ID"].dropna().astype(str).str.strip()) if "Loan ID" in pd_frame else set()
    config = await collateral_parameters(db, tenant_id)

    outcome = evaluate_lgd(
        collateral, config,
        exposure=exposure if exposure is not None and not exposure.empty else None,
        pd_loan_ids=pd_ids, engine_version=ENGINE_VERSION,
    )
    preview = None
    if outcome.status != "blocking" or not outcome.lgd.empty:
        prepared, lgd = outcome.prepared, outcome.lgd
        by_name = {c["name"]: c for c in config}
        params = [
            CollateralParameterOut(
                name=t,
                haircut=by_name[t]["haircut"] if t in by_name else None,
                years=by_name[t]["time_to_realize"] if t in by_name else None,
                valid=t in prepared.parameters,
            )
            for t in prepared.collateral_types
        ]
        loans = [
            LgdLoanOut(
                loanId=str(r["Loan ID"]),
                customerId=str(r[CUSTOMER_ID]),
                outstanding=_f(r["Outstanding Amount"]),
                totalCustomerExposure=_f(r[TOTAL_CUSTOMER_EXPOSURE]),
                proportion=_f(r[LOAN_PROPORTION]),
                collateralEir=_f(r[COLLATERAL_EIR]),
                collateral={
                    t: [_f(r[t]), _f(r[allocated_col(t)]), _f(r[discounted_col(t)])]
                    for t in prepared.collateral_types
                },
                discountedCollateral=_f(r[DISCOUNTED_COLLATERAL]),
            )
            for _, r in lgd.head(MAX_PREVIEW_LOANS).iterrows()
        ]
        exceptions = [e for e in prepared.exceptions if e.dataset != "Exposure"]
        preview = LgdPreviewOut(
            provisional=outcome.provisional,
            parameters=params,
            loans=loans,
            loanCount=len(lgd),
            exceptions=[_exception_out(e) for e in exceptions[:MAX_PREVIEW_EXCEPTIONS]],
            exceptionCount=len(exceptions),
        )

    await _record(db, lgd_uploads, outcome.status, run_id=run_id, user_id=user_id, scope="lgd",
                  criteria=outcome.criteria, ip=ip, user_agent=user_agent)
    log.info("lgd_validate_complete", run_id=run_id, status=outcome.status, provisional=outcome.provisional)
    return LgdValidationResultOut(
        status=outcome.status,  # type: ignore[arg-type]
        criteria=[_criterion_out(c) for c in outcome.criteria],
        preview=preview,
        engine_version=ENGINE_VERSION,
    )


# ── EAD ──────────────────────────────────────────────────────────────────────


def _pd_latest(pd_frame: pd.DataFrame) -> pd.DataFrame:
    if pd_frame.empty:
        return pd.DataFrame(columns=["Loan ID", "Staging", "Loan Amount"])
    prepared = prepare_pd_frame(pd_frame)
    last = prepared.sort_values("Reporting Month", kind="mergesort").groupby("Loan ID").tail(1)
    out = last[["Loan ID", "Staging", "Loan Amount"]].copy()
    out["Loan ID"] = out["Loan ID"].astype(str).str.strip()
    out["Loan Amount"] = pd.to_numeric(out["Loan Amount"], errors="coerce")
    return out


def _cell(v: Any) -> float | int | str | None:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return float(v)
    return v


def summary_rows(table: pd.DataFrame, key: str) -> list[SummaryRowOut]:
    return [
        SummaryRowOut(key=str(r[key]), outstanding=_f(r["Outstanding Amount"]), ecl=_f(r["ECL"]),
                      coverage=_f(r["ECL Coverage Ratio"]))
        for _, r in table.iterrows()
    ]


def ead_preview(run: EclRun, reporting_date_source: str) -> EadPreviewOut:
    summary = run.loan_summary
    rows = run.rows
    first = rows.drop_duplicates("Loan ID").set_index("Loan ID")
    counts = rows.groupby("Loan ID").size()
    loans = [
        EadLoanOut(
            loanId=str(r["Loan ID"]), customerId=str(r[CUSTOMER_ID]), segment=str(r["SEGMENT"]),
            stage=str(r["Stage"]), treatment=str(first.at[r["Loan ID"], "Treatment"]),
            outstanding=_f(r["Outstanding Amount"]), discountedCollateral=_f(r[DISCOUNTED_COLLATERAL]),
            rows=int(counts.get(r["Loan ID"], 0)), eclBeforeCap=_f(r[ECL_BEFORE_CAP]),
            totalEcl=_f(r[TOTAL_ECL]), capped=r[ECL_CAPPED] == "Yes",
        )
        for _, r in summary.head(MAX_PREVIEW_LOANS).iterrows()
    ]
    detail = row_detail_frame(run)
    # Whole loans only, up to the row budget.
    sizes = detail.groupby("Loan ID", sort=False).size().cumsum()
    keep = set(sizes[sizes <= MAX_PREVIEW_ROWS].index)
    shown = detail[detail["Loan ID"].isin(keep)]
    excluded = run.prepared.excluded
    prepared_rep = run.prepared.reporting_date
    return EadPreviewOut(
        reportingDate=prepared_rep.date().isoformat() if prepared_rep is not None else None,
        reportingDateSource=reporting_date_source,  # type: ignore[arg-type]
        loans=loans,
        loanCount=len(summary),
        rows=RowTableOut(
            columns=list(shown.columns),
            data=[[_cell(v) for v in row] for row in shown.itertuples(index=False, name=None)],
        ),
        rowLoanCount=len(keep),
        totals=EclTotalsOut(
            outstanding=run.total_outstanding,
            ecl=run.total_ecl,
            eclBeforeCap=float(summary[ECL_BEFORE_CAP].sum()),
            coverage=run.coverage_ratio,
            loans=len(summary),
            excludedRows=len(excluded),
            cappedLoans=int((summary[ECL_CAPPED] == "Yes").sum()),
        ),
        stageSummary=summary_rows(run.stage_summary, "Stage"),
        segmentSummary=summary_rows(run.segment_summary, "SEGMENT"),
        reconciliation=[
            ReconciliationOut(name=c.name, expected=c.expected, actual=c.actual,
                              difference=c.difference, passed=c.passed, detail=c.detail)
            for c in run.reconciliation
        ],
        exceptions=[_exception_out(e) for e in run.exceptions[:MAX_PREVIEW_EXCEPTIONS]],
        exceptionCounts=dict(Counter(e.category for e in run.exceptions)),
        excluded=[
            ExcludedLoanOut(
                loanId="" if pd.isna(r["Loan ID"]) else str(r["Loan ID"]),
                customerId="" if pd.isna(r[CUSTOMER_ID]) else str(r[CUSTOMER_ID]),
                outstanding=None if pd.isna(r["Outstanding Amount"]) else float(r["Outstanding Amount"]),
                reason=str(r["Reason"]),
            )
            for _, r in excluded.head(MAX_PREVIEW_EXCEPTIONS).iterrows()
        ],
    )


async def build_ead_validation(db: AsyncSession, run, tenant_id: str):
    """Everything EAD validation and its export need, from the stored uploads."""
    ead_uploads = await _uploads(db, run.id, UploadKind.EAD.value)
    lgd_uploads = await _uploads(db, run.id, UploadKind.LGD.value)
    if not ead_uploads:
        raise ECLException("NO_EAD_UPLOADS", "Upload the EAD (exposure) file before validating it.", 400)
    if not lgd_uploads:
        raise ECLException("NO_LGD_UPLOADS", "Upload and validate LGD before EAD.", 400)
    exposure = await _frame(ead_uploads, is_exposure_sheet)
    collateral = await _frame(lgd_uploads, is_collateral_sheet)
    pd_frame, curve = await _pd_inputs(db, run, curve=True)
    if curve is None:
        raise ECLException("NO_PD_UPLOADS", "Upload and validate PD before EAD.", 400)
    config = await collateral_parameters(db, tenant_id)
    outcome = evaluate_ead(
        exposure, collateral, config, curve,
        pd_latest=_pd_latest(pd_frame), run_reporting_date=run.reporting_date,
        engine_version=ENGINE_VERSION,
    )
    return outcome, ead_uploads


async def validate_ead_files(
    db: AsyncSession, tenant_id: str, run_id: str, user_id: str,
    *, ip: str | None = None, user_agent: str | None = None,
) -> EadValidationResultOut:
    from app.modules.runs.service import _get_run

    run = await _get_run(db, tenant_id, run_id)
    await _started(db, run_id=run_id, user_id=user_id, scope="ead", ip=ip, user_agent=user_agent)
    outcome, ead_uploads = await build_ead_validation(db, run, tenant_id)
    preview = ead_preview(outcome.run, outcome.reporting_date_source) if outcome.run else None
    await _record(db, ead_uploads, outcome.status, run_id=run_id, user_id=user_id, scope="ead",
                  criteria=outcome.criteria, ip=ip, user_agent=user_agent)
    log.info("ead_validate_complete", run_id=run_id, status=outcome.status,
             loans=preview.loanCount if preview else 0)
    return EadValidationResultOut(
        status=outcome.status,  # type: ignore[arg-type]
        criteria=[_criterion_out(c) for c in outcome.criteria],
        preview=preview,
        engine_version=ENGINE_VERSION,
    )


async def build_ecl_preview_export(db: AsyncSession, tenant_id: str, run_id: str) -> tuple[bytes, str]:
    """Workbook 1 (Loan Detail) from the validation inputs, before compute."""
    from datetime import UTC, datetime

    from app.engine.ecl_workbooks import LOAN_DETAIL_FILENAME, build_loan_detail_workbook
    from app.modules.runs.service import _get_run

    run = await _get_run(db, tenant_id, run_id)
    outcome, ead_uploads = await build_ead_validation(db, run, tenant_id)
    if outcome.run is None:
        raise ECLException("EAD_BLOCKED", "Fix the blocking EAD checks before exporting.", 400)
    meta = {
        "run_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC") + " (validation preview)",
        "run_by": "validation preview",
        "run_id": run.id,
        "engine_version": ENGINE_VERSION,
        "inputs": [{"dataset": "Exposure Dataset (EAD)",
                    "file": ", ".join(u.original_filename for u in ead_uploads),
                    "modified": max(u.created_at for u in ead_uploads).strftime("%Y-%m-%d %H:%M UTC"),
                    "rows": outcome.run.prepared.exposure_rows_in}],
    }
    return build_loan_detail_workbook(outcome.run, meta), f"Preview - {LOAN_DETAIL_FILENAME}"


async def accept_review(db: AsyncSession, tenant_id: str, run_id: str, user_id: str, kind: str) -> AcceptReviewOut:
    """The reviewer accepts every Review item of one component's validation."""
    from app.modules.runs.service import _get_run

    kind = kind.upper()
    if kind not in {k.value for k in UploadKind}:
        raise ECLException("INVALID_UPLOAD_KIND", "kind must be PD, LGD, or EAD.", 400)
    await _get_run(db, tenant_id, run_id)
    uploads = [u for u in await _uploads(db, run_id, kind) if u.validation_status == ValidationStatus.WARN.value]
    for upload in uploads:
        upload.warnings_accepted = True
    await log_event(
        db,
        AuditEvent.VALIDATION_COMPLETED.value,
        user_id=user_id,
        details={"run_id": run_id, "description": f"{kind} review items accepted", "scope": kind.lower()},
    )
    await db.flush()
    return AcceptReviewOut(kind=kind, accepted=len(uploads))  # type: ignore[arg-type]
