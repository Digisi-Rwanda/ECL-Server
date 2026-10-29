"""Post a validation and wait for its background job, returning a response-like result."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from httpx import AsyncClient


@dataclass
class JobResponse:
    status_code: int
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body

    @property
    def text(self) -> str:
        return str(self.body)


async def post_validate(client: AsyncClient, url: str, **kwargs: Any) -> JobResponse:
    started = await client.post(url, **kwargs)
    if started.status_code != 202:
        return JobResponse(started.status_code, started.json())
    job_id = started.json()["data"]["jobId"]
    base = url.split("/validate")[0]
    for _ in range(600):
        polled = await client.get(f"{base}/validation-jobs/{job_id}", headers=kwargs.get("headers"))
        job = polled.json()["data"]
        if job["status"] == "complete":
            return JobResponse(200, {"data": job["result"]})
        if job["status"] == "error":
            err = job["error"]
            return JobResponse(err["status"], {"code": err["code"], "detail": err["detail"]})
        await asyncio.sleep(0.05)
    raise AssertionError("validation job did not finish")
