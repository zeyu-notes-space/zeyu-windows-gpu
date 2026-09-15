import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from zeyu_fabric.engine import Engine, TERMINAL
from zeyu_fabric.gpu_lease import GpuLease, LeaseBusyError


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zeyu-engine-test-")
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.email", "tests@example.invalid")
        self.git("config", "user.name", "ZeYu Engine Tests")
        (self.repository / "smoke.py").write_text("import json, os, pathlib, sys\nprint('smoke stdout', flush=True)\nprint('smoke stderr', file=sys.stderr, flush=True)\npathlib.Path('result.txt').write_text('committed source')\npathlib.Path(os.environ['ZRUN_ARTIFACT_DIR'], 'direct.json').write_text(json.dumps({'job_id': os.environ['ZRUN_JOB_ID']}))\n", encoding="utf-8")
        (self.repository / "fail.py").write_text("from pathlib import Path\nPath('partial.txt').write_text('partial result')\nraise RuntimeError('deliberate test failure')\n", encoding="utf-8")
        (self.repository / "slow.py").write_text("import os, pathlib, subprocess, sys, time\np = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\npathlib.Path(os.environ['ZRUN_ARTIFACT_DIR'], 'child.txt').write_text(str(p.pid))\nprint('ready', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.config = {"root": str(self.base / "storage"), "projects": {"fixture": str(self.repository)}, "environments": {"test": {"python": sys.executable}}, "metrics_interval": 0.1, "allowed_job_env": ["EXPERIMENT_LABEL"]}
        self.engine = Engine(self.config)

    def tearDown(self):
        self.engine.stop()
        self.engine._db.close()
        self.temporary.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repository), *args], text=True, stderr=subprocess.STDOUT)

    def spec(self, script="smoke.py", **overrides):
        result = {"project": "fixture", "environment": "test", "git_commit": self.commit, "command": ["{python}", script], "timeout": 20, "artifact_paths": ["result.txt"]}
        result.update(overrides)
        return result

    def wait(self, job_id, states=TERMINAL, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            manifest = self.engine.get_job(job_id)
            if manifest["state"] in states:
                return manifest
            time.sleep(0.025)
        self.fail("Job did not reach expected state: " + repr(self.engine.get_job(job_id)))

    def submit(self, specification=None, key="test"):
        self.engine.start()
        return self.engine.submit(specification or self.spec(), key)["job_id"]

    def test_smoke_manifest_logs_artifacts_and_source_isolation(self):
        (self.repository / "smoke.py").write_text("raise RuntimeError('dirty working tree must not run')", encoding="utf-8")
        job_id = self.submit()
        result = self.wait(job_id)
        self.assertEqual(result["state"], "COMPLETED", result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual([event["state"] for event in result["history"]], ["QUEUED", "STARTING", "RUNNING", "COMPLETED"])
        self.assertEqual(result["source"]["git_commit"], self.commit)
        self.assertFalse(result["source"]["working_tree_used"])
        self.assertTrue(result["runtime"]["python_version"])
        self.assertEqual(len(result["runtime"]["dependency_identity_sha256"]), 64)
        self.assertGreater(result["duration_seconds"], 0)
        directory = self.engine.root / "runs" / job_id
        self.assertIn("smoke stdout", (directory / "logs" / "stdout.log").read_text())
        self.assertIn("smoke stderr", (directory / "logs" / "stderr.log").read_text())
        self.assertEqual((directory / "artifacts" / "result.txt").read_text(), "committed source")
        self.assertEqual(json.loads((directory / "artifacts" / "direct.json").read_text())["job_id"], job_id)
        artifacts = {item["path"]: item for item in result["artifacts"]}
        self.assertEqual(artifacts["result.txt"]["sha256"], hashlib.sha256(b"committed source").hexdigest())
        self.assertTrue((directory / "metrics" / "samples.jsonl").read_text().strip())
        self.assertIn("smoke stdout", (directory / "stdout.log").read_text())
        self.assertIn("smoke stderr", (directory / "stderr.log").read_text())
        metrics_summary = json.loads((directory / "metrics.json").read_text())
        self.assertEqual(metrics_summary["status"], "COMPLETED")
        self.assertGreaterEqual(metrics_summary["sample_count"], 1)
        self.assertEqual(json.loads((directory / "manifest.json").read_text()), result)
        self.assertIn("dirty working tree", (self.repository / "smoke.py").read_text())

    def test_python_exception_preserves_partial_artifact_and_traceback(self):
        job_id = self.submit(self.spec("fail.py", artifact_paths=["partial.txt"]))
        result = self.wait(job_id)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["failure"]["code"], "PYTHON_EXCEPTION")
        self.assertIn("deliberate test failure", result["failure"]["stderr_tail"])
        self.assertEqual(result["artifacts"][0]["path"], "partial.txt")

    def test_environment_identity_changes_with_settings_without_exporting_values(self):
        hidden_value = "private-config-value-must-not-be-exported"
        self.engine.config["environments"]["test"]["variables"] = {"OMP_NUM_THREADS": "1", "PRIVATE_TEST_KEY": hidden_value}
        first = self.wait(self.submit(key="environment-one"))
        self.engine.config["environments"]["test"]["variables"]["OMP_NUM_THREADS"] = "2"
        second = self.wait(self.submit(key="environment-two"))
        self.assertEqual(first["state"], "COMPLETED", first)
        self.assertEqual(second["state"], "COMPLETED", second)
        self.assertEqual(first["runtime"]["dependency_identity_sha256"], second["runtime"]["dependency_identity_sha256"])
        self.assertNotEqual(first["runtime"]["environment_identity_sha256"], second["runtime"]["environment_identity_sha256"])
        for manifest in (first, second):
            environment_text = (self.engine.root / "runs" / manifest["job_id"] / "environment.json").read_text()
            self.assertNotIn(hidden_value, environment_text)
            self.assertNotIn(hidden_value, json.dumps(manifest))
            self.assertIn("PRIVATE_TEST_KEY", manifest["runtime"]["configured_variable_names"])

    def test_invalid_command_has_diagnostic(self):
        result = self.wait(self.submit(self.spec(command=["zeyu-nonexistent-command-123456"])))
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["failure"]["code"], "COMMAND_NOT_FOUND")
        self.assertIsNone(result["exit_code"])

    def test_timeout_kills_descendant_and_captures_artifact(self):
        job_id = self.submit(self.spec("slow.py", timeout=2))
        result = self.wait(job_id)
        self.assertEqual(result["failure"]["code"], "TIMEOUT", result)
        self.assertEqual(result["state"], "FAILED")
        child = int((self.engine.root / "runs" / job_id / "artifacts" / "child.txt").read_text())
        self.assert_process_dead(child)

    def assert_process_dead(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    return
            except psutil.NoSuchProcess:
                return
            time.sleep(0.025)
        self.fail("Descendant survived termination: " + str(pid))

    def test_cancel_running_and_queued(self):
        queued = self.engine.submit(self.spec(), "queued")["job_id"]
        self.assertEqual(self.engine.cancel(queued)["state"], "CANCELLED")
        active = self.submit(self.spec("slow.py"), key="active")
        self.wait(active, {"RUNNING"})
        child_path = self.engine.root / "runs" / active / "artifacts" / "child.txt"
        deadline = time.monotonic() + 3
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        self.engine.cancel(active)
        result = self.wait(active)
        self.assertEqual(result["state"], "CANCELLED", result)
        self.assertEqual(result["failure"]["code"], "CANCELLED")
        self.assert_process_dead(int(child_path.read_text()))

    def test_idempotency_and_conflict(self):
        first = self.engine.submit(self.spec(), "stable-key")
        second = self.engine.submit(self.spec(), "stable-key")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.engine.list_jobs()), 1)
        with self.assertRaisesRegex(RuntimeError, "different specification"):
            self.engine.submit(self.spec(arguments=["changed"]), "stable-key")

    def test_invalid_specs(self):
        changes = [{"git_commit": "HEAD"}, {"timeout": 0}, {"timeout": float("nan")}, {"timeout": True}, {"command": "python smoke.py"}, {"artifact_paths": ["../secret"]}, {"artifact_paths": ["C:/secret"]}, {"artifact_paths": ["foo\\bar"]}, {"resources": {"gpu": 1}}, {"resources": {"min_ram_mb": -1}}, {"env": {"PATH": "evil"}}, {"typo": 1}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.engine.submit(self.spec(**change), "invalid")

    def test_missing_git_commit_fails_visibly(self):
        result = self.wait(self.submit(self.spec(git_commit="f" * 40)))
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["failure"]["code"], "PREPARATION_FAILED")
        self.assertTrue(result["failure"]["stderr"])

    @unittest.skipIf(os.name == "nt", "Creating symlinks may require additional Windows privileges")
    def test_git_symlink_is_rejected(self):
        (self.repository / "link").symlink_to("smoke.py")
        self.git("add", "link")
        self.git("commit", "--quiet", "-m", "add unsupported link")
        result = self.wait(self.submit(self.spec(git_commit=self.git("rev-parse", "HEAD").strip())))
        self.assertEqual(result["failure"]["code"], "UNSUPPORTED_SOURCE_ENTRY")

    def test_recovery_keeps_queue_and_does_not_repeat_interrupted_job(self):
        interrupted = self.engine.submit(self.spec(), "interrupted")["job_id"]
        queued = self.engine.submit(self.spec(), "queued")["job_id"]
        self.engine._update(interrupted, "STARTING", started_at="2026-01-01T00:00:00+00:00")
        self.engine._db.close()
        self.engine = Engine(self.config)
        self.engine.start()
        recovered = self.engine.get_job(interrupted)
        self.assertEqual(recovered["state"], "FAILED")
        self.assertEqual(recovered["failure"]["code"], "WORKER_INTERRUPTED")
        self.assertEqual(self.wait(queued)["state"], "COMPLETED")
        self.assertNotIn("RUNNING", [item["state"] for item in recovered["history"]])

    def test_single_worker_storage_lock(self):
        self.engine.start()
        second = Engine(self.config)
        try:
            with self.assertRaisesRegex(RuntimeError, "already owns"):
                second.start()
        finally:
            second.stop()
            second._db.close()

    def test_recovery_refuses_to_dispatch_when_cleanup_is_unverified(self):
        interrupted = self.engine.submit(self.spec(), "interrupted")["job_id"]
        queued = self.engine.submit(self.spec(), "queued")["job_id"]
        self.engine._update(interrupted, "STARTING", started_at="2026-01-01T00:00:00+00:00", process={"pid": 12345, "create_time": 12345})
        with patch("zeyu_fabric.engine.psutil.Process", side_effect=psutil.AccessDenied(12345)):
            with self.assertRaisesRegex(RuntimeError, "cleanup is verified"):
                self.engine.start()
        self.assertEqual(self.engine.get_job(queued)["state"], "QUEUED")
        self.assertEqual(self.engine.health()["status"], "degraded")

    @unittest.skipIf(os.name == "nt", "Creating symlinks may require additional Windows privileges")
    def test_artifact_symlink_escape_is_rejected(self):
        script = "import os, pathlib; pathlib.Path(os.environ['ZRUN_ARTIFACT_DIR'], 'escape').symlink_to('/etc/hosts')"
        result = self.wait(self.submit(self.spec(command=["{python}", "-c", script])))
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["failure"]["code"], "ARTIFACT_COLLECTION_FAILED")

    def test_gpu_oom_classification_is_explicitly_inferred(self):
        job_id = self.engine.submit(self.spec(), "oom-classifier")["job_id"]
        run_dir = self.engine.root / "runs" / job_id
        (run_dir / "logs" / "stderr.log").write_text("torch.OutOfMemoryError: CUDA out of memory.\n", encoding="utf-8")
        diagnosis = self.engine._diagnose_exit(run_dir, 1)
        self.assertEqual(diagnosis["code"], "GPU_OUT_OF_MEMORY")
        self.assertEqual(diagnosis["classification"], "inferred_from_stderr")

    def test_gpu_job_respects_shared_runtime_lease(self):
        lease_path = self.base / "gpu-coordination" / "gpu-exclusive.lock"
        lease_path.parent.mkdir()
        self.engine.config["gpu_lease_path"] = str(lease_path)
        holder = GpuLease(lease_path, {"component": "GPU_RUNTIME", "test": True})
        holder.acquire()
        try:
            result = self.wait(self.submit(self.spec(resources={"gpu": True}), key="gpu-lease"))
        finally:
            holder.release()
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(result["failure"]["code"], "GPU_BUSY")
        self.assertEqual(result["failure"]["owner"]["component"], "GPU_RUNTIME")

    def test_gpu_lease_stale_held_marker_fails_closed(self):
        lease_path = self.base / "stale-gpu.lock"
        lease_path.write_text('0\n{"component":"GPU_RUNTIME","state":"HELD","pid":999}\n', encoding="utf-8")
        with self.assertRaises(LeaseBusyError) as raised:
            GpuLease(lease_path, {"component": "COMPUTE_FABRIC"}).acquire()
        self.assertTrue(raised.exception.stale)

    def test_shutdown_active_job_and_restart_queue(self):
        active = self.submit(self.spec("slow.py"), key="active")
        self.wait(active, {"RUNNING"})
        queued = self.engine.submit(self.spec(), "queued")["job_id"]
        self.engine.stop()
        self.assertEqual(self.engine.get_job(active)["failure"]["code"], "WORKER_STOPPED")
        self.assertEqual(self.engine.get_job(queued)["state"], "QUEUED")
        self.engine.start()
        self.assertEqual(self.wait(queued)["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
