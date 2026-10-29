"""Input validation rules (spec "ECL Calculation Specification" v1.0, section 1.5).

Pure and DB-free. Validation previews and compute both call `prepare_inputs`,
so a loan the reviewer saw excluded is exactly the loan compute excludes.

A failing loan does not stop the run: it is removed before Step 1 and written
to the Exception Report with the action taken. Only an unreadable file or a
missing required column blocks, and that is decided by the validators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from app.engine.workbook import _key

STAGES = ("Stage 1", "Stage 2", "Stage 3")

LOAN_ID = "Loan ID"
CUSTOMER_ID = "Customer ID"
SEGMENT = "SEGMENT"
REPORTING_DATE = "Reporting Date"
FIRST_PAYMENT_DATE = "First Payment Date"
MATURITY_DATE = "Maturity Date"
STAGE = "Stage"
OUTSTANDING = "Outstanding Amount"
EIR = "EIR"

EXPOSURE_FIELDS = (
    LOAN_ID,
    CUSTOMER_ID,
    SEGMENT,
    REPORTING_DATE,
    FIRST_PAYMENT_DATE,
    MATURITY_DATE,
    STAGE,
    OUTSTANDING,
    EIR,
)
_DATE_FIELDS = (REPORTING_DATE, FIRST_PAYMENT_DATE, MATURITY_DATE)

STAGE_ALIASES = ("Stage", "Staging", "Staging (Stage)")
EIR_ALIASES = ("EIR", "Effective Interest Rate (EIR)", "Effective Interest Rate")
# Accepted for older templates; the spec derives Adjusted Maturity and is monthly-only.
IGNORED_EXPOSURE_COLUMNS = ("Adjusted Maturity Date", "Repayment Frequency", "Payment Frequency")
COLLATERAL_KNOWN_COLUMNS = frozenset({LOAN_ID, CUSTOMER_ID, OUTSTANDING, *EIR_ALIASES})

# Exception Report categories (spec 12.2).
CAT_INPUT = "Input Validation"
CAT_PD = "PD Match"
CAT_CAP = "ECL Cap"
CAT_RECON = "Reconciliation"

DS_EXPOSURE = "Exposure"
DS_COLLATERAL = "Collateral"
DS_PARAMETERS = "Collateral Parameters"
DS_PD = "PD"


@dataclass(frozen=True, slots=True)
class ExceptionLine:
    category: str
    dataset: str
    loan_id: str
    customer_id: str
    field: str
    value: str
    issue: str
    action: str


@dataclass
class PreparedInputs:
    """Clean inputs for Steps 1-10 plus everything removed or adjusted on the way."""

    reporting_date: pd.Timestamp | None
    exposure: pd.DataFrame  # one row per included loan, canonical columns
    collateral: pd.DataFrame  # one row per included loan with a collateral row
    collateral_types: list[str]  # every collateral column, in file order
    parameters: dict[str, dict[str, float]]  # valid types only: haircut, time_to_realize
    excluded: pd.DataFrame  # Loan ID, Customer ID, Outstanding Amount, Reason
    exposure_rows_in: int
    exposure_outstanding_in: float
    exceptions: list[ExceptionLine] = field(default_factory=list)


def _first_present(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    return next((n for n in names if n in df.columns), None)


def _text(series: pd.Series) -> pd.Series:
    """Strip text cells; blank and NaN both become NA."""
    out = series.astype("string").str.strip()
    return out.mask(out == "", pd.NA)


def _fmt(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
        return ""
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    return str(value)


def is_exposure_sheet(df: pd.DataFrame) -> bool:
    cols = set(map(str, df.columns))
    return {LOAN_ID, REPORTING_DATE}.issubset(cols)


def is_collateral_sheet(df: pd.DataFrame) -> bool:
    cols = set(map(str, df.columns))
    return {LOAN_ID, CUSTOMER_ID, OUTSTANDING}.issubset(cols) and REPORTING_DATE not in cols


def collateral_type_columns(df: pd.DataFrame) -> list[str]:
    return [
        str(c)
        for c in df.columns
        if str(c) not in COLLATERAL_KNOWN_COLUMNS and not str(c).startswith("Unnamed")
    ]


def canonical_exposure(raw: pd.DataFrame) -> pd.DataFrame:
    """Rename aliases and coerce types. Unparseable cells become NA, never errors."""
    df = raw.copy()
    stage_col = _first_present(df, STAGE_ALIASES)
    if stage_col and stage_col != STAGE:
        df = df.rename(columns={stage_col: STAGE})
    eir_col = _first_present(df, EIR_ALIASES)
    if eir_col and eir_col != EIR:
        df = df.rename(columns={eir_col: EIR})
    for col in EXPOSURE_FIELDS:
        if col not in df.columns:
            df[col] = pd.NA
    for col in (LOAN_ID, CUSTOMER_ID, SEGMENT, STAGE):
        df[col] = _text(df[col])
    for col in _DATE_FIELDS:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    df[OUTSTANDING] = pd.to_numeric(df[OUTSTANDING], errors="coerce")
    df[EIR] = pd.to_numeric(df[EIR], errors="coerce")
    return df[list(EXPOSURE_FIELDS)].reset_index(drop=True)


def resolve_reporting_date(exposure: pd.DataFrame, run_date: date | None) -> pd.Timestamp | None:
    """The run's date when set, else the most common Reporting Date in the file."""
    if run_date is not None:
        return pd.Timestamp(run_date).normalize()
    dates = exposure[REPORTING_DATE].dropna()
    if dates.empty:
        return None
    counts = dates.value_counts()
    # Ties broken by earliest date so the choice is deterministic.
    return pd.Timestamp(min(counts[counts == counts.max()].index)).normalize()


def _exposure_failures(
    df: pd.DataFrame, reporting_date: pd.Timestamp | None
) -> list[tuple[pd.Series, str, pd.Series, str]]:
    """Every rule as (failing-rows mask, field, value column, issue), whole columns at once."""
    rules: list[tuple[pd.Series, str, pd.Series, str]] = [
        (df[LOAN_ID].notna() & df[LOAN_ID].duplicated(keep=False), LOAN_ID, df[LOAN_ID],
         "Loan ID appears more than once"),
    ]
    blank = pd.Series("", index=df.index)
    for col in EXPOSURE_FIELDS:
        rules.append((df[col].isna(), col, blank, f"{col} is missing or not a valid value"))
    stage, amount, rate = df[STAGE], df[OUTSTANDING], df[EIR]
    rep, fp, mat = df[REPORTING_DATE], df[FIRST_PAYMENT_DATE], df[MATURITY_DATE]
    rules += [
        (stage.notna() & ~stage.isin(STAGES), STAGE, stage, 'Stage is not "Stage 1", "Stage 2" or "Stage 3"'),
        (amount < 0, OUTSTANDING, amount, "Outstanding Amount is negative"),
        (rate < 0, EIR, rate, "EIR is negative"),
        (rate > 1, EIR, rate, "EIR is above 1 (probably given as a percentage, not a decimal)"),
        (fp > rep, FIRST_PAYMENT_DATE, fp, "First Payment Date is after the Reporting Date"),
        (mat < fp, MATURITY_DATE, mat, "Maturity Date is before the First Payment Date"),
    ]
    if reporting_date is not None:
        rules.append((
            rep.notna() & (rep != reporting_date), REPORTING_DATE, rep,
            "Reporting Date differs from the run's Reporting Date "
            f"({reporting_date.date().isoformat()})",
        ))
    return [(mask.fillna(False).astype(bool), fld, values, issue) for mask, fld, values, issue in rules]


def _parameters(
    collateral_types: list[str], config: list[dict[str, object]], exceptions: list[ExceptionLine]
) -> dict[str, dict[str, float]]:
    # "real estate" in the file matches the workspace type "Real Estate".
    by_name = {_key(str(c["name"])): c for c in config}
    valid: dict[str, dict[str, float]] = {}
    for name in collateral_types:
        cfg = by_name.get(_key(name))
        if cfg is None:
            exceptions.append(
                ExceptionLine(
                    CAT_INPUT,
                    DS_PARAMETERS,
                    "",
                    "",
                    "Collateral Type",
                    name,
                    "Collateral column has no matching parameters row",
                    "Treated as 0",
                )
            )
            continue
        haircut = pd.to_numeric(cfg.get("haircut"), errors="coerce")
        years = pd.to_numeric(cfg.get("time_to_realize"), errors="coerce")
        if pd.isna(haircut) or not 0 <= haircut <= 1:
            exceptions.append(
                ExceptionLine(
                    CAT_INPUT,
                    DS_PARAMETERS,
                    "",
                    "",
                    f"Haircut ({name})",
                    _fmt(haircut),
                    "Haircut is outside 0-1",
                    "Treated as 0",
                )
            )
            continue
        if pd.isna(years) or years < 0:
            exceptions.append(
                ExceptionLine(
                    CAT_INPUT,
                    DS_PARAMETERS,
                    "",
                    "",
                    f"Time to Realization ({name})",
                    _fmt(years),
                    "Time to Realization is negative or missing",
                    "Treated as 0",
                )
            )
            continue
        valid[name] = {"haircut": float(haircut), "time_to_realize": float(years)}
    return valid


def prepare_inputs(
    exposure_raw: pd.DataFrame,
    collateral_raw: pd.DataFrame | None,
    collateral_config: list[dict[str, object]],
    run_reporting_date: date | None = None,
) -> PreparedInputs:
    exceptions: list[ExceptionLine] = []
    exposure = canonical_exposure(exposure_raw)
    reporting_date = resolve_reporting_date(exposure, run_reporting_date)

    rules = _exposure_failures(exposure, reporting_date)
    excluded_mask = pd.Series(False, index=exposure.index)
    hits: list[tuple[int, int, ExceptionLine]] = []
    reasons_by_row: dict[int, list[str]] = {}
    for order, (mask, fld, values, issue) in enumerate(rules):
        excluded_mask |= mask
        for i in mask.index[mask]:
            row = exposure.loc[i]
            hits.append((i, order, ExceptionLine(
                CAT_INPUT, DS_EXPOSURE, _fmt(row[LOAN_ID]), _fmt(row[CUSTOMER_ID]),
                fld, _fmt(values.loc[i]), issue, "Loan excluded",
            )))
            reasons_by_row.setdefault(i, []).append(issue)
    # Row order, then rule order: the Exception Report reads loan by loan.
    exceptions.extend(line for _, _, line in sorted(hits, key=lambda h: (h[0], h[1])))
    reasons = ["; ".join(reasons_by_row.get(i, [])) for i in exposure.index]

    raw_ids = set(exposure[LOAN_ID].dropna())
    included = exposure[~excluded_mask]
    excluded_parts = [
        exposure.loc[excluded_mask, [LOAN_ID, CUSTOMER_ID, OUTSTANDING]].assign(
            Reason=[r for r, m in zip(reasons, excluded_mask, strict=True) if m]
        )
    ]

    # ── Collateral dataset ────────────────────────────────────────────────
    collateral_raw = collateral_raw if collateral_raw is not None else pd.DataFrame()
    types = collateral_type_columns(collateral_raw) if not collateral_raw.empty else []
    parameters = _parameters(types, collateral_config, exceptions)
    coll = collateral_raw.copy()
    for col in (LOAN_ID, CUSTOMER_ID):
        coll[col] = (
            _text(coll[col])
            if col in coll.columns
            else pd.Series(pd.NA, index=coll.index, dtype="string")
        )
    coll_eir_col = _first_present(coll, EIR_ALIASES)
    coll["Collateral EIR"] = (
        pd.to_numeric(coll[coll_eir_col], errors="coerce") if coll_eir_col else pd.NA
    )
    for t in types:
        numeric = pd.to_numeric(coll[t], errors="coerce")
        text_cells = numeric.isna() & _text(coll[t]).notna()
        for _, row in coll[text_cells].iterrows():
            exceptions.append(
                ExceptionLine(
                    CAT_INPUT,
                    DS_COLLATERAL,
                    _fmt(row[LOAN_ID]),
                    _fmt(row[CUSTOMER_ID]),
                    t,
                    _fmt(row[t]),
                    "Collateral value is not a number",
                    "Treated as 0",
                )
            )
        coll[t] = numeric
    coll = (
        coll[[LOAN_ID, CUSTOMER_ID, "Collateral EIR", *types]].copy()
        if not coll.empty
        else pd.DataFrame(columns=[LOAN_ID, CUSTOMER_ID, "Collateral EIR", *types])
    )

    orphan = coll[LOAN_ID].isna() | ~coll[LOAN_ID].isin(raw_ids)
    for _, row in coll[orphan].iterrows():
        exceptions.append(
            ExceptionLine(
                CAT_INPUT,
                DS_COLLATERAL,
                _fmt(row[LOAN_ID]),
                _fmt(row[CUSTOMER_ID]),
                LOAN_ID,
                _fmt(row[LOAN_ID]),
                "Loan ID is not in the Exposure Dataset",
                "Row ignored",
            )
        )
    coll = coll[~orphan]

    dup_ids = sorted(set(coll.loc[coll[LOAN_ID].duplicated(keep=False), LOAN_ID]))
    for loan_id in dup_ids:
        cust = included.loc[included[LOAN_ID] == loan_id, CUSTOMER_ID]
        exceptions.append(
            ExceptionLine(
                CAT_INPUT,
                DS_COLLATERAL,
                loan_id,
                _fmt(cust.iloc[0]) if len(cust) else "",
                LOAN_ID,
                loan_id,
                "Loan ID appears more than once in the Collateral Dataset",
                "Loan excluded",
            )
        )
    if dup_ids:
        dup_mask = included[LOAN_ID].isin(dup_ids)
        excluded_parts.append(
            included.loc[dup_mask, [LOAN_ID, CUSTOMER_ID, OUTSTANDING]].assign(
                Reason="Loan ID appears more than once in the Collateral Dataset"
            )
        )
        included = included[~dup_mask]
    # Rows for loans excluded above leave with their loan.
    coll = coll[coll[LOAN_ID].isin(set(included[LOAN_ID]))].copy()

    exposure_customer = included.set_index(LOAN_ID)[CUSTOMER_ID]
    expected_customer = coll[LOAN_ID].map(exposure_customer)
    mismatch = coll[CUSTOMER_ID].isna() | (coll[CUSTOMER_ID] != expected_customer)
    for (_, row), want in zip(coll[mismatch].iterrows(), expected_customer[mismatch], strict=True):
        exceptions.append(
            ExceptionLine(
                CAT_INPUT,
                DS_COLLATERAL,
                row[LOAN_ID],
                want,
                CUSTOMER_ID,
                _fmt(row[CUSTOMER_ID]),
                "Customer ID differs from the Exposure Dataset",
                "Exposure Customer ID used",
            )
        )
    coll[CUSTOMER_ID] = expected_customer

    exposure_eir = coll[LOAN_ID].map(included.set_index(LOAN_ID)[EIR])
    bad_eir = (
        coll["Collateral EIR"].isna() | (coll["Collateral EIR"] < 0) | (coll["Collateral EIR"] > 1)
    )
    for (_, row), want in zip(coll[bad_eir].iterrows(), exposure_eir[bad_eir], strict=True):
        exceptions.append(
            ExceptionLine(
                CAT_INPUT,
                DS_COLLATERAL,
                row[LOAN_ID],
                row[CUSTOMER_ID],
                EIR,
                _fmt(row["Collateral EIR"]),
                "Collateral EIR is missing or outside 0-1",
                f"Exposure EIR used ({_fmt(want)})",
            )
        )
    coll["Collateral EIR"] = coll["Collateral EIR"].mask(bad_eir, exposure_eir).astype(float)

    for t in types:
        negative = coll[t] < 0
        for _, row in coll[negative].iterrows():
            exceptions.append(
                ExceptionLine(
                    CAT_INPUT,
                    DS_COLLATERAL,
                    row[LOAN_ID],
                    row[CUSTOMER_ID],
                    t,
                    _fmt(row[t]),
                    "Negative collateral value",
                    "Treated as 0",
                )
            )
        # Blank means no collateral of that type (spec 1.1).
        coll[t] = coll[t].mask(negative, 0.0).fillna(0.0).astype(float)
        if t not in parameters:
            coll[t] = 0.0
            continue
        spread = coll.groupby(CUSTOMER_ID)[t].agg(["min", "max"])
        for customer, lo, hi in spread[spread["min"] != spread["max"]].itertuples():
            exceptions.append(
                ExceptionLine(
                    CAT_INPUT,
                    DS_COLLATERAL,
                    "",
                    customer,
                    t,
                    f"{lo:g} – {hi:g}",
                    "Customer shows different values for this collateral type across loan rows",
                    "Lowest value used",
                )
            )
        coll[t] = coll.groupby(CUSTOMER_ID)[t].transform("min")

    excluded = pd.concat(excluded_parts, ignore_index=True)
    excluded[OUTSTANDING] = pd.to_numeric(excluded[OUTSTANDING], errors="coerce")

    return PreparedInputs(
        reporting_date=reporting_date,
        exposure=included.reset_index(drop=True),
        collateral=coll.reset_index(drop=True),
        collateral_types=types,
        parameters=parameters,
        excluded=excluded,
        exposure_rows_in=len(exposure),
        exposure_outstanding_in=float(exposure[OUTSTANDING].fillna(0.0).sum()),
        exceptions=exceptions,
    )
