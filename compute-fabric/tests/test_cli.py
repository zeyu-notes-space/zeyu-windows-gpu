import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import zipfile

from zeyu_fabric.cli import (Client, ClientError, OfflineError, load_config, main,
                             read_token, ssh_command, validate_url, verify_extract_bundle)


JOB = "11111111-1111-4111-8111-111111111111"
TOKEN = "0123456789abcdef" * 4


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}


def bundle_bytes(extra=None, inventory_transform=None, manifest_transform=None):
    manifest = {"job_id": JOB, "state": "COMPLETED"}
    if manifest_transform:
        manifest_transform(manifest)
    files = {
        "manifest.json": json.dumps(manifest).encode(),
        "environment.json": b'{"packages":[{"name":"torch","version":"2.8.0"}]}',
        "logs/stdout.log": "实际任务输出\n".encode(),
        "logs/stderr.log": b"",
        "metrics/samples.jsonl": b'{"cpu_percent":42}\n',
        "artifacts/result.txt": b"GPU result\n",
    }
    inventory = {"job_id": JOB, "files": [
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items()
    ]}
    if inventory_transform:
        inventory_transform(inventory)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("inventory.json", json.dumps(inventory))
        for name, data in extra or []:
            archive.writestr(name, data)
    return stream.getvalue()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token_path = self.root / "token"
        self.token_path.write_text(TOKEN)
        self.token_path.chmod(0o600)
        self.config = {"url": "http://127.0.0.1:8765", "token_file": str(self.token_path),
                       "ssh_host": "zeyu-worker", "local_port": 8765, "remote_port": 8765}

    def client(self):
        return Client(self.config, timeout=1)

    def test_only_exact_loopback_http_url_is_allowed(self):
        self.assertEqual(validate_url("http://127.0.0.1:8765/"), "http://127.0.0.1:8765")
        for value in ("http://localhost:8765", "http://127.1:8765", "http://2130706433:8765",
                      "https://127.0.0.1:8765", "http://127.0.0.1:8765/path", "http://127.0.0.1:8765?x=1",
                      "http://user:pass@127.0.0.1:8765", "http://127.0.0.1:8765#x", "http://192.0.2.1:8765",
                      "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:08765",
                      "http://127.0.0.1:8765\n", None):
            with self.subTest(url=value), self.assertRaises(ClientError):
                validate_url(value)

    def test_token_permissions_and_symlink_are_rejected(self):
        self.assertEqual(read_token(self.token_path), TOKEN)
        if os.name != "nt":
            self.token_path.chmod(0o644)
            with self.assertRaisesRegex(ClientError, "private"):
                read_token(self.token_path)
            self.token_path.chmod(0o600)
            link = self.root / "token-link"
            link.symlink_to(self.token_path)
            with self.assertRaisesRegex(ClientError, "regular"):
                read_token(link)

    def test_config_resolves_token_relative_to_config_file(self):
        config_path = self.root / "client.json"
        config_path.write_text(json.dumps({"url": self.config["url"], "token_file": "token"}))
        self.assertEqual(load_config(config_path)["token_file"], str(self.token_path.resolve()))

    def test_submit_retries_identical_request_after_connection_loss(self):
        client = self.client()
        result = {"job_id": JOB, "state": "QUEUED"}
        with mock.patch.object(client.opener, "open", side_effect=[
                urllib.error.URLError("interrupted"), Response(json.dumps(result).encode())]) as opened, \
                mock.patch("zeyu_fabric.cli.time.sleep"):
            self.assertEqual(client.submit({"command": ["{python}", "smoke.py"]}, "retry-key"), result)
        self.assertEqual(opened.call_count, 2)
        requests = [call.args[0] for call in opened.call_args_list]
        self.assertIs(requests[0], requests[1])
        self.assertEqual(requests[0].get_header("Idempotency-key"), "retry-key")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer " + TOKEN)

    def test_offline_error_never_claims_job_failed(self):
        client = self.client()
        with mock.patch.object(client.opener, "open", side_effect=urllib.error.URLError("offline")), \
                mock.patch("zeyu_fabric.cli.time.sleep"), self.assertRaises(OfflineError) as raised:
            client.submit({"command": ["x"]}, "recoverable-key")
        self.assertIn("does not cancel", str(raised.exception))
        self.assertNotIn(TOKEN, str(raised.exception))

    def test_logs_use_server_byte_offsets_and_drain_terminal_pages(self):
        client = self.client()
        sink = io.StringIO()
        with mock.patch.object(client, "json", side_effect=[
                {"text": "\ufffd", "next_offset": 1, "state": "RUNNING"},
                {"text": "中文", "next_offset": 7, "state": "COMPLETED"},
                {"text": "", "next_offset": 7, "state": "COMPLETED"}]) as requested:
            self.assertEqual(client.logs(JOB, follow=True, output=sink), 7)
        self.assertEqual(sink.getvalue(), "\ufffd中文")
        self.assertIn("offset=1&", requested.call_args_list[1].args[0])
        self.assertIn("offset=7&", requested.call_args_list[2].args[0])

    def test_offline_logs_report_exact_resume_offset(self):
        client = self.client()
        with mock.patch.object(client, "json", side_effect=[
                {"text": "abc", "next_offset": 503, "state": "RUNNING"}, OfflineError("offline")]), \
                self.assertRaisesRegex(OfflineError, "--offset 503 --follow"):
            client.logs(JOB, follow=True, offset=500, output=io.StringIO())

    def test_invalid_log_offset_does_not_loop(self):
        client = self.client()
        with mock.patch.object(client, "json", return_value={"text": "oops", "next_offset": 0, "state": "RUNNING"}), \
                self.assertRaisesRegex(ClientError, "without advancing"):
            client.logs(JOB, output=io.StringIO())

    def test_ssh_tunnel_has_strict_host_checking_and_loopback_binding(self):
        command = ssh_command(self.config)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("ExitOnForwardFailure=yes", command)
        self.assertIn("ServerAliveInterval=15", command)
        self.assertIn("127.0.0.1:8765:127.0.0.1:8765", command)
        self.assertIn("ForwardAgent=no", command)
        self.assertEqual(command[-1], "zeyu-worker")
        self.assertIn("-N", command)
        for host in ("-oProxyCommand=evil", "worker;evil", "user@worker", "worker command", "$(whoami)"):
            with self.subTest(host=host), self.assertRaises(ClientError):
                ssh_command(dict(self.config, ssh_host=host))
        with self.assertRaisesRegex(ClientError, "must match"):
            ssh_command(dict(self.config, local_port=8766))

    def test_submit_cli_emits_recoverable_key_on_unknown_result(self):
        config_path = self.root / "client.json"
        config_path.write_text(json.dumps(self.config))
        spec_path = self.root / "job.json"
        spec_path.write_text('{"command":["anything"]}')
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(Client, "submit", side_effect=OfflineError("offline")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--config", str(config_path), "submit", str(spec_path), "--idempotency-key", "fixed-key"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn('"idempotency_key": "fixed-key"', stderr.getvalue())
        self.assertNotIn(TOKEN, stderr.getvalue())

    def test_submit_wait_failure_has_nonzero_exit_code(self):
        config_path = self.root / "client.json"
        config_path.write_text(json.dumps(self.config))
        spec_path = self.root / "job.json"
        spec_path.write_text('{"command":["anything"]}')
        stdout = io.StringIO()
        with mock.patch.object(Client, "submit", return_value={"job_id": JOB, "state": "QUEUED"}), \
                mock.patch.object(Client, "wait", return_value={"job_id": JOB, "state": "FAILED", "exit_code": 1}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = main(["--config", str(config_path), "submit", str(spec_path), "--wait"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "FAILED")

    def write_bundle(self, content):
        path = self.root / "input.zip"
        path.write_bytes(content)
        return path

    def test_bundle_verifies_hashes_and_extracts_complete_run(self):
        destination = self.root / "verified"
        inventory = verify_extract_bundle(self.write_bundle(bundle_bytes()), destination, JOB)
        self.assertEqual(len(inventory["files"]), 6)
        self.assertEqual(json.loads((destination / "environment.json").read_text())["packages"][0]["name"], "torch")
        self.assertEqual((destination / "artifacts/result.txt").read_text(), "GPU result\n")
        self.assertTrue((destination / "inventory.json").is_file())
        self.assertTrue((destination / "metrics").is_dir())

    def test_archive_traversal_and_symlinks_are_rejected(self):
        for name in ("../outside.txt", "/tmp/outside.txt", "artifacts/../../outside.txt",
                     "artifacts\\outside.txt", "C:/outside.txt", "artifacts/./x", "artifacts//x",
                     "workspace/private.txt", "artifacts/x\x00ignored"):
            with self.subTest(name=name), self.assertRaises(ClientError):
                verify_extract_bundle(self.write_bundle(bundle_bytes(extra=[(name, b"bad")])), self.root / "extract", JOB)
        symlink = zipfile.ZipInfo("artifacts/link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ClientError, "symlink"):
            verify_extract_bundle(self.write_bundle(bundle_bytes(extra=[(symlink, b"../../outside")])), self.root / "extract", JOB)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_bundle_rejects_unlisted_files_and_case_collisions(self):
        with self.assertRaisesRegex(ClientError, "every file"):
            verify_extract_bundle(self.write_bundle(bundle_bytes(extra=[("artifacts/unlisted", b"x")])), self.root / "a", JOB)
        with self.assertRaisesRegex(ClientError, "case-colliding"):
            verify_extract_bundle(self.write_bundle(bundle_bytes(extra=[("artifacts/RESULT.TXT", b"x")])), self.root / "b", JOB)
        with self.assertRaisesRegex(ClientError, "case-colliding"):
            verify_extract_bundle(self.write_bundle(bundle_bytes(extra=[
                ("artifacts/Case/first", b"x"), ("artifacts/case/second", b"y")
            ])), self.root / "c", JOB)

    def test_bundle_rejects_hash_size_job_and_state_mismatches(self):
        transforms = [
            {"inventory_transform": lambda inv: inv["files"][0].update(sha256="0" * 64)},
            {"inventory_transform": lambda inv: inv["files"][0].update(size=999)},
            {"inventory_transform": lambda inv: inv.update(job_id="wrong")},
            {"manifest_transform": lambda manifest: manifest.update(job_id="wrong")},
            {"manifest_transform": lambda manifest: manifest.update(state="RUNNING")},
        ]
        for index, changes in enumerate(transforms):
            with self.subTest(changes=index), self.assertRaises(ClientError):
                verify_extract_bundle(self.write_bundle(bundle_bytes(**changes)), self.root / str(index), JOB)

    def test_artifact_download_retries_incomplete_stream_then_publishes(self):
        client = self.client()
        data = bundle_bytes()
        output = self.root / "runs"
        with mock.patch.object(client.opener, "open", side_effect=[Response(data[:20], len(data)), Response(data)]) as opened, \
                mock.patch("zeyu_fabric.cli.time.sleep"):
            result = client.artifacts(JOB, output)
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(result["verified"])
        self.assertEqual(list(output.iterdir()), [output / JOB])
        self.assertEqual((output / JOB / "artifacts/result.txt").read_text(), "GPU result\n")
        with self.assertRaisesRegex(ClientError, "already exists"):
            client.artifacts(JOB, output)

    def test_bad_bundle_is_never_published_and_partials_are_cleaned(self):
        client = self.client()
        data = bundle_bytes(inventory_transform=lambda inv: inv["files"][0].update(sha256="0" * 64))
        output = self.root / "runs"
        with mock.patch.object(client.opener, "open", return_value=Response(data)), self.assertRaisesRegex(ClientError, "SHA-256"):
            client.artifacts(JOB, output)
        self.assertEqual(list(output.iterdir()), [])

    def test_local_disk_error_is_not_retried_as_network_failure(self):
        client = self.client()
        with mock.patch.object(client.opener, "open", return_value=Response(b"x")) as opened, \
                self.assertRaisesRegex(OSError, "disk full"):
            client._retry(client._request("/v1/health"), mock.Mock(side_effect=OSError("disk full")))
        self.assertEqual(opened.call_count, 1)

    def test_real_http_submit_lost_ack_uses_one_key_and_creates_one_job(self):
        keys = []
        jobs = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Authorization") != "Bearer " + TOKEN:
                    self.send_error(401)
                    return
                key = self.headers.get("Idempotency-Key")
                keys.append(key)
                result = jobs.setdefault(key, {"job_id": JOB, "state": "QUEUED"})
                if len(keys) == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = json.dumps(result).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(dict(self.config, url="http://127.0.0.1:" + str(server.server_port)))
            with mock.patch("zeyu_fabric.cli.time.sleep"):
                self.assertEqual(client.submit({"command": ["smoke.py"]}, "real-retry")["job_id"], JOB)
            self.assertEqual(keys, ["real-retry", "real-retry"])
            self.assertEqual(len(jobs), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_redirect_is_rejected_without_sending_token_to_target(self):
        visited = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                visited.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:{}/token-trap".format(self.server.server_port))
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(dict(self.config, url="http://127.0.0.1:" + str(server.server_port)))
            with self.assertRaisesRegex(ClientError, "HTTP 302"):
                client.json("/v1/health")
            self.assertEqual(visited, ["/v1/health"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
