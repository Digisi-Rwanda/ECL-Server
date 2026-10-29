import pytest
from sqlalchemy import func, select

from app.modules.results.models import PdObservationSnapshot
from app.modules.results.pd_observation import persist_pd_observation_payload
from tests.conftest import TestSessionLocal


@pytest.mark.asyncio
async def test_concurrent_snapshot_writes_upsert_instead_of_failing() -> None:
    run_id, tenant_id = "01RUN000000000000000000000", "01TEN000000000000000000000"
    # Two sessions open at once mimic two overlapping PD tasks for one run.
    async with TestSessionLocal() as a, TestSessionLocal() as b:
        await persist_pd_observation_payload(
            a, run_id=run_id, tenant_id=tenant_id, engine_version="v1", payload={"n": 1}
        )
        await a.commit()
        await persist_pd_observation_payload(
            b, run_id=run_id, tenant_id=tenant_id, engine_version="v2", payload={"n": 2}
        )
        await b.commit()

    async with TestSessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(PdObservationSnapshot)) == 1
        row = await db.scalar(select(PdObservationSnapshot))
        assert (row.engine_version, row.payload) == ("v2", {"n": 2})
