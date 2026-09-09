from typing import Literal

from pydantic import BaseModel, Field

from app.modules.runs.schemas import PdLoanPreviewOut, PdSegmentPreviewOut


class RunContextOut(BaseModel):
    runId: str
    period: str
    computedAt: str
    engineVersion: str
    currency: str


class SegmentDataOut(BaseModel):
    name: str
    mix: tuple[float, float, float]
    ecl: float
    outstanding: float
    coverage: str
    loans: int
    delta: float


class PortfolioTotalsOut(BaseModel):
    ecl: float
    outstanding: float
    loans: int
    coverage: str


class PortfolioViewOut(BaseModel):
    runContext: RunContextOut
    segments: list[SegmentDataOut]
    totals: PortfolioTotalsOut


class LoanRowOut(BaseModel):
    id: str
    customer: str
    stage: Literal[1, 2, 3]
    pd: float
    lgd: float
    ead: float
    ecl: float


class PDMatrixOut(BaseModel):
    rows: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]


class SegmentRateStatsOut(BaseModel):
    cureRate: float
    avgLgd: float | None = None
    avgPd12m: float | None = None


class SegmentViewOut(BaseModel):
    segment: SegmentDataOut
    pdMatrix: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    pdObservation: PdSegmentPreviewOut | None = None
    pdLoans: list[PdLoanPreviewOut] = Field(default_factory=list)
    rateStats: SegmentRateStatsOut | None = None
    loans: list[LoanRowOut]
    meta: dict[str, int | bool]


class MonthlyRundownOut(BaseModel):
    month: int
    marginalPd: float
    cumulativePd: float
    ead: float
    ecl: float


class LoanViewOut(BaseModel):
    id: str
    customer: str
    stage: Literal[1, 2, 3]
    pd: float
    lgd: float
    ead: float
    ecl: float
    segment: str
    outstanding: float
    collateralValue: float
    maturity: str
    rundown: list[MonthlyRundownOut]


class KpiDashboardOut(BaseModel):
    id: str
    label: str
    helpText: str | None = None
    currencyPrefix: str | None = None
    value: str
    delta: float | None = None
    deltaDir: Literal["up", "down", "flat"] | None = None
    subNote: str | None = None


class SegmentBarDashboardOut(BaseModel):
    name: str
    value: float


class StageSliceOut(BaseModel):
    stage: Literal["Stage 1", "Stage 2", "Stage 3"]
    percent: float
    colorVar: int = Field(ge=1, le=8)


class TrendPointOut(BaseModel):
    label: str
    value: float


class RunSummaryOut(BaseModel):
    id: str
    fullId: str
    period: str
    byInitials: str
    byName: str
    status: str
    eclAmount: float | None
    currency: str


class ActiveRunOut(BaseModel):
    runId: str
    name: str
    status: str
    progress: float
    stage: str | None = None


class DashboardOut(BaseModel):
    kpis: list[KpiDashboardOut]
    segments: list[SegmentBarDashboardOut]
    stages: list[StageSliceOut]
    trend: list[TrendPointOut]
    runs: list[RunSummaryOut]
    activeRun: ActiveRunOut | None = None


class DashboardResponse(BaseModel):
    data: DashboardOut


class PortfolioResponse(BaseModel):
    data: PortfolioViewOut


class SegmentResponse(BaseModel):
    data: SegmentViewOut


class LoanResponse(BaseModel):
    data: LoanViewOut
