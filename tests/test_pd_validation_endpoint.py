"""Endpoint tests for POST /tenants/{tenant}/runs/{run}/validate?scope=pd.

Runs against real PostgreSQL. Only object storage is substituted — uploads are
inserted as rows and their bytes served from memory, so the download is the one
thing not exercised here (`test_storage.py` covers that separately).

The status codes matter more than usual on this route: the web client treats a
4xx as "endpoint not implemented" and swaps in fabricated all-green sample data,
so returning 4xx for a real rejection would show a reviewer a fake pass.
"""

import io
from collections.abc import AsyncGenerator, Iterator
from typing import Any

import pandas as pd
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.core.run_enums import UploadKind, ValidationStatus
from app.core.security import create_access_token, new_ulid
from app.engine import ENGINE_VERSION
from app.modules.audit.models import AuditEvent, AuditLog
from app.modules.auth.models import User
from app.modules.runs.models import PdValidationThresholds, Run, Upload
from app.modules.segments.models import Segment
from app.modules.tenants.models import Tenant, TenantMembership
from tests.conftest import TestSessionLocal

MONTH_ENDS = ["2024-10-31", "2024-11-30", "2024-12-31", "2025-01-31", "2025-02-28"]

_FILES: dict[str, bytes] = {}


def _workbook(rows: list[tuple], *, sheets: dict[str, pd.DataFrame] | None = None) -> bytes:
    frames = sheets or {
        "PD Data": pd.DataFrame(
            rows, columns=["Loan ID", "Reporting Month", "Staging", "Loan Amount", "SEGMENT"]
        )
    }
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in frames.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def _healthy_rows() -> list[tuple]:
    rows: list[tuple] = []
    for month in MONTH_ENDS:
        rows.append(("L001", month, "Stage 1", 100_000.0, "Retail"))
        rows.append(("L002", month, "Stage 1", 100_000.0, "Retail"))
        rows.append(("L003", month, "Stage 2", 60_000.0, "Retail"))
    rows.append(("L004", MONTH_ENDS[0], "Stage 3", 40_000.0, "Retail"))
    rows.append(("L004", MONTH_ENDS[1], "Stage 3", 40_000.0, "Retail"))
    for month in MONTH_ENDS[2:]:
        rows.append(("L004", month, "Stage 2", 40_000.0, "Retail"))
    return rows


class _Ctx:
    def __init__(self, tenant_id: str, user_id: str, run_id: str, headers: dict[str, str]):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.run_id = run_id
        self.headers = headers

    def url(self, *, run_id: str | None = None, scope: str | None = "pd") -> str:
        base = f"/api/v1/tenants/{self.tenant_id}/runs/{run_id or self.run_id}/validate"
        return f"{base}?scope={scope}" if scope else base


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bytes]]:
    """Serve upload bytes from memory instead of the object store."""

    async def _download(path: str) -> bytes:
        return _FILES[path]

    monkeypatch.setattr("app.modules.runs.pd_validation.download_bytes", _download)
    _FILES.clear()
    yield _FILES
    _FILES.clear()


@pytest_asyncio.fixture
async def ctx() -> AsyncGenerator[_Ctx, None]:
    tenant_id, user_id, run_id = new_ulid(), new_ulid(), new_ulid()
    async with TestSessionLocal() as db:
        db.add(
            User(
                id=user_id,
                email="pd-analyst@example.com",
                name="PD Analyst",
                hashed_password="x",
                is_active=True,
                is_email_verified=True,
            )
        )
        db.add(Tenant(id=tenant_id, name="Tenant PD", slug=f"t-{tenant_id[:8]}", currency="KES"))
        db.add(
            TenantMembership(
                id=new_ulid(),
                tenant_id=tenant_id,
                user_id=user_id,
                role="analyst",
                status="active",
            )
        )
        db.add(
            Run(
                id=run_id,
                tenant_id=tenant_id,
                created_by_user_id=user_id,
                name="PD Preview Run",
                status="draft",
                engine_version="v1.2.0",
            )
        )
        await db.commit()

    token = create_access_token({"sub": user_id})
    yield _Ctx(tenant_id, user_id, run_id, {"Authorization": f"Bearer {token}"})


async def _add_upload(
    ctx: _Ctx,
    content: bytes,
    *,
    kind: str = UploadKind.PD.value,
    filename: str = "pd.xlsx",
) -> str:
    upload_id = new_ulid()
    storage_path = f"tenants/{ctx.tenant_id}/{upload_id}.xlsx"
    _FILES[storage_path] = content
    async with TestSessionLocal() as db:
        db.add(
            Upload(
                id=upload_id,
                run_id=ctx.run_id,
                tenant_id=ctx.tenant_id,
                kind=kind,
                original_filename=filename,
                sha256="0" * 64,
                storage_path=storage_path,
                file_size_bytes=len(content),
            )
        )
        await db.commit()
    return upload_id


def _codes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["code"]: c for c in payload["data"]["criteria"]}


# ── Happy path ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_pd_file_returns_the_full_checklist_and_preview(
    client: AsyncClient, ctx: _Ctx
) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["status"] == "ok"
    assert data["issues"] == []

    codes = _codes(response.json())
    assert set(codes) == {
        "PD-01",
        "PD-02",
        "PD-03",
        "PD-04",
        "PD-05",
        "PD-06",
        "PD-07",
        *(f"EC-{n}" for n in range(10, 20)),
    }
    assert codes["EC-18"]["outcome"] == "info"
    assert all(
        c["outcome"] == "pass" for code, c in codes.items() if code != "EC-18"
    ), [c for c in codes.values() if c["outcome"] not in ("pass", "info")]


@pytest.mark.asyncio
async def test_preview_payload_matches_the_engine(client: AsyncClient, ctx: _Ctx) -> None:
    """Every displayed number is computed server-side; the client only renders."""
    await _add_upload(ctx, _workbook(_healthy_rows()))

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    preview = response.json()["data"]["pd_preview"]

    assert preview["engine_version"] == ENGINE_VERSION
    assert len(preview["loans"]) == len(_healthy_rows())

    first = preview["loans"][0]
    assert first["loan_id"] == "L001"
    assert first["reporting_month"] == "Oct 2024"
    assert first["reporting_month_key"] == "2024-10"
    assert first["staging"] == 1
    assert first["next_staging"] == 1
    assert first["is_final_month"] is False

    retail = preview["segments"][0]
    assert retail["segment"] == "Retail"
    assert retail["dest_labels"] == ["Stage 1", "Stage 2", "Stage 3", "Offbooks"]
    assert len(retail["matrix"]) == 3
    assert all(len(row) == 4 for row in retail["matrix"])
    # Amounts arrive pre-formatted so the client never re-derives currency display.
    assert all(isinstance(cell, str) for row in retail["amounts"] for cell in row)
    assert retail["stats"]["loans_observed"] == 4
    assert retail["stats"]["months_of_history"] == 5
    assert 0.0 <= retail["stats"]["cure_rate"] <= 1.0


@pytest.mark.asyncio
async def test_validation_marks_only_the_pd_uploads(client: AsyncClient, ctx: _Ctx) -> None:
    pd_id = await _add_upload(ctx, _workbook(_healthy_rows()))
    lgd_id = await _add_upload(
        ctx, b"not read on this path", kind=UploadKind.LGD.value, filename="lgd.xlsx"
    )

    await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)

    async with TestSessionLocal() as db:
        uploads = {
            u.id: u.validation_status
            for u in (await db.execute(select(Upload))).scalars().all()
        }
    assert uploads[pd_id] == ValidationStatus.OK.value
    assert uploads[lgd_id] is None


@pytest.mark.asyncio
async def test_thresholds_and_segments_are_provisioned_on_first_use(
    client: AsyncClient, ctx: _Ctx
) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200

    async with TestSessionLocal() as db:
        thresholds = (
            await db.execute(
                select(PdValidationThresholds).where(
                    PdValidationThresholds.tenant_id == ctx.tenant_id
                )
            )
        ).scalar_one()
        segments = {
            s.name
            for s in (
                await db.execute(select(Segment).where(Segment.tenant_id == ctx.tenant_id))
            )
            .scalars()
            .all()
        }

    assert int(thresholds.min_transitions) == 3
    assert float(thresholds.stage3_share_max) == 0.4
    # A workspace should not have to pre-declare its vocabulary to preview PD.
    assert "Retail" in segments
    assert _codes(response.json())["PD-07"]["outcome"] == "pass"


@pytest.mark.asyncio
async def test_validation_writes_scoped_audit_events(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))
    await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)

    async with TestSessionLocal() as db:
        logs = (await db.execute(select(AuditLog).order_by(AuditLog.created_at))).scalars().all()

    events = [log.event_type for log in logs]
    assert AuditEvent.VALIDATION_TRIGGERED.value in events
    assert AuditEvent.VALIDATION_COMPLETED.value in events

    completed = next(
        log for log in logs if log.event_type == AuditEvent.VALIDATION_COMPLETED.value
    )
    assert completed.details["scope"] == "pd"
    assert completed.details["status"] == "ok"
    assert completed.details["engine_version"] == ENGINE_VERSION
    assert completed.details["criteria_counts"]["block"] == 0


@pytest.mark.asyncio
async def test_validation_is_repeatable(client: AsyncClient, ctx: _Ctx) -> None:
    """Nothing is persisted, so a second call must return the identical payload."""
    await _add_upload(ctx, _workbook(_healthy_rows()))

    first = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    second = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


@pytest.mark.asyncio
async def test_scope_matching_is_case_insensitive(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))
    response = await client.post(
        ctx.url(scope="PD"), json={"accepted_warning_ids": []}, headers=ctx.headers
    )
    assert response.status_code == 200
    assert "pd_preview" in response.json()["data"]


# ── Multiple files ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_pd_files_are_always_combined_for_the_preview(
    client: AsyncClient, ctx: _Ctx
) -> None:
    """Even with combining off, the reviewer sees every uploaded file.

    Compute may use only the first, but silently previewing a subset would hide
    data the user just uploaded. EC-18 states the divergence instead.
    """
    early = [row for row in _healthy_rows() if row[1] in MONTH_ENDS[:3]]
    late = [row for row in _healthy_rows() if row[1] in MONTH_ENDS[3:]]
    await _add_upload(ctx, _workbook(early), filename="pd_q4.xlsx")
    await _add_upload(ctx, _workbook(late), filename="pd_q1.xlsx")

    async with TestSessionLocal() as db:
        run = (await db.execute(select(Run).where(Run.id == ctx.run_id))).scalar_one()
        run.combine_pd_files = False
        await db.commit()

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["pd_preview"]["segments"][0]["stats"]["months_of_history"] == 5
    assert len(data["pd_preview"]["loans"]) == len(_healthy_rows())

    stamp = _codes(response.json())["EC-18"]["rationale"]
    assert "2 PD file(s)" in stamp
    assert "will use only the first file" in stamp


@pytest.mark.asyncio
async def test_duplicate_rows_across_two_files_block(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()), filename="pd_a.xlsx")
    await _add_upload(
        ctx, _workbook(_healthy_rows()[:1]), filename="pd_b.xlsx"
    )

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["status"] == "blocking"
    assert _codes(response.json())["EC-10"]["outcome"] == "block"
    locations = " ".join(issue["location"] for issue in data["issues"])
    assert "pd_a.xlsx" in locations
    assert "pd_b.xlsx" in locations


@pytest.mark.asyncio
async def test_non_data_sheets_are_skipped(client: AsyncClient, ctx: _Ctx) -> None:
    sheets = {
        "Cover": pd.DataFrame({"Report": ["Q4 credit pack"], "Owner": ["Risk"]}),
        "PD Data": pd.DataFrame(
            _healthy_rows(),
            columns=["Loan ID", "Reporting Month", "Staging", "Loan Amount", "SEGMENT"],
        ),
    }
    await _add_upload(ctx, _workbook([], sheets=sheets))

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"
    assert "1 sheet(s) were skipped" in _codes(response.json())["EC-18"]["rationale"]


# ── Failures that must still be a 200 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocking_structural_data_returns_200_with_real_criteria(
    client: AsyncClient, ctx: _Ctx
) -> None:
    """A 4xx here would make the client substitute an all-green mock."""
    rows = [*_healthy_rows(), ("L900", MONTH_ENDS[0], "Stage 4", 10_000.0, "Retail")]
    await _add_upload(ctx, _workbook(rows))

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200
    data = response.json()["data"]

    assert data["status"] == "blocking"
    assert _codes(response.json())["PD-05"]["outcome"] == "block"
    assert data["issues"]
    assert all(issue["id"] for issue in data["issues"])
    # Present but empty, so the client renders the failure rather than falling through.
    assert data["pd_preview"]["loans"] == []
    assert data["pd_preview"]["segments"] == []
    assert data["pd_preview"]["engine_version"] == ENGINE_VERSION

    async with TestSessionLocal() as db:
        upload = (await db.execute(select(Upload))).scalars().one()
    assert upload.validation_status == ValidationStatus.ERROR.value


@pytest.mark.asyncio
async def test_warnings_return_200_with_status_warn(client: AsyncClient, ctx: _Ctx) -> None:
    rows = [row for row in _healthy_rows() if row[1] != MONTH_ENDS[2]]
    await _add_upload(ctx, _workbook(rows))

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "warn"
    assert _codes(response.json())["EC-11"]["outcome"] == "review"

    async with TestSessionLocal() as db:
        upload = (await db.execute(select(Upload))).scalars().one()
    assert upload.validation_status == ValidationStatus.WARN.value


# ── Genuine 4xx cases ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_pd_upload_returns_400(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, b"lgd", kind=UploadKind.LGD.value, filename="lgd.xlsx")

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 400
    assert response.json()["code"] == "NO_PD_UPLOADS"


@pytest.mark.asyncio
async def test_unreadable_workbook_returns_422(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, b"this is not a workbook", filename="broken.xlsx")

    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "INVALID_FILE"
    assert "broken.xlsx" in body["detail"]


@pytest.mark.asyncio
async def test_unknown_run_returns_404(client: AsyncClient, ctx: _Ctx) -> None:
    response = await client.post(
        ctx.url(run_id=new_ulid()), json={"accepted_warning_ids": []}, headers=ctx.headers
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_run_from_another_tenant_returns_404(client: AsyncClient, ctx: _Ctx) -> None:
    other_tenant, other_run = new_ulid(), new_ulid()
    async with TestSessionLocal() as db:
        db.add(
            Tenant(id=other_tenant, name="Other", slug=f"o-{other_tenant[:8]}", currency="KES")
        )
        db.add(
            Run(
                id=other_run,
                tenant_id=other_tenant,
                created_by_user_id=ctx.user_id,
                name="Someone else's run",
                status="draft",
                engine_version="v1.2.0",
            )
        )
        await db.commit()

    response = await client.post(
        ctx.url(run_id=other_run), json={"accepted_warning_ids": []}, headers=ctx.headers
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected(client: AsyncClient, ctx: _Ctx) -> None:
    response = await client.post(ctx.url(), json={"accepted_warning_ids": []})
    assert response.status_code == 401


# ── One lane: there is no combined validate any more ─────────────────────────


@pytest.mark.asyncio
async def test_unscoped_validate_is_rejected(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))
    response = await client.post(
        ctx.url(scope=None), json={"accepted_warning_ids": []}, headers=ctx.headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SCOPE"


@pytest.mark.asyncio
async def test_lgd_scope_needs_an_lgd_upload(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))
    response = await client.post(
        ctx.url(scope="lgd"), json={"accepted_warning_ids": []}, headers=ctx.headers
    )
    assert response.status_code == 400
    assert response.json()["code"] == "NO_LGD_UPLOADS"


async def test_preview_carries_the_term_structure(client: AsyncClient, ctx: _Ctx) -> None:
    await _add_upload(ctx, _workbook(_healthy_rows()))
    response = await client.post(ctx.url(), json={"accepted_warning_ids": []}, headers=ctx.headers)
    assert response.status_code == 200
    segment = response.json()["data"]["pd_preview"]["segments"][0]
    assert len(segment["proportions"]) == 3 and len(segment["normalized"]) == 3
    assert segment["normalized"][2] == [0.0, 0.0, 1.0]
    assert [len(t) for t in segment["term_structure"]] == [299, 299, 299]
    assert all(len(row) == 4 for row in segment["term_structure"][0])


async def test_pd_export_needs_only_the_pd_upload(client: AsyncClient, ctx: _Ctx) -> None:
    from openpyxl import load_workbook

    await _add_upload(ctx, _workbook(_healthy_rows()))
    url = f"/api/v1/tenants/{ctx.tenant_id}/runs/{ctx.run_id}/pd/export"
    response = await client.get(url, headers=ctx.headers)
    assert response.status_code == 200, response.text
    wb = load_workbook(io.BytesIO(response.content))
    assert wb.sheetnames[:3] == ["Transition Matrix", "Normalized Matrix", "Dataset"]


async def test_pd_export_without_pd_upload_is_400(client: AsyncClient, ctx: _Ctx) -> None:
    url = f"/api/v1/tenants/{ctx.tenant_id}/runs/{ctx.run_id}/pd/export"
    response = await client.get(url, headers=ctx.headers)
    assert response.status_code == 400
