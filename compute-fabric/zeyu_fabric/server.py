"""Private loopback HTTP transport. Put SSH in front; never expose this port."""
import argparse
import codecs
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__

TERMINAL = frozenset(("COMPLETED", "FAILED", "CANCELLED"))
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_BODY = 1024 * 1024


def read_token(path):
    path = Path(path).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ValueError("token_file must be an existing regular file")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("token_file must be private (chmod 600)")
    token = path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("token must contain at least 32 non-whitespace characters")
    return token


def load_config(path):
    config = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError("worker config must be an object")
    if config.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("worker only supports host 127.0.0.1; use SSH forwarding")
    port = config.get("port", 8765)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("port must be 1..65535")
    for name in ("root", "token_file"):
        if not isinstance(config.get(name), str) or not Path(config[name]).is_absolute():
            raise ValueError(name + " must be an absolute path")
    for name in ("projects", "environments"):
        if not isinstance(config.get(name), dict) or not config[name]:
            raise ValueError(name + " must be a nonempty mapping")
    for alias, repo in config["projects"].items():
        if not isinstance(repo, str) or not Path(repo).is_absolute():
            raise ValueError("project path must be absolute: " + alias)
    for alias, env in config["environments"].items():
        if not isinstance(env, dict) or not Path(env.get("python", "")).is_absolute():
            raise ValueError("environment python must be absolute: " + alias)
    interval = config.get("metrics_interval", 2.0)
    if not isinstance(interval, (int, float)) or not 0.1 <= interval <= 60:
        raise ValueError("metrics_interval must be 0.1..60 seconds")
    return config


def build_bundle(root, job_id):
    """Take a terminal run snapshot with a content inventory, excluding source."""
    run = Path(root) / "runs" / job_id
    if run.is_symlink():
        raise ValueError("run directory cannot be a symlink")
    inventory = {"job_id": job_id, "files": []}
    output = tempfile.TemporaryFile(mode="w+b")
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            candidates = [run / name for name in ("manifest.json", "environment.json", "stdout.log", "stderr.log", "metrics.json")]
            for name in ("logs", "metrics", "artifacts"):
                directory = run / name
                if directory.is_symlink():
                    raise ValueError("bundle contains symlink directory")
                if directory.exists():
                    candidates.extend(sorted(directory.rglob("*")))
            for path in candidates:
                if path.is_symlink():
                    raise ValueError("bundle contains symlink: " + str(path.relative_to(run)))
                if not path.is_file():
                    continue
                resolved = path.resolve()
                resolved.relative_to(run.resolve())
                rel = path.relative_to(run).as_posix()
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as source, archive.open(rel, "w", force_zip64=True) as sink:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        sink.write(chunk)
                inventory["files"].append({"path": rel, "size": size, "sha256": digest.hexdigest()})
            archive.writestr("inventory.json", json.dumps(inventory, sort_keys=True, indent=2))
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise


class FabricServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine, token):
        if address[0] != "127.0.0.1":
            raise ValueError("only loopback binding is permitted")
        self.engine = engine
        self.token = token
        self.instance_id = str(uuid.uuid4())
        self.started = time.time()
        self.bundle_slots = threading.BoundedSemaphore(1)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "ZeYuFabric/" + __version__
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, fmt, *args):
        # Request lines can contain user data. Keep transport logs minimal.
        return

    def send_json(self, code, body):
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def authenticated(self):
        actual = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(actual.encode(), expected.encode()):
            self.send_json(401, {"error": "authentication required"})
            return False
        if self.headers.get("Origin"):
            self.send_json(403, {"error": "browser origin requests are not supported"})
            return False
        return True

    def dispatch(self, method):
        try:
            if not self.authenticated():
                return
            parsed = urlsplit(self.path)
            path = parsed.path.rstrip("/")
            engine = self.server.engine
            if method == "GET" and path == "/v1/health":
                health = engine.health()
                return self.send_json(200 if health["status"] == "ok" else 503, {**health, "version": __version__,
                    "instance_id": self.server.instance_id, "pid": os.getpid(),
                    "started_at_unix": self.server.started})
            if method == "GET" and path == "/v1/workers":
                return self.send_json(200, engine.workers())
            if path == "/v1/jobs":
                if method == "GET":
                    return self.send_json(200, engine.list_jobs())
                if method == "POST":
                    if engine.health()["status"] != "ok":
                        return self.send_json(503, {"error": "scheduler is not running; inspect worker logs"})
                    key = self.headers.get("Idempotency-Key", "")
                    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
                        raise ValueError("Idempotency-Key must be 1..128 safe characters")
                    spec = self.read_json()
                    return self.send_json(201, engine.submit(spec, key))
            parts = path.split("/")
            if len(parts) not in (4, 5) or parts[1:3] != ["v1", "jobs"] or not JOB_ID.fullmatch(parts[3]):
                return self.send_json(404, {"error": "endpoint not found"})
            job_id = parts[3]
            manifest = engine.get_job(job_id)
            if len(parts) == 4 and method == "GET":
                return self.send_json(200, manifest)
            if len(parts) == 5:
                if method == "POST" and parts[4] == "cancel":
                    return self.send_json(200, engine.cancel(job_id))
                if method == "GET" and parts[4] == "logs":
                    return self.logs(job_id, parse_qs(parsed.query))
                if method == "GET" and parts[4] == "bundle":
                    if manifest["state"] not in TERMINAL:
                        raise RuntimeError("run is not terminal; retry after completion")
                    return self.bundle(job_id)
            self.send_json(404, {"error": "endpoint not found"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except KeyError:
            self.send_json(404, {"error": "job not found"})
        except (ValueError, TypeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.send_json(409, {"error": str(exc)})
        except Exception as exc:
            # Exception type is useful without leaking a config or token.
            print("request error: " + type(exc).__name__, flush=True)
            self.send_json(500, {"error": "worker internal error: " + type(exc).__name__})

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("chunked request bodies are unsupported")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("JSON body must be 1..1048576 bytes")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("incomplete request body")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("job must be a JSON object")
        return value

    def logs(self, job_id, query):
        stream = query.get("stream", ["stdout"])[0]
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream must be stdout or stderr")
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["65536"])[0])
        if offset < 0 or not 1 <= limit <= 1024 * 1024:
            raise ValueError("invalid offset or limit")
        # Read state first: terminal implies the writer has already finished.
        # Reading state after EOF could otherwise miss the last write in a race.
        state = self.server.engine.get_job(job_id)["state"]
        path = self.server.engine.root / "runs" / job_id / "logs" / (stream + ".log")
        data = b""
        rendered = ""
        pending = b""
        if path.exists():
            if path.is_symlink():
                raise ValueError("log cannot be a symlink")
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit)
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                rendered = decoder.decode(data, final=False)
                # Complete a character crossing a requested page boundary.
                for _ in range(3):
                    if not decoder.getstate()[0]:
                        break
                    extra = handle.read(1)
                    if not extra:
                        break
                    data += extra
                    rendered += decoder.decode(extra, final=False)
                pending = decoder.getstate()[0]
                if pending and state in TERMINAL:
                    rendered += decoder.decode(b"", final=True)
                    pending = b""
        self.send_json(200, {"text": rendered,
                             "next_offset": offset + len(data) - len(pending), "state": state})

    def bundle(self, job_id):
        if not self.server.bundle_slots.acquire(blocking=False):
            raise RuntimeError("another bundle is being exported; retry shortly")
        try:
            with build_bundle(self.server.engine.root, job_id) as handle:
                size = handle.seek(0, 2)
                handle.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="' + job_id + '.zip"')
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(handle, self.wfile, 1024 * 1024)
        finally:
            self.server.bundle_slots.release()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeYu private Windows compute worker")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        token = read_token(config["token_file"])
        from .engine import Engine
        engine = Engine(config)
        server = FabricServer(("127.0.0.1", config.get("port", 8765)), engine, token)
        engine.start()
    except Exception as exc:
        parser.exit(2, "Worker startup failed: " + str(exc) + "\n")
    shutting_down = threading.Event()
    fatal_error = threading.Event()

    def shutdown(signum, frame):
        if not shutting_down.is_set():
            shutting_down.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    def monitor_scheduler():
        while not shutting_down.wait(1):
            health = engine.health()
            if health["status"] != "ok":
                print(json.dumps({"worker": "scheduler_failed", "health": health}), flush=True)
                fatal_error.set()
                shutdown(None, None)
                return

    monitor = threading.Thread(target=monitor_scheduler, name="scheduler-watchdog", daemon=True)
    monitor.start()
    print(json.dumps({"worker": "ready", "url": "http://127.0.0.1:" + str(server.server_port),
                      "instance_id": server.instance_id}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        shutting_down.set()
        server.server_close()
        engine.stop()
    return 1 if fatal_error.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
