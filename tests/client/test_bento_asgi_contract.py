"""Run the client-facing routes against the real handoff Bento ASGI app.

This stays in-process so it exercises Bento's multipart/schema/middleware
boundary without claiming a physical GPU or requiring a listening socket.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import httpx


def test_agent_bento_asgi_contract(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    handoff = root / "runtime" / "bento"
    monkeypatch.syspath_prepend(str(handoff / "tests"))
    monkeypatch.syspath_prepend(str(handoff))
    monkeypatch.setenv("ZEYU_BENTO_TOKEN", "t" * 64)
    monkeypatch.setenv("ZEYU_GPU_LEASE_PATH", str(tmp_path / "gpu.lease"))
    monkeypatch.setenv("ZEYU_BENTO_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("ZEYU_BENTO_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("ZEYU_UNET_ADAPTER", "support_adapter:create_adapter")
    monkeypatch.setenv("ZEYU_TIGER_ADAPTER", "support_adapter:create_adapter")
    monkeypatch.setenv(
        "ZEYU_FABRIC_ROOT",
        str(root / "compute-fabric"),
    )

    from service import svc

    asyncio.run(_exercise(svc.to_asgi()))


async def _exercise(app):
    queue: asyncio.Queue[dict] = asyncio.Queue()
    events: list[dict] = []

    async def receive():
        return await queue.get()

    async def send(message):
        events.append(message)

    lifespan = asyncio.create_task(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}},
            receive,
            send,
        )
    )
    await queue.put({"type": "lifespan.startup"})
    for _ in range(200):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events and events[0]["type"] == "lifespan.startup.complete", events

    token = "t" * 64
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bento-fixture",
            headers={"Authorization": "Bearer " + token},
        ) as client:
            health = await client.post("/gpu_health", json={})
            assert health.status_code == 200, health.text

            run_id = "11111111-1111-4111-8111-111111111111"
            infer = await client.post(
                "/infer_audio",
                data={"model": "unet", "run_id": run_id},
                files={"audio": ("input.wav", b"RIFFcontract", "audio/wav")},
            )
            assert infer.status_code == 200, infer.text
            operation = json.loads(infer.headers["x-zeyu-operation"])
            assert operation["run_id"] == run_id
            assert operation["artifact_sha256"]

            release = await client.post("/release_model", json={})
            assert release.status_code == 200, release.text
    finally:
        await queue.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(lifespan, timeout=5)
        assert events[-1]["type"] == "lifespan.shutdown.complete", events
