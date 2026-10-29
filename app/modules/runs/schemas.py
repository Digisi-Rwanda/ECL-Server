from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.engine.format_utils import format_file_size, short_ulid


class CreateRunRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    reporting_period: str | None = Field(default=None, max_length=100)
    # Spec 1.5: exposure rows with another Reporting Date are excluded.
    # Null = use the EAD file's most common Reporting Date.
    reporting_date: date | None = None


class UpdateRunRequest(BaseModel):
    combine_pd_files: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    reporting_period: str | None = Field(default=None, max_length=100)
    reporting_date: date | None = None


class RerunRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)


class UploadOut(BaseModel):
    id: str
    kind: str
    filename: str
    size_bytes: int
    sha256: str
    sheet_count: int | None = None
    row_count: int | None = None


class ValidateRunRequest(BaseModel):
    accepted_warning_ids: list[str] = Field(default_factory=list)


class RunListItemOut(BaseModel):
    id: str
    fullId: str
    name: str
    period: str
    createdAt: str
    byInitials: str
    byName: str
    status: str
    eclAmount: float | None
    coverage: str | None
    currency: str


class EngineStageProgressOut(BaseModel):
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int | None = None


class EngineProgressOut(BaseModel):
    pd: EngineStageProgressOut
    lgd: EngineStageProgressOut
    ead: EngineStageProgressOut
    ecl: EngineStageProgressOut


class RunInputFileOut(BaseModel):
    type: Literal["PD", "LGD", "EAD"]
    name: str
    size: str
    sheets: int
    validationStatus: Literal["ok", "warn", "error"]
    warningCount: int | None = None
    hash: str


class RunAuditEventOut(BaseModel):
    id: str
    kind: Literal["ok", "err", "accent", "default"]
    iconName: str
    title: str
    description: str
    who: str
    time: str


class EngineInfoOut(BaseModel):
    version: str
    releasedDate: str
    pdMethod: str
    lgdMethod: str
    eadMethod: str
    pdFilesCombined: bool
    deterministic: bool


class FailureDetailsOut(BaseModel):
    stage: str
    message: str
    ref: str


class KpiOut(BaseModel):
    id: str
    label: str
    helpText: str | None = None
    currencyPrefix: str | None = None
    value: str
    delta: float | None = None
    deltaDir: Literal["up", "down", "flat"] | None = None
    subNote: str | None = None
    staleAsOf: str | None = None


class SegmentBarOut(BaseModel):
    name: str
    value: float


class RunDetailOut(RunListItemOut):
    reportingDate: str | None = None
    # pending | running | complete | error: the Excel downloads are built after the run completes.
    workbooksStatus: str | None = None
    elapsed: str | None = None
    kpis: list[KpiOut]
    segments: list[SegmentBarOut]
    inputFiles: list[RunInputFileOut]
    auditEvents: list[RunAuditEventOut]
    engineInfo: EngineInfoOut
    engineProgress: EngineProgressOut | None = None
    failureDetails: FailureDetailsOut | None = None
    deletedBy: str | None = None
    deletedAt: str | None = None
    acceptedWarnings: int | None = None


class ExecuteRunOut(BaseModel):
    run_id: str
    status: str
    dispatch_task: bool = True


class PresignedDownloadOut(BaseModel):
    url: str
    expires_at: datetime


# ── PD-scoped validation (?scope=pd) ─────────────────────────────────────────
# These are additive. The unscoped validate path keeps returning
# ValidationResultOut unchanged.


class PdCriterionOut(BaseModel):
    id: str
    code: str
    name: str
    category: Literal["structural", "business", "info"]
    outcome: Literal["pass", "review", "block", "info"]
    rationale: str


class PdLoanPreviewOut(BaseModel):
    loan_id: str
    segment: str
    reporting_month: str
    reporting_month_key: str
    # 1 | 2 | 3 for an on-book stage, "offbooks" once the loan leaves.
    staging: int | str
    next_staging: int | str
    # True when this is the segment's last reporting month, where "offbooks"
    # means "no next month in the data" rather than "the loan exited".
    is_final_month: bool


class PdSegmentStatsOut(BaseModel):
    # Fraction, not a percentage. The client formats it.
    cure_rate: float
    loans_observed: int
    months_of_history: int


class PdSegmentPreviewOut(BaseModel):
    segment: str
    # Row-normalized 3x4, aligned with dest_labels.
    matrix: list[list[float]]
    # Pre-formatted money for each cell — the server owns all formatting.
    amounts: list[list[str]]
    dest_labels: list[str]
    stats: PdSegmentStatsOut
    # Lifetime PD term structure (spec Steps 1-7), identical to compute_pd.
    proportions: list[list[float]]
    observed_3: list[list[str]]
    normalized: list[list[float]]
    # [transition][month - 1] = [Stage 1, Stage 2, Stage 3, Marginal PD], months 1..299
    term_structure: list[list[list[float]]]


class PdPreviewOut(BaseModel):
    loans: list[PdLoanPreviewOut]
    segments: list[PdSegmentPreviewOut]
    engine_version: str


class PdValidationIssueOut(BaseModel):
    id: str
    level: Literal["warn", "block"]
    title: str
    location: str
    fix: str
    category: str | None = None


class PdValidationResultOut(BaseModel):
    status: Literal["ok", "warn", "blocking"]
    criteria: list[PdCriterionOut]
    pd_preview: PdPreviewOut
    issues: list[PdValidationIssueOut]


class PdValidationResponse(BaseModel):
    data: PdValidationResultOut


class RunResponse(BaseModel):
    data: RunDetailOut


class RunListResponse(BaseModel):
    data: list[RunListItemOut]
    meta: dict[str, Any]


class UploadResponse(BaseModel):
    data: UploadOut


class ExecuteRunResponse(BaseModel):
    data: ExecuteRunOut


class PresignedDownloadResponse(BaseModel):
    data: PresignedDownloadOut


def hash_display(sha256: str) -> str:
    return short_ulid(sha256)


def upload_to_input_file(
    *,
    kind: str,
    name: str,
    file_size_bytes: int,
    sheet_count: int | None,
    validation_status: str | None,
    warning_count: int,
    sha256: str,
) -> RunInputFileOut:
    status: Literal["ok", "warn", "error"] = "ok"
    if validation_status == "error":
        status = "error"
    elif validation_status == "warn":
        status = "warn"
    return RunInputFileOut(
        type=kind,  # type: ignore[arg-type]
        name=name,
        size=format_file_size(file_size_bytes),
        sheets=sheet_count or 1,
        validationStatus=status,
        warningCount=warning_count if warning_count else None,
        hash=hash_display(sha256),
    )


# ── ECL spec v1.0 report pieces, shared by LGD/EAD validation and results ────


class ReconciliationOut(BaseModel):
    name: str
    expected: float
    actual: float
    difference: float
    passed: bool
    detail: str


class SummaryRowOut(BaseModel):
    key: str
    outstanding: float
    ecl: float
    # Fraction; the client formats it.
    coverage: float


class ExceptionOut(BaseModel):
    category: str
    dataset: str
    loanId: str
    customerId: str
    field: str
    value: str
    issue: str
    action: str


class EclTotalsOut(BaseModel):
    outstanding: float
    ecl: float
    eclBeforeCap: float
    coverage: float
    loans: int
    excludedRows: int
    cappedLoans: int


class EclReportOut(BaseModel):
    runId: str
    reportingDate: str | None
    specVersion: str
    engineVersion: str
    totals: EclTotalsOut
    stageSummary: list[SummaryRowOut]
    segmentSummary: list[SummaryRowOut]
    reconciliation: list[ReconciliationOut]
    exceptionCounts: dict[str, int]


class EclReportResponse(BaseModel):
    data: EclReportOut


class ExceptionsPageOut(BaseModel):
    items: list[ExceptionOut]
    total: int
    page: int
    perPage: int


class ExceptionsPageResponse(BaseModel):
    data: ExceptionsPageOut


# ── LGD / EAD validation (?scope=lgd, ?scope=ead) ────────────────────────────


class CollateralParameterOut(BaseModel):
    name: str
    haircut: float | None
    years: float | None
    valid: bool


class LgdLoanOut(BaseModel):
    loanId: str
    customerId: str
    outstanding: float
    totalCustomerExposure: float
    proportion: float
    collateralEir: float
    # Per collateral type: [customer value, allocated, discounted]
    collateral: dict[str, list[float]]
    discountedCollateral: float


class LgdPreviewOut(BaseModel):
    provisional: bool
    parameters: list[CollateralParameterOut]
    loans: list[LgdLoanOut]
    loanCount: int
    exceptions: list[ExceptionOut]
    exceptionCount: int


class LgdValidationResultOut(BaseModel):
    status: Literal["ok", "warn", "blocking"]
    criteria: list[PdCriterionOut]
    preview: LgdPreviewOut | None
    engine_version: str


class LgdValidationResponse(BaseModel):
    data: LgdValidationResultOut


class ExcludedLoanOut(BaseModel):
    loanId: str
    customerId: str
    outstanding: float | None
    reason: str


class EadLoanOut(BaseModel):
    loanId: str
    customerId: str
    segment: str
    stage: str
    treatment: str
    outstanding: float
    discountedCollateral: float
    rows: int
    eclBeforeCap: float
    totalEcl: float
    capped: bool


class RowTableOut(BaseModel):
    """Row Detail as columns + rows of values (compact for large books)."""

    columns: list[str]
    data: list[list[float | int | str | None]]


class EadPreviewOut(BaseModel):
    reportingDate: str | None
    reportingDateSource: Literal["run", "file"]
    loans: list[EadLoanOut]
    loanCount: int
    rows: RowTableOut
    rowLoanCount: int
    totals: EclTotalsOut
    stageSummary: list[SummaryRowOut]
    segmentSummary: list[SummaryRowOut]
    reconciliation: list[ReconciliationOut]
    exceptions: list[ExceptionOut]
    exceptionCounts: dict[str, int]
    excluded: list[ExcludedLoanOut]


class EadValidationResultOut(BaseModel):
    status: Literal["ok", "warn", "blocking"]
    criteria: list[PdCriterionOut]
    preview: EadPreviewOut | None
    engine_version: str


class EadValidationResponse(BaseModel):
    data: EadValidationResultOut


class AcceptReviewOut(BaseModel):
    kind: Literal["PD", "LGD", "EAD"]
    accepted: int


class AcceptReviewResponse(BaseModel):
    data: AcceptReviewOut
