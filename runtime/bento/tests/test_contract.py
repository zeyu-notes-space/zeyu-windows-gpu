from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


HANDOFF = Path(__file__).resolve().parents[1]
REPO = HANDOFF.parents[1]
sys.path.insert(0, str(HANDOFF))
sys.path.insert(0, str(HANDOFF / "tests"))
sys.path.insert(0, str(REPO / "compute-fabric" / "zeyu_fabric"))

from gpu_lease import GpuLease  # noqa: E402
from service import (  # noqa: E402
    HonestTimeoutMiddleware,
    ZeYuBentoError,
    svc,
)
from support_adapter import FakeAdapter, reset_counts  # noqa: E402


class _Request:
    def __init__(self, token: str | None = "secret"):
        self.headers = {} if token is None else {"authorization": f"Bearer {token}"}


class _Response:
    def __init__(self):
        self.headers = {}


class _Context:
    def __init__(self, token: str | None = "secret"):
        self.request = _Request(token)
        self.response = _Response()


class BentoContractTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.lease_path = self.root / "gpu.lease"
        self.audio_path = self.root / "input.wav"
        self.audio_path.write_bytes(b"contract-input")
        self.env = mock.patch.dict(
            os.environ,
            {
                "ZEYU_BENTO_TOKEN": "secret",
                "ZEYU_GPU_LEASE_PATH": str(self.lease_path),
                "ZEYU_FABRIC_ROOT": str(REPO / "compute-fabric"),
                "ZEYU_UNET_ADAPTER": "support_adapter:create_adapter",
                "ZEYU_TIGER_ADAPTER": "support_adapter:create_adapter",
                "ZEYU_BENTO_ARTIFACT_ROOT": str(self.root / "artifacts"),
                "ZEYU_BENTO_TIMEOUT_SECONDS": "0.05",
            },
            clear=False,
        )
        self.env.start()
        reset_counts()

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def test_schema_exposes_exact_routes_and_file_fields(self):
        schema = svc.schema()
        routes = {route["name"]: route for route in schema["routes"]}
        self.assertEqual(
            {"gpu_health", "infer_audio", "release_model"}, set(routes)
        )
        infer_input = routes["infer_audio"]["input"]
        self.assertEqual(infer_input["properties"]["model"]["enum"], ["unet", "tiger"])
        self.assertEqual(infer_input["properties"]["audio"]["type"], "file")
        self.assertIn("run_id", infer_input["properties"])
        self.assertIn("run_id", infer_input["required"])

    def test_gpu_health_is_cpu_only_and_does_not_load_a_model(self):
        instance = svc()
        result = instance.gpu_health(_Context())
        self.assertFalse(result["model_loaded"])
        self.assertEqual(result["cuda_probe"], "deferred")
        self.assertIsNone(instance._adapter)

    def test_missing_bearer_token_is_rejected(self):
        instance = svc()
        with self.assertRaises(ZeYuBentoError) as caught:
            instance.gpu_health(_Context(token=None))
        self.assertEqual(caught.exception.code, "AUTH_REQUIRED")
        self.assertEqual(caught.exception.status_code, 401)

    def test_protected_token_file_takes_precedence_over_inline_fallback(self):
        token_file = self.root / "worker.token"
        token_file.write_text("file-secret\n", encoding="utf-8")
        os.environ["ZEYU_BENTO_TOKEN_FILE"] = str(token_file)
        os.environ["ZEYU_BENTO_TOKEN"] = "wrong-inline-value"
        instance = svc()
        self.assertTrue(instance.gpu_health(_Context(token="file-secret"))["ok"])
        with self.assertRaises(ZeYuBentoError) as caught:
            instance.gpu_health(_Context(token="wrong-inline-value"))
        self.assertEqual(caught.exception.code, "AUTH_REQUIRED")

    def test_infer_resident_model_reuses_lease_and_returns_metadata(self):
        instance = svc()
        run_id = "11111111-1111-4111-8111-111111111111"
        context = _Context()
        output = instance.infer_audio(self.audio_path, "unet", run_id, context)
        self.assertTrue(output.exists())
        operation = json.loads(context.response.headers["X-ZeYu-Operation"])
        self.assertEqual(operation["run_id"], run_id)
        self.assertEqual(operation["model"]["alias"], "unet")
        self.assertEqual(operation["model"]["checkpoint_sha256"], "b" * 64)
        self.assertEqual(operation["model"]["git_commit"], "a" * 40)
        self.assertEqual(operation["artifact_sha256"], operation["artifact"]["sha256"])
        self.assertGreaterEqual(operation["timing"]["total_wall_ms"], 0)
        self.assertEqual(FakeAdapter.infer_count, 1)

        instance.infer_audio(
            self.audio_path,
            "unet",
            "22222222-2222-4222-8222-222222222222",
            _Context(),
        )
        self.assertEqual(FakeAdapter.infer_count, 2)
        self.assertEqual(instance._model_alias, "unet")

        release = instance.release_model(_Context())
        self.assertTrue(release["released"])
        self.assertIsNone(instance._adapter)
        self.assertEqual(FakeAdapter.close_count, 1)
        self.assertEqual(instance._lease, None)

    def test_different_model_is_busy_until_release(self):
        instance = svc()
        instance.infer_audio(
            self.audio_path,
            "unet",
            "33333333-3333-4333-8333-333333333333",
            _Context(),
        )
        with self.assertRaises(ZeYuBentoError) as caught:
            instance.infer_audio(
                self.audio_path,
                "tiger",
                "44444444-4444-4444-8444-444444444444",
                _Context(),
            )
        self.assertEqual(caught.exception.code, "MODEL_BUSY")
        self.assertEqual(caught.exception.status_code, 409)
        instance.shutdown()

    def test_cross_process_lease_busy_is_structured(self):
        held = GpuLease(self.lease_path, {"owner": "test-holder"}).acquire()
        try:
            instance = svc()
            with self.assertRaises(ZeYuBentoError) as caught:
                instance.infer_audio(
                    self.audio_path,
                    "unet",
                    "55555555-5555-4555-8555-555555555555",
                    _Context(),
                )
            self.assertEqual(caught.exception.code, "GPU_BUSY")
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(
                caught.exception.run_id,
                "55555555-5555-4555-8555-555555555555",
            )
        finally:
            held.release()

    def test_model_load_failure_keeps_lease_when_cleanup_cannot_be_proven(self):
        os.environ["ZEYU_UNET_ADAPTER"] = "support_adapter:create_factory_failure"
        instance = svc()
        with self.assertRaises(ZeYuBentoError) as caught:
            instance.infer_audio(
                self.audio_path,
                "unet",
                "66666666-6666-4666-8666-666666666666",
                _Context(),
            )
        self.assertEqual(caught.exception.code, "MODEL_LOAD_FAILED")
        self.assertEqual(instance._model_state, "load_failed")
        self.assertIsNotNone(instance._lease)

        with self.assertRaises(ZeYuBentoError) as unload:
            instance.release_model(_Context())
        self.assertEqual(unload.exception.code, "MODEL_UNLOAD_FAILED")
        self.assertIsNotNone(instance._lease)
        # The production supervisor owns the verified process-tree recovery
        # path for this state; close the test handle so the fixture is clean.
        instance._lease.release()
        instance._lease = None

    def test_honest_timeout_returns_without_cancelling_work(self):
        sent = []
        finished = threading.Event()

        async def app(scope, receive, send):
            await asyncio.to_thread(lambda: (time.sleep(0.12), finished.set()))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"late"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        async def exercise():
            middleware = HonestTimeoutMiddleware(app, timeout=0.01)
            await middleware({"type": "http", "path": "/infer_audio"}, receive, send)
            await asyncio.sleep(0.15)

        asyncio.run(exercise())
        self.assertTrue(finished.is_set())
        self.assertEqual(sent[0]["status"], 504)
        body = json.loads(sent[1]["body"])
        self.assertEqual(body["error_code"], "SERVICE_TIMEOUT")
        self.assertIn("worker continues", body["message"])


if __name__ == "__main__":
    unittest.main()
