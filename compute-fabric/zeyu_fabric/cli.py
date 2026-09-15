"""Mac control plane: a private SSH tunnel and a small, resumable HTTP client."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile


TERMINAL = frozenset(("COMPLETED", "FAILED", "CANCELLED"))
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
DEFAULT_CONFIG = "~/.config/zeyu-fabric/client.json"
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError)
CHUNK = 1024 * 1024


class ClientError(Exception):
    """An actionable error that is safe to display without a traceback."""


class OfflineError(ClientError):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a private bearer token to a different endpoint.
        return None


def validate_url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ClientError("url must be http://127.0.0.1:PORT")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ClientError("url must be http://127.0.0.1:PORT") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not port or parsed.netloc != "127.0.0.1:" + str(port)):
        raise ClientError("url must be http://127.0.0.1:PORT; use zrun connect for SSH forwarding")
    return "http://127.0.0.1:" + str(port)


def load_config(path):
    source = Path(path).expanduser()
    config = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ClientError("client config must be a JSON object")
    config["url"] = validate_url(config.get("url"))
    token = config.get("token_file")
    if not isinstance(token, str) or not token:
        raise ClientError("client config requires token_file")
    token_path = Path(token).expanduser()
    if not token_path.is_absolute():
        token_path = source.resolve().parent / token_path
    config["token_file"] = str(token_path)
    return config


def read_token(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ClientError("token_file must be an existing regular file")
    info = path.stat()
    if os.name != "nt":
        if stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
            raise ClientError("token_file must be owned by you and private (chmod 600)")
    token = path.read_text(encoding="utf-8-sig").strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ClientError("token must contain at least 32 non-whitespace characters")
    return token


def checked_id(value):
    if not isinstance(value, str) or not JOB_ID.fullmatch(value):
        raise ClientError("job ID must be a lowercase UUID")
    return value


class Client:
    def __init__(self, config, timeout=15, attempts=3):
        self.url = validate_url(config.get("url"))
        self.token = read_token(config["token_file"])
        self.timeout = timeout
        self.attempts = attempts
        # Local requests must not traverse HTTP_PROXY or a system proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def _request(self, path, data=None, key=None):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        body = None
        if data is not None:
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(body) > CHUNK:
                raise ClientError("request JSON exceeds 1 MiB")
            headers["Content-Type"] = "application/json"
        if key is not None:
            if not isinstance(key, str) or not KEY.fullmatch(key):
                raise ClientError("idempotency key must be 1..128 letters, digits, or ._:-")
            headers["Idempotency-Key"] = key
        return urllib.request.Request(self.url + path, data=body, headers=headers)

    def _http_error(self, exc):
        try:
            content = json.loads(exc.read(65536))
            detail = str(content.get("error", "request rejected")) if isinstance(content, dict) else "request rejected"
        except (ValueError, OSError):
            detail = "request rejected"
        finally:
            exc.close()
        detail = detail.replace(self.token, "[redacted]")
        return ClientError("Worker HTTP {}: {}".format(exc.code, detail))

    def _retry(self, request, consume):
        for attempt in range(self.attempts):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    return consume(response)
            except urllib.error.HTTPError as exc:
                if exc.code not in (500, 502, 503, 504) or attempt + 1 == self.attempts:
                    raise self._http_error(exc) from exc
                exc.close()
            except NETWORK_ERRORS as exc:
                if attempt + 1 == self.attempts:
                    raise OfflineError("Worker unreachable or connection interrupted; check zrun connect and zrun doctor. "
                                       "A connection failure does not cancel a submitted job.") from exc
            time.sleep(min(0.5 * (2 ** attempt), 2))

    def json(self, path, data=None, key=None):
        request = self._request(path, data, key)

        def consume(response):
            raw = response.read(16 * CHUNK + 1)
            if len(raw) > 16 * CHUNK:
                raise ClientError("worker JSON response exceeds 16 MiB")
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ClientError("worker returned invalid JSON") from exc

        return self._retry(request, consume)

    def submit(self, spec, key):
        if not isinstance(spec, dict):
            raise ClientError("job specification must be a JSON object")
        return self.json("/v1/jobs", spec, key)

    def status(self, job_id=None):
        return self.json("/v1/jobs" + ("/" + checked_id(job_id) if job_id else ""))

    def wait(self, job_id, interval=1):
        while True:
            result = self.status(job_id)
            if result.get("state") in TERMINAL:
                return result
            time.sleep(interval)

    def logs(self, job_id, stream="stdout", follow=False, offset=0, output=None):
        checked_id(job_id)
        if stream not in ("stdout", "stderr") or type(offset) is not int or offset < 0:
            raise ClientError("invalid log stream or byte offset")
        output = output or sys.stdout
        while True:
            try:
                page = self.json("/v1/jobs/{}/logs?{}".format(job_id, urllib.parse.urlencode(
                    {"stream": stream, "offset": offset, "limit": 65536})))
            except OfflineError as exc:
                raise OfflineError(str(exc) + " Resume logs with --offset {}{}".format(
                    offset, " --follow" if follow else "")) from exc
            next_offset = page.get("next_offset")
            if (type(next_offset) is not int or next_offset < offset
                    or not isinstance(page.get("text"), str)):
                raise ClientError("worker returned invalid log offsets or text")
            if next_offset == offset and page["text"]:
                raise ClientError("worker returned log text without advancing its byte offset")
            output.write(page["text"])
            output.flush()
            if next_offset == offset:
                if not follow or page.get("state") in TERMINAL:
                    return offset
                time.sleep(1)
            offset = next_offset

    def artifacts(self, job_id, output):
        checked_id(job_id)
        parent = Path(output).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        target = parent / job_id
        if target.exists() or target.is_symlink():
            raise ClientError("artifact destination already exists: " + str(target))
        # A partial download and extraction stay on the destination filesystem.
        with tempfile.TemporaryDirectory(prefix="." + job_id + ".partial-", dir=str(parent)) as scratch:
            scratch = Path(scratch)
            archive_path = scratch / "bundle.zip"

            def consume(response):
                declared = response.headers.get("Content-Length")
                try:
                    expected = int(declared) if declared is not None else None
                    if expected is not None and expected < 0:
                        raise ValueError()
                except ValueError as exc:
                    raise ClientError("invalid bundle Content-Length") from exc
                size = 0
                with archive_path.open("wb") as sink:
                    while True:
                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        sink.write(chunk)
                        size += len(chunk)
                if expected is not None and size != expected:
                    raise http.client.IncompleteRead(b"", expected - size)

            self._retry(self._request("/v1/jobs/" + job_id + "/bundle"), consume)
            extracted = scratch / "run"
            inventory = verify_extract_bundle(archive_path, extracted, job_id)
            if target.exists() or target.is_symlink():
                raise ClientError("artifact destination appeared during download: " + str(target))
            os.rename(str(extracted), str(target))
        return {"job_id": job_id, "directory": str(target), "verified": True,
                "files": len(inventory["files"]), "bytes": sum(item["size"] for item in inventory["files"])}


def safe_archive_path(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or "\x00" in name or name.startswith("/")):
        raise ClientError("bundle contains an unsafe path")
    clean = name[:-1] if name.endswith("/") else name
    components = clean.split("/")
    if any(part in ("", ".", "..") for part in components):
        raise ClientError("bundle contains a traversal or ambiguous path")
    if ((len(components) == 1 and clean not in ("manifest.json", "inventory.json", "environment.json", "stdout.log", "stderr.log", "metrics.json", "logs", "metrics", "artifacts"))
            or (len(components) > 1 and components[0] not in ("logs", "metrics", "artifacts"))):
        raise ClientError("bundle contains an unexpected root path")
    return clean


def verify_extract_bundle(archive_path, destination, job_id):
    """Extract only a complete, hash-verified, portable, ordinary-file bundle."""
    checked_id(job_id)
    destination = Path(destination)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = {}
            folded = set()
            portable_paths = {}
            for info in archive.infolist():
                if info.orig_filename != info.filename:
                    raise ClientError("bundle contains a truncated filename")
                name = safe_archive_path(info.filename)
                if ((name in ("logs", "metrics", "artifacts") and not info.is_dir())
                        or (name in ("manifest.json", "inventory.json", "environment.json", "stdout.log", "stderr.log", "metrics.json") and info.is_dir())):
                    raise ClientError("bundle has an invalid root file type")
                normalized = unicodedata.normalize("NFD", name).casefold()
                if name in entries or normalized in folded:
                    raise ClientError("bundle contains duplicate or case-colliding paths")
                # macOS commonly has a case-insensitive, normalization-insensitive
                # filesystem. Include implicit directories in collision checks.
                for component in (PurePosixPath(name), *PurePosixPath(name).parents):
                    spelling = str(component)
                    canonical = unicodedata.normalize("NFD", spelling).casefold()
                    previous = portable_paths.setdefault(canonical, spelling)
                    if previous != spelling:
                        raise ClientError("bundle contains case-colliding or Unicode-colliding directories")
                mode = info.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ClientError("bundle contains a symlink or special file")
                if (info.is_dir() and kind == stat.S_IFREG) or (not info.is_dir() and kind == stat.S_IFDIR):
                    raise ClientError("bundle contains inconsistent file types")
                if info.flag_bits & 1:
                    raise ClientError("encrypted bundles are unsupported")
                entries[name] = info
                folded.add(normalized)
            files = {name: info for name, info in entries.items() if not info.is_dir()}
            if not {"manifest.json", "inventory.json"}.issubset(files):
                raise ClientError("bundle requires manifest.json and inventory.json")
            for name in entries:
                for ancestor in PurePosixPath(name).parents:
                    if str(ancestor) in files:
                        raise ClientError("bundle has conflicting file and directory paths")
            if files["inventory.json"].file_size > 32 * CHUNK or files["manifest.json"].file_size > 16 * CHUNK:
                raise ClientError("bundle metadata exceeds the safety limit")
            inventory = json.loads(archive.read("inventory.json"))
            if (not isinstance(inventory, dict) or inventory.get("job_id") != job_id
                    or not isinstance(inventory.get("files"), list)):
                raise ClientError("bundle inventory does not match the requested job")
            expected = {}
            for item in inventory["files"]:
                if not isinstance(item, dict):
                    raise ClientError("bundle inventory entry must be an object")
                name = safe_archive_path(item.get("path"))
                if (name in expected or name == "inventory.json" or type(item.get("size")) is not int
                        or item["size"] < 0 or not isinstance(item.get("sha256"), str)
                        or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                    raise ClientError("bundle has an invalid inventory entry")
                expected[name] = item
            if set(expected) != set(files) - {"inventory.json"}:
                raise ClientError("bundle inventory must cover every file exactly once")
            for name, item in expected.items():
                if files[name].file_size != item["size"]:
                    raise ClientError("bundle size mismatch: " + name)
            manifest = json.loads(archive.read("manifest.json"))
            if (not isinstance(manifest, dict) or manifest.get("job_id") != job_id
                    or manifest.get("state") not in TERMINAL):
                raise ClientError("bundle manifest does not describe the requested terminal job")
            total_size = sum(info.file_size for info in files.values())
            if total_size > shutil.disk_usage(destination.parent).free:
                raise ClientError("insufficient local disk space for the extracted bundle")
            destination.mkdir(exist_ok=False)
            for name, info in files.items():
                target = destination.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, target.open("xb") as sink:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        size += len(chunk)
                        if size > info.file_size:
                            raise ClientError("bundle expands beyond its declared size")
                        digest.update(chunk)
                        sink.write(chunk)
                if name != "inventory.json" and (size != expected[name]["size"]
                        or digest.hexdigest() != expected[name]["sha256"]):
                    raise ClientError("bundle SHA-256 verification failed: " + name)
            for directory in ("logs", "metrics", "artifacts"):
                (destination / directory).mkdir(exist_ok=True)
            return inventory
    except (zipfile.BadZipFile, ValueError, UnicodeError, RuntimeError) as exc:
        raise ClientError("invalid bundle: " + str(exc)) from exc


def ssh_command(config):
    host = config.get("ssh_host")
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host):
        raise ClientError("ssh_host must be a configured SSH alias or hostname, without options or commands")
    actual_port = urllib.parse.urlsplit(validate_url(config["url"])).port
    local = config.get("local_port", actual_port)
    remote = config.get("remote_port", 8765)
    if any(type(port) is not int or not 1 <= port <= 65535 for port in (local, remote)):
        raise ClientError("local_port and remote_port must be integers in 1..65535")
    if local != actual_port:
        raise ClientError("local_port must match the port in url")
    return ["ssh", "-N", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10",
            "-o", "ForwardAgent=no", "-o", "GatewayPorts=no", "-L",
            "127.0.0.1:{}:127.0.0.1:{}".format(local, remote), host]


def emit(value, output=None):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), file=output or sys.stdout, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="ZeYu Compute Fabric Mac control plane")
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="action", required=True)
    submit = commands.add_parser("submit", help="submit a JSON job specification")
    submit.add_argument("spec")
    submit.add_argument("--idempotency-key")
    submit.add_argument("--wait", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("job_id", nargs="?")
    logs = commands.add_parser("logs")
    logs.add_argument("job_id")
    logs.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--offset", type=int, default=0, help="resume from this byte offset")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("job_id")
    commands.add_parser("workers")
    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("job_id")
    artifacts.add_argument("--output", default="./runs", help="parent directory for the verified run")
    commands.add_parser("connect", help="hold a private SSH tunnel in the foreground")
    commands.add_parser("doctor", help="check local settings and authenticated worker reachability")
    args = parser.parse_args(argv)
    key = None
    try:
        config = load_config(args.config)
        if args.action == "connect":
            command = ssh_command(config)
            if not shutil.which("ssh"):
                raise ClientError("OpenSSH client is not installed")
            emit({"event": "opening_tunnel", "url": config["url"], "ssh_host": config["ssh_host"],
                  "note": "Keep this process running. The host key must already be verified in known_hosts."}, sys.stderr)
            return subprocess.call(command)
        client = Client(config)
        if args.action == "submit":
            path = Path(args.spec).expanduser()
            if path.stat().st_size > CHUNK:
                raise ClientError("job specification exceeds 1 MiB")
            spec = json.loads(path.read_text(encoding="utf-8-sig"))
            key = args.idempotency_key or str(uuid.uuid4())
            if not KEY.fullmatch(key):
                raise ClientError("idempotency key must be 1..128 letters, digits, or ._:-")
            emit({"event": "submitting", "idempotency_key": key}, sys.stderr)
            result = client.submit(spec, key)
            if args.wait:
                emit({"event": "submitted", "job_id": result["job_id"], "idempotency_key": key}, sys.stderr)
                result = client.wait(result["job_id"])
            result.setdefault("idempotency_key", key)
            emit(result)
            return 1 if args.wait and result.get("state") != "COMPLETED" else 0
        if args.action == "status":
            emit(client.status(args.job_id))
        elif args.action == "logs":
            client.logs(args.job_id, args.stream, args.follow, args.offset)
        elif args.action == "cancel":
            emit(client.json("/v1/jobs/" + checked_id(args.job_id) + "/cancel", {}))
        elif args.action == "workers":
            emit(client.json("/v1/workers"))
        elif args.action == "artifacts":
            emit(client.artifacts(args.job_id, args.output))
        elif args.action == "doctor":
            report = {"url": config["url"], "token_file_private": True, "ssh_available": bool(shutil.which("ssh"))}
            if config.get("ssh_host"):
                ssh_command(config)
                report["ssh_host"] = config["ssh_host"]
            try:
                report["worker"] = client.json("/v1/health")
                report["ok"] = isinstance(report["worker"], dict) and report["worker"].get("status") == "ok"
            except ClientError as exc:
                report.update(ok=False, error=str(exc))
            emit(report)
            return 0 if report["ok"] else 2
        return 0
    except KeyboardInterrupt:
        emit({"error": "Client interrupted. Submitted jobs continue independently; use zrun status or zrun cancel."}, sys.stderr)
        return 130
    except (ClientError, OSError, ValueError, KeyError, TypeError) as exc:
        error = {"error": str(exc)}
        if key:
            error["idempotency_key"] = key
            error["recovery"] = "After a connection error, retry the same specification with this --idempotency-key; do not generate a new key."
        emit(error, sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
