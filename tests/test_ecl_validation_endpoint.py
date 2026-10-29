"""Endpoint tests for the PD → LGD → EAD validation lane (?scope=lgd, ?scope=ead).

Real PostgreSQL; only object storage is served from memory.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, Iterator
from decimal import Decimal

import pandas as pd
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.core.run_enums import UploadKind, ValidationStatus
from app.core.security import create_access_token, new_ulid
from app.modules.auth.models import User
from app.modules.collateral.models import CollateralType
from app.modules.runs.models import Run, Upload
from app.modules.tenants.models import Tenant, TenantMembership
from tests.conftest import TestSessionLocal
from tests.validation_client import post_validate

_FILES: dict[str, bytes] = {}
MONTHS = ["2024-10-31", "2024-11-30", "2024-12-31", "2025-01-31", "2025-02-28", "2025-03-31"]


def _xlsx(frame: pd.DataFrame, sheet: str = "Data") -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet, index=False)
        pd.DataFrame({"Notes": ["ignored"]}).to_excel(writer, sheet_name="Instructions", index=False)
    return buffer.getvalue()


def _pd_file() -> bytes:
    rows = []
    for i, m in enumerate(MONTHS):
        rows += [
            ("L1", m, "Stage 1", 100_000.0, "Retail"),
            ("L2", m, "Stage 1" if i < 2 else "Stage 2", 60_000.0, "Retail"),
            ("L3", m, "Stage 2" if i < 3 else "Stage 3", 30_000.0, "Retail"),
            ("L4", m, "Stage 2" if i % 2 else "Stage 1", 20_000.0, "Retail"),
        ]
    return _xlsx(pd.DataFrame(rows, columns=["Loan ID", "Reporting Month", "Staging", "Loan Amount", "SEGMENT"]))


def _lgd_file() -> bytes:
    return _xlsx(pd.DataFrame({
        "Customer ID": ["C1", "C1", "C2", "C3"],
        "Loan ID": ["L1", "L2", "L3", "L4"],
        "Outstanding Amount": [100_000, 60_000, 30_000, 20_000],
        "Effective Interest Rate (EIR)": [0.12, 0.12, 0.15, 0.10],
        "Property": [80_000, 50_000, 10_000, 0],   # C1 inconsistent → lowest (review)
    }))


def _ead_file(*, extra_bad_row: bool = True) -> bytes:
    rows = [
        ("L1", "C1", "Retail", "2025-03-31", "2023-01-31", "2028-03-31", "Stage 1", 100_000, 0.12),
        ("L2", "C1", "Retail", "2025-03-31", "2023-01-31", "2027-03-31", "Stage 2", 60_000, 0.12),
        ("L3", "C2", "Retail", "2025-03-31", "2022-01-31", "2026-03-31", "Stage 3", 30_000, 0.15),
        ("L4", "C3", "Retail", "2025-03-31", "2024-01-31", "2026-03-31", "Stage 2", 20_000, 0.10),
    ]
    if extra_bad_row:
        rows.append(("L5", "C4", "Retail", "2025-03-31", "2024-01-31", "2026-03-31", "Stage 1", 5_000, 14.0))
    return _xlsx(pd.DataFrame(rows, columns=[
        "Loan ID", "Customer ID", "SEGMENT", "Reporting Date", "First Payment Date", "Maturity Date",
        "Staging", "Outstanding Amount", "Effective Interest Rate (EIR)",
    ]))


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def _download(path: str) -> bytes:
        return _FILES[path]

    monkeypatch.setattr("app.modules.runs.pd_validation.download_bytes", _download)
    monkeypatch.setattr("app.modules.runs.ecl_validation.download_bytes", _download)
    _FILES.clear()
    yield
    _FILES.clear()


class Ctx:
    def __init__(self, tenant_id: str, run_id: str, headers: dict[str, str]):
        self.tenant_id, self.run_id, self.headers = tenant_id, run_id, headers

    def url(self, path: str) -> str:
        return f"/api/v1/tenants/{self.tenant_id}/runs/{self.run_id}/{path}"


@pytest_asyncio.fixture
async def ctx() -> AsyncGenerator[Ctx, None]:
    tenant_id, user_id, run_id = new_ulid(), new_ulid(), new_ulid()
    async with TestSessionLocal() as db:
        db.add(User(id=user_id, email="ecl@example.com", name="ECL Analyst", hashed_password="x",
                    is_active=True, is_email_verified=True))
        db.add(Tenant(id=tenant_id, name="T", slug=f"t-{tenant_id[:8]}", currency="KES"))
        db.add(TenantMembership(id=new_ulid(), tenant_id=tenant_id, user_id=user_id, role="analyst",
                                status="active"))
        db.add(CollateralType(id=new_ulid(), tenant_id=tenant_id, name="Property",
                              haircut=Decimal("20"), time_to_realize=24, created_by_user_id=user_id))
        db.add(Run(id=run_id, tenant_id=tenant_id, created_by_user_id=user_id, name="Run",
                   status="draft", engine_version="v1.3.0"))
        await db.commit()
    yield Ctx(tenant_id, run_id, {"Authorization": f"Bearer {create_access_token({'sub': user_id})}"})


async def _upload(ctx: Ctx, kind: str, content: bytes) -> str:
    upload_id = new_ulid()
    path = f"tenants/{ctx.tenant_id}/{upload_id}.xlsx"
    _FILES[path] = content
    async with TestSessionLocal() as db:
        db.add(Upload(id=upload_id, run_id=ctx.run_id, tenant_id=ctx.tenant_id, kind=kind,
                      original_filename=f"{kind.lower()}.xlsx", sha256="0" * 64, storage_path=path,
                      file_size_bytes=len(content)))
        await db.commit()
    return upload_id


async def _validate(client: AsyncClient, ctx: Ctx, scope: str) -> dict:
    r = await post_validate(client,ctx.url(f"validate?scope={scope}"), json={}, headers=ctx.headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def _statuses(ctx: Ctx) -> dict[str, tuple[str | None, bool]]:
    async with TestSessionLocal() as db:
        rows = (await db.execute(select(Upload).where(Upload.run_id == ctx.run_id))).scalars().all()
        return {u.kind: (u.validation_status, u.warnings_accepted) for u in rows}


def _codes(data: dict) -> dict[str, dict]:
    return {c["code"]: c for c in data["criteria"]}


@pytest.mark.asyncio
async def test_full_lane_pd_lgd_ead(client: AsyncClient, ctx: Ctx) -> None:
    await _upload(ctx, UploadKind.PD.value, _pd_file())
    assert (await _validate(client, ctx, "pd"))["status"] in ("ok", "warn")

    await _upload(ctx, UploadKind.LGD.value, _lgd_file())
    lgd = await _validate(client, ctx, "lgd")
    codes = _codes(lgd)
    assert lgd["preview"]["provisional"] is True
    assert codes["LG-14"]["outcome"] == "review"          # C1 rows differ → lowest
    assert codes["LG-11"]["outcome"] == "pass"
    params = lgd["preview"]["parameters"]
    assert params == [{"name": "Property", "haircut": 0.2, "years": 2.0, "valid": True}]
    l1 = next(x for x in lgd["preview"]["loans"] if x["loanId"] == "L1")
    # Provisional: C1 exposure 160,000 from the collateral file, lowest Property 50,000.
    assert l1["collateral"]["Property"][1] == pytest.approx(50_000 * 100_000 / 160_000)
    assert l1["discountedCollateral"] == pytest.approx(50_000 * 100_000 / 160_000 * 0.8 * 1.12**-2)
    assert lgd["status"] == "warn"
    assert (await _statuses(ctx))["LGD"] == (ValidationStatus.WARN.value, False)

    await _upload(ctx, UploadKind.EAD.value, _ead_file())
    ead = await _validate(client, ctx, "ead")
    codes = _codes(ead)
    assert codes["EA-11"]["outcome"] == "review"          # L5 EIR given as a percentage
    assert codes["EA-03"]["outcome"] == "pass"
    preview = ead["preview"]
    assert preview["reportingDate"] == "2025-03-31" and preview["reportingDateSource"] == "file"
    assert preview["totals"]["loans"] == 4 and preview["totals"]["excludedRows"] == 1
    assert [e["loanId"] for e in preview["excluded"]] == ["L5"]
    assert {r["name"] for r in preview["reconciliation"]} >= {"Loan count", "ECL build-up", "ECL limits"}
    assert preview["stageSummary"][-1]["key"] == "Total"
    assert preview["stageSummary"][-1]["ecl"] == pytest.approx(preview["totals"]["ecl"])
    l3 = next(x for x in preview["loans"] if x["loanId"] == "L3")
    assert l3["treatment"].startswith("Stage 3") and l3["rows"] == 1
    cols = preview["rows"]["columns"]
    l3_row = next(r for r in preview["rows"]["data"] if r[cols.index("Loan ID")] == "L3")
    assert l3_row[cols.index("Marginal PD")] == 1.0
    assert ead["status"] == "warn"

    # Accepting review items is per component.
    for kind in ("LGD", "EAD"):
        r = await client.post(ctx.url(f"validation/{kind}/accept"), json={}, headers=ctx.headers)
        assert r.status_code == 200 and r.json()["data"]["accepted"] == 1
    statuses = await _statuses(ctx)
    assert statuses["LGD"] == (ValidationStatus.WARN.value, True)
    assert statuses["EAD"] == (ValidationStatus.WARN.value, True)


@pytest.mark.asyncio
async def test_ead_blocks_without_a_pd_curve_for_the_segment(client: AsyncClient, ctx: Ctx) -> None:
    await _upload(ctx, UploadKind.PD.value, _pd_file())
    await _upload(ctx, UploadKind.LGD.value, _lgd_file())
    frame = pd.read_excel(io.BytesIO(_ead_file(extra_bad_row=False)))
    frame.loc[0, "SEGMENT"] = "Mortgage"
    await _upload(ctx, UploadKind.EAD.value, _xlsx(frame))
    ead = await _validate(client, ctx, "ead")
    assert ead["status"] == "blocking"
    assert _codes(ead)["EA-03"]["outcome"] == "block"
    assert "Mortgage" in _codes(ead)["EA-03"]["rationale"]


@pytest.mark.asyncio
async def test_missing_columns_block_lgd(client: AsyncClient, ctx: Ctx) -> None:
    await _upload(ctx, UploadKind.LGD.value, _xlsx(pd.DataFrame({"Customer ID": ["C1"], "Loan ID": ["L1"],
                                                                "Outstanding Amount": [1]})))
    lgd = await _validate(client, ctx, "lgd")
    assert lgd["status"] == "blocking"
    assert _codes(lgd)["LG-02"]["outcome"] == "block"


@pytest.mark.asyncio
async def test_unknown_scope_is_rejected(client: AsyncClient, ctx: Ctx) -> None:
    r = await post_validate(client,ctx.url("validate"), json={}, headers=ctx.headers)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reporting_date_change_resets_ead_validation(client: AsyncClient, ctx: Ctx) -> None:
    await _upload(ctx, UploadKind.PD.value, _pd_file())
    await _upload(ctx, UploadKind.LGD.value, _lgd_file())
    await _upload(ctx, UploadKind.EAD.value, _ead_file(extra_bad_row=False))
    await _validate(client, ctx, "ead")
    assert (await _statuses(ctx))["EAD"][0] is not None

    r = await client.patch(ctx.url("").rstrip("/"), json={"reporting_date": "2025-02-28"}, headers=ctx.headers)
    assert r.status_code == 200, r.text
    assert (await _statuses(ctx))["EAD"][0] is None
    ead = await _validate(client, ctx, "ead")
    # Every loan is dated 2025-03-31, so all are excluded against the run's date.
    assert _codes(ead)["EA-12"]["outcome"] == "review"
    assert ead["preview"]["reportingDateSource"] == "run"


@pytest.mark.asyncio
async def test_double_click_joins_the_running_validation_job(client: AsyncClient, ctx: Ctx) -> None:
    await _upload(ctx, UploadKind.PD.value, _pd_file())
    first = await client.post(ctx.url("validate?scope=pd"), json={}, headers=ctx.headers)
    second = await client.post(ctx.url("validate?scope=pd"), json={}, headers=ctx.headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["data"]["jobId"] == second.json()["data"]["jobId"]
    done = await post_validate(client, ctx.url("validate?scope=pd"), json={}, headers=ctx.headers)
    assert done.status_code == 200 and done.json()["data"]["status"] in ("ok", "warn")
