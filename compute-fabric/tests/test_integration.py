"""Real Mac/POSIX subprocess integration; this is NOT Windows or GPU validation."""
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler
import uuid
import zipfile

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED"}
PROJECT = Path(__file__).resolve().parents[1]


class RealProcessIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zeyu-integration-")
        self.base = Path(self.tmp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "tracked.txt").write_text("immutable source\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=ZeYu Test", "-c", "user.email=local@example.invalid", "commit", "-qm", "test fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.token = uuid.uuid4().hex + uuid.uuid4().hex
        token_file = self.base / "auth.token"
        token_file.write_text(self.token)
        token_file.chmod(0o600)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.config = self.base / "worker.json"
        self.config.write_text(json.dumps({"root": str(self.base / "worker"),
            "host": "127.0.0.1", "port": self.port, "token_file": str(token_file),
            "projects": {"test": str(self.repo)},
            "environments": {"test": {"python": sys.executable}}, "metrics_interval": 0.1}))
        self.client_config = self.base / "client.json"
        self.client_config.write_text(json.dumps({"url": "http://127.0.0.1:" + str(self.port),
                                                   "token_file": str(token_file)}))
        self.url = "http://127.0.0.1:" + str(self.port)
        self.opener = build_opener(ProxyHandler({}))
        self.process = None
        self.output = (self.base / "server.log").open("ab")
        self.start_worker()

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.output.close()
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.STDOUT)

    def start_worker(self):
        self.process = subprocess.Popen([sys.executable, "-m", "zeyu_fabric.server", "--config", str(self.config)],
            cwd=str(PROJECT), stdout=self.output, stderr=self.output)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("worker exited: " + (self.base / "server.log").read_text())
            try:
                return self.request("GET", "/v1/health")
            except (OSError, URLError):
                time.sleep(0.05)
        self.fail("worker startup timeout")

    def request(self, method, path, body=None, key=None, token=True):
        headers = {"Authorization": "Bearer " + self.token} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key:
            headers["Idempotency-Key"] = key
        request = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method=method)
        with self.opener.open(request, timeout=20) as response:
            data = response.read()
            return data if path.endswith("/bundle") else json.loads(data)

    def spec(self, code, timeout=30, **changes):
        result = {"project": "test", "git_commit": self.commit,
            "environment": "test", "command": ["{python}", "-u", "-c", code],
            "arguments": [], "timeout": timeout, "artifact_paths": []}
        result.update(changes)
        return result

    def submit(self, spec, key=None):
        return self.request("POST", "/v1/jobs", spec, key or str(uuid.uuid4()))

    def wait(self, job_id, states=TERMINAL, limit=30):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            result = self.request("GET", "/v1/jobs/" + job_id)
            if result["state"] in states:
                return result
            time.sleep(0.05)
        self.fail("job did not reach " + str(states) + ": " + json.dumps(result))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeyu_fabric.cli", "--config", str(self.client_config), *args],
            cwd=str(PROJECT), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_full_smoke_cli_logs_metrics_and_verified_artifacts(self):
        code = "import os,pathlib,sys; print('计算成功',flush=True); print('diagnostic',file=sys.stderr); pathlib.Path(os.environ['ZRUN_ARTIFACT_DIR'],'result.txt').write_text(pathlib.Path('tracked.txt').read_text())"
        spec_path = self.base / "smoke.json"
        spec_path.write_text(json.dumps(self.spec(code)))
        submission = self.cli("submit", str(spec_path))
        self.assertEqual(submission.returncode, 0, submission.stderr)
        job = json.loads(submission.stdout)
        final = self.wait(job["job_id"])
        self.assertEqual(final["state"], "COMPLETED", final)
        status = self.cli("status", job["job_id"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "COMPLETED")
        logs = self.cli("logs", job["job_id"])
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertIn("计算成功", logs.stdout)
        stderr = self.request("GET", "/v1/jobs/" + job["job_id"] + "/logs?stream=stderr")
        self.assertIn("diagnostic", stderr["text"])
        workers = self.cli("workers")
        self.assertEqual(workers.returncode, 0, workers.stderr)
        destination = self.base / "download"
        fetch = self.cli("artifacts", job["job_id"], "--output", str(destination))
        self.assertEqual(fetch.returncode, 0, fetch.stderr)
        local = destination / job["job_id"]
        self.assertEqual((local / "artifacts/result.txt").read_text(), "immutable source\n")
        self.assertTrue((local / "metrics/samples.jsonl").is_file())
        self.assertTrue((local / "manifest.json").is_file())
        self.assertTrue((local / "environment.json").is_file())
        if os.environ.get("ZEYU_TEST_EVIDENCE_DIR"):
            evidence = Path(os.environ["ZEYU_TEST_EVIDENCE_DIR"]) / "local-smoke" / job["job_id"]
            evidence.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(local, evidence)
        inventory = json.loads((local / "inventory.json").read_text())
        for file in inventory["files"]:
            self.assertEqual(hashlib.sha256((local / file["path"]).read_bytes()).hexdigest(), file["sha256"])

    def test_real_exception_invalid_command_timeout_and_cancel(self):
        failure = self.submit(self.spec("raise RuntimeError('intentional-python-exception')"))
        failed = self.wait(failure["job_id"])
        self.assertEqual(failed["state"], "FAILED", failed)
        stderr = self.request("GET", "/v1/jobs/" + failure["job_id"] + "/logs?stream=stderr")
        self.assertIn("intentional-python-exception", stderr["text"])
        invalid = self.submit(self.spec("", command=["zeyu-definitely-missing-command-38292"]))
        self.assertEqual(self.wait(invalid["job_id"])["state"], "FAILED")
        timeout = self.submit(self.spec("import time; print('before-timeout',flush=True); time.sleep(30)", timeout=3))
        timed = self.wait(timeout["job_id"])
        self.assertEqual(timed["state"], "FAILED", timed)
        self.assertEqual(timed["failure"]["code"], "TIMEOUT")
        cancel = self.submit(self.spec("import time; time.sleep(30)"))
        self.wait(cancel["job_id"], {"RUNNING"})
        self.request("POST", "/v1/jobs/" + cancel["job_id"] + "/cancel", {})
        self.assertEqual(self.wait(cancel["job_id"])["state"], "CANCELLED")

    def test_connection_dropped_after_submit_does_not_duplicate_or_stop_job(self):
        key = str(uuid.uuid4())
        spec = self.spec("import time; print('survived disconnect',flush=True); time.sleep(2)")
        payload = json.dumps(spec).encode()
        head = ("POST /v1/jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + self.token +
                "\r\nContent-Type: application/json\r\nIdempotency-Key: " + key +
                "\r\nContent-Length: " + str(len(payload)) + "\r\nConnection: close\r\n\r\n").encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.sendall(head + payload)
            # Lose the response: the client cannot know whether submission committed.
        time.sleep(0.2)
        retried = self.submit(spec, key)
        self.assertEqual(self.wait(retried["job_id"])["state"], "COMPLETED")
        jobs = self.request("GET", "/v1/jobs")
        self.assertEqual(len(jobs), 1)

    def test_hard_worker_restart_preserves_queue_and_marks_interrupted(self):
        old_instance = self.request("GET", "/v1/health")["instance_id"]
        active = self.submit(self.spec("import time; print('active-before-crash',flush=True); time.sleep(60)", timeout=90))
        self.wait(active["job_id"], {"RUNNING"})
        queued = self.submit(self.spec("print('after-restart',flush=True)"))
        self.assertEqual(queued["state"], "QUEUED")
        self.process.kill()
        self.process.wait(timeout=5)
        health = self.start_worker()
        self.assertNotEqual(old_instance, health["instance_id"])
        interrupted = self.wait(active["job_id"])
        self.assertEqual(interrupted["state"], "FAILED", interrupted)
        self.assertEqual(interrupted["failure"]["code"], "WORKER_INTERRUPTED")
        self.assertEqual(self.wait(queued["job_id"])["state"], "COMPLETED")
        logs = self.request("GET", "/v1/jobs/" + active["job_id"] + "/logs")
        self.assertIn("active-before-crash", logs["text"])

    def test_auth_paths_idempotency_and_loopback(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("GET", "/v1/jobs", token=False)
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(HTTPError) as caught:
            self.request("GET", "/v1/jobs/../../worker.json")
        self.assertEqual(caught.exception.code, 404)
        key = str(uuid.uuid4())
        first = self.submit(self.spec("print('one')"), key)
        second = self.submit(self.spec("print('one')"), key)
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaises(HTTPError) as caught:
            self.submit(self.spec("print('different')"), key)
        self.assertIn(caught.exception.code, (400, 409))
        with self.assertRaises(HTTPError):
            self.submit(self.spec("pass", artifact_paths=["../secret"]))
        self.wait(first["job_id"])
        unsafe = json.loads(self.config.read_text())
        unsafe["host"] = "0.0.0.0"
        self.config.write_text(json.dumps(unsafe))
        from zeyu_fabric.server import load_config
        with self.assertRaises(ValueError):
            load_config(self.config)

    def test_live_utf8_split_write_preserves_bytes_and_final_drain(self):
        code = "import os,pathlib,time; os.write(1,bytes([228])); pathlib.Path(os.environ['ZRUN_ARTIFACT_DIR'],'partial').touch(); time.sleep(1); os.write(1,bytes([189,160])); print(' final',flush=True)"
        job = self.submit(self.spec(code))
        marker = self.base / "worker/runs" / job["job_id"] / "artifacts/partial"
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())
        partial = self.request("GET", "/v1/jobs/" + job["job_id"] + "/logs")
        self.assertEqual(partial["text"], "")
        self.assertEqual(partial["next_offset"], 0)
        self.wait(job["job_id"])
        complete = self.request("GET", "/v1/jobs/" + job["job_id"] + "/logs?offset=0&limit=1")
        self.assertEqual(complete["text"], "你")
        self.assertEqual(complete["next_offset"], 3)
        tail = self.request("GET", "/v1/jobs/" + job["job_id"] + "/logs?offset=3")
        self.assertEqual(tail["text"], " final\n")

    def test_timeout_kills_spawned_child_process(self):
        import psutil
        code = "import os,pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); pathlib.Path(os.environ['ZRUN_ARTIFACT_DIR'],'child.pid').write_text(str(p.pid)); time.sleep(90)"
        job = self.submit(self.spec(code, timeout=4))
        final = self.wait(job["job_id"])
        self.assertEqual(final["failure"]["code"], "TIMEOUT", final)
        pid_file = self.base / "worker/runs" / job["job_id"] / "artifacts/child.pid"
        self.assertTrue(pid_file.is_file())
        pid = int(pid_file.read_text())
        try:
            self.assertEqual(psutil.Process(pid).status(), psutil.STATUS_ZOMBIE)
        except psutil.NoSuchProcess:
            pass

    def test_durable_scheduler_failure_exits_nonzero_for_supervisor(self):
        import sqlite3
        db = sqlite3.connect(str(self.base / "worker/queue.sqlite3"))
        with db:
            db.execute("CREATE TRIGGER deny_start BEFORE UPDATE ON jobs WHEN NEW.state = 'STARTING' BEGIN SELECT RAISE(FAIL, 'injected durable write failure'); END")
        db.close()
        self.submit(self.spec("print('must not execute')"))
        self.process.wait(timeout=10)
        self.assertEqual(self.process.returncode, 1)
        self.assertIn("scheduler_failed", (self.base / "server.log").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
