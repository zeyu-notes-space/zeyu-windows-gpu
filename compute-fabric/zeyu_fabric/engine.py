"""Durable, serial, single-user compute engine.

SQLite is the status authority. Manifests and all run material remain on disk.
Interrupted jobs become diagnostic failures; queued jobs survive restarts.
"""

import copy
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import tarfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

import psutil

from .runner import spawn
from .gpu_lease import GpuLease, LeaseBusyError
from .telemetry import host_identity, sample_metrics, utc_now


TERMINAL = frozenset(("COMPLETED", "FAILED", "CANCELLED"))
ACTIVE = frozenset(("STARTING", "RUNNING"))
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUNTIME_PROBE = r'''import importlib.metadata, json, platform, sys
packages = sorted([{"name": d.metadata.get("Name", "unknown"), "version": d.version} for d in importlib.metadata.distributions()], key=lambda d: (d["name"].lower(), d["version"]))
print(json.dumps({"python_version": platform.python_version(), "python_implementation": platform.python_implementation(), "executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix, "packages": packages}))
'''


class JobFailure(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        temporary.unlink(missing_ok=True)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts or value in (".", "./"):
        return False
    # Keep source and artifact names portable to Windows and exclude device paths.
    for part in PurePosixPath(value).parts:
        if part.endswith((".", " ")) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            return False
    return True


def _is_link_or_reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class Engine:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.root = Path(config["root"])
        if not self.root.is_absolute():
            raise ValueError("Worker root must be absolute")
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        (self.root / "runs").mkdir(exist_ok=True)
        self._interval = config.get("metrics_interval", 2.0)
        if isinstance(self._interval, bool) or not isinstance(self._interval, (int, float)) or not math.isfinite(self._interval) or not 0.1 <= self._interval <= 60:
            raise ValueError("metrics_interval must be between 0.1 and 60 seconds")
        for name in ("projects", "environments"):
            if not isinstance(config.get(name, {}), dict):
                raise ValueError(name + " must be an object")
        allowed = config.get("allowed_job_env", [])
        if not isinstance(allowed, list) or any(not isinstance(key, str) or not _ENV_NAME.fullmatch(key) or key.upper().startswith("ZRUN_") for key in allowed):
            raise ValueError("allowed_job_env must be an array of valid non-ZRUN_ variable names")
        gpu_lease_path = config.get("gpu_lease_path")
        if gpu_lease_path is not None and (not isinstance(gpu_lease_path, str) or not Path(gpu_lease_path).is_absolute()):
            raise ValueError("gpu_lease_path must be an absolute path")
        gpu_lease_wait = config.get("gpu_lease_wait_seconds", 0)
        if isinstance(gpu_lease_wait, bool) or not isinstance(gpu_lease_wait, (int, float)) or not math.isfinite(gpu_lease_wait) or not 0 <= gpu_lease_wait <= 300:
            raise ValueError("gpu_lease_wait_seconds must be between 0 and 300")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock_file = None
        self._active_id = None
        self._last_error = None
        self._db = sqlite3.connect(str(self.root / "queue.sqlite3"), timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA busy_timeout=30000")
        self._db.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, spec_hash TEXT NOT NULL, state TEXT NOT NULL, submitted_at TEXT NOT NULL, manifest TEXT NOT NULL)")
        self._db.commit()

    def _acquire_worker_lock(self):
        handle = (self.root / "worker.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("Another worker already owns this storage root") from exc
        self._lock_file = handle

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._acquire_worker_lock()
            try:
                self._recover()
                self._stop.clear()
                self._thread = threading.Thread(target=self._scheduler, name="zeyu-worker", daemon=True)
                self._thread.start()
            except BaseException:
                self._lock_file.close()
                self._lock_file = None
                raise

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
            if thread.is_alive():
                raise RuntimeError("Worker is still finishing shutdown; storage lock remains held")
        with self._lock:
            self._thread = None
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None

    def _validate(self, spec):
        if not isinstance(spec, dict):
            raise ValueError("Job specification must be an object")
        known = {"project", "git_commit", "command", "arguments", "environment", "timeout", "artifact_paths", "resources", "env"}
        unknown = set(spec) - known
        if unknown:
            raise ValueError("Unknown job fields: " + ", ".join(sorted(unknown)))
        result = copy.deepcopy(spec)
        project = result.get("project")
        environment = result.get("environment")
        if not isinstance(project, str) or project not in self.config.get("projects", {}):
            raise ValueError("Unknown project alias")
        if not isinstance(environment, str) or environment not in self.config.get("environments", {}):
            raise ValueError("Unknown environment alias")
        repository = self.config["projects"][project]
        if not isinstance(repository, str) or not Path(repository).is_absolute() or not Path(repository).is_dir():
            raise ValueError("Configured project path must be an existing absolute directory")
        env_config = self.config["environments"][environment]
        if not isinstance(env_config, dict) or not isinstance(env_config.get("python"), str) or not Path(env_config["python"]).is_absolute():
            raise ValueError("Configured environment must specify an absolute Python executable")
        if not isinstance(env_config.get("variables", {}), dict) or any(not isinstance(k, str) or not _ENV_NAME.fullmatch(k) or k.upper().startswith("ZRUN_") or not isinstance(v, str) or "\x00" in v for k, v in env_config.get("variables", {}).items()):
            raise ValueError("Configured environment variables must be valid string pairs without reserved ZRUN_ names")
        commit = result.get("git_commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
            raise ValueError("git_commit must be a full immutable 40- or 64-character commit hash")
        result["git_commit"] = commit.lower()
        for field, default in (("command", None), ("arguments", []), ("artifact_paths", [])):
            value = result.setdefault(field, default)
            if not isinstance(value, list) or any(not isinstance(item, str) or "\x00" in item for item in value):
                raise ValueError(field + " must be an array of strings without NUL bytes")
        if not result["command"] or not result["command"][0]:
            raise ValueError("command must contain a nonempty executable")
        if any(not _safe_relative(value) for value in result["artifact_paths"]):
            raise ValueError("artifact_paths must be safe relative globs using forward slashes")
        timeout = result.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 604800:
            raise ValueError("timeout must be a positive number no greater than 604800 seconds")
        resources = result.setdefault("resources", {})
        if not isinstance(resources, dict) or set(resources) - {"gpu", "min_vram_mb", "min_ram_mb"}:
            raise ValueError("resources only supports gpu, min_vram_mb and min_ram_mb")
        if "gpu" in resources and not isinstance(resources["gpu"], bool):
            raise ValueError("resources.gpu must be a boolean")
        for key in ("min_vram_mb", "min_ram_mb"):
            if key in resources and (type(resources[key]) is not int or resources[key] < 0):
                raise ValueError("resources." + key + " must be a nonnegative integer")
        variables = result.setdefault("env", {})
        allowed = set(self.config.get("allowed_job_env", []))
        if not isinstance(variables, dict) or any(not isinstance(k, str) or k not in allowed or not isinstance(v, str) or "\x00" in v for k, v in variables.items()):
            raise ValueError("Job env values must be strings and names must be explicitly allowlisted")
        # Ensure the complete user-supplied payload is JSON-safe before writing.
        _canonical(result)
        return result

    def submit(self, spec, idempotency_key):
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200 or any(ord(c) < 32 for c in idempotency_key):
            raise ValueError("Idempotency key must contain 1-200 printable characters")
        specification = self._validate(spec)
        spec_hash = hashlib.sha256(_canonical(specification).encode("utf-8")).hexdigest()
        with self._lock:
            existing = self._db.execute("SELECT spec_hash, manifest FROM jobs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                if existing[0] != spec_hash:
                    raise RuntimeError("Idempotency key already used with a different specification")
                return json.loads(existing[1])
            job_id = str(uuid.uuid4())
            run_dir = self.root / "runs" / job_id
            run_dir.mkdir()
            for name in ("logs", "metrics", "artifacts", "workspace"):
                (run_dir / name).mkdir()
            for name in ("stdout", "stderr"):
                (run_dir / "logs" / (name + ".log")).touch()
                (run_dir / (name + ".log")).touch()
            (run_dir / "metrics" / "samples.jsonl").touch()
            _atomic_json(run_dir / "metrics.json", {"status": "PENDING", "detail_path": "metrics/samples.jsonl"})
            _atomic_json(run_dir / "environment.json", {"status": "PENDING"})
            now = utc_now()
            manifest = {
                "schema_version": 1, "job_id": job_id, "state": "QUEUED", "submitted_at": now,
                "started_at": None, "execution_started_at": None, "ended_at": None, "duration_seconds": None,
                "exit_code": None, "spec": specification, "git_commit": specification["git_commit"],
                "command": specification["command"], "parameters": specification["arguments"],
                "environment": specification["environment"], "cancel_requested": False,
                "failure": None, "artifacts": [], "history": [{"state": "QUEUED", "at": now}],
            }
            _atomic_json(run_dir / "manifest.json", manifest)
            with self._db:
                self._db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)", (job_id, idempotency_key, spec_hash, "QUEUED", now, _canonical(manifest)))
        self._wake.set()
        return manifest

    def list_jobs(self):
        with self._lock:
            return [json.loads(row[0]) for row in self._db.execute("SELECT manifest FROM jobs ORDER BY submitted_at DESC, rowid DESC")]

    def get_job(self, job_id):
        with self._lock:
            row = self._db.execute("SELECT manifest FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            return json.loads(row[0])

    def _persist(self, manifest):
        _atomic_json(self.root / "runs" / manifest["job_id"] / "manifest.json", manifest)
        with self._db:
            self._db.execute("UPDATE jobs SET state = ?, manifest = ? WHERE job_id = ?", (manifest["state"], _canonical(manifest), manifest["job_id"]))

    def _update(self, job_id, state=None, **fields):
        with self._lock:
            manifest = self.get_job(job_id)
            if manifest["state"] in TERMINAL:
                return manifest
            if state is not None and state != manifest["state"]:
                allowed = {"QUEUED": {"STARTING", "CANCELLED"}, "STARTING": {"RUNNING", "FAILED", "CANCELLED"}, "RUNNING": {"COMPLETED", "FAILED", "CANCELLED"}}
                if state not in allowed.get(manifest["state"], set()):
                    raise RuntimeError("Invalid job state transition")
                manifest["state"] = state
                manifest["history"].append({"state": state, "at": utc_now()})
            manifest.update(fields)
            self._persist(manifest)
            return manifest

    def cancel(self, job_id):
        with self._lock:
            manifest = self.get_job(job_id)
            if manifest["state"] in TERMINAL:
                return manifest
            if manifest["state"] == "QUEUED":
                result = self._update(job_id, "CANCELLED", cancel_requested=True, ended_at=utc_now(), duration_seconds=0.0, failure={"code": "CANCELLED", "message": "Cancelled before execution"})
                self._write_contract_exports(job_id, "CANCELLED")
                return result
            manifest = self._update(job_id, cancel_requested=True, cancel_requested_at=utc_now())
        self._wake.set()
        return manifest

    def workers(self):
        with self._lock:
            queued = self._db.execute("SELECT COUNT(*) FROM jobs WHERE state = 'QUEUED'").fetchone()[0]
            running = self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
            active_id = self._active_id
        return {"worker_id": platform.node(), "state": "BUSY" if active_id else ("IDLE" if running else "STOPPED"), "active_job_id": active_id, "queued_jobs": queued, "serial_execution": True, "host": host_identity(), "metrics": sample_metrics(), "last_error": self._last_error}

    def health(self):
        running = self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
        return {"status": "ok" if running else "degraded", "scheduler_running": running, "last_error": self._last_error}

    def _recover(self):
        for manifest in self.list_jobs():
            run_dir = self.root / "runs" / manifest["job_id"]
            if manifest["state"] in ACTIVE:
                cleanup = self._cleanup_interrupted_process(manifest)
                failure = {"code": "WORKER_INTERRUPTED", "message": "Worker stopped before recording a terminal result; this job is not automatically rerun", "previous_state": manifest["state"], "process_cleanup": cleanup}
                try:
                    artifacts = self._collect_artifacts(manifest)
                except Exception as exc:
                    artifacts = []
                    failure["artifact_collection_error"] = str(exc)
                self._finish(manifest["job_id"], "FAILED", None, failure, artifacts)
            else:
                # Reconcile any manifest file left ahead of its SQLite commit.
                _atomic_json(run_dir / "manifest.json", manifest)

    def _cleanup_interrupted_process(self, manifest):
        pid = manifest.get("process", {}).get("pid")
        created = manifest.get("process", {}).get("create_time")
        if not pid or created is None:
            return "No recorded process; Windows Job Object handles close with the previous worker"
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - created) > 0.01:
                return "Recorded PID was reused; unrelated process was not touched"
            if os.name != "nt":
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                for child in process.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
            try:
                process.wait(timeout=5)
            except psutil.TimeoutExpired:
                raise RuntimeError("Cannot recover worker: recorded job process did not exit after termination")
            return "Recorded surviving process tree terminated"
        except psutil.NoSuchProcess:
            return "Recorded process is no longer running"
        except (psutil.AccessDenied, OSError) as exc:
            raise RuntimeError("Cannot recover worker until previous process cleanup is verified: " + str(exc)) from exc

    def _scheduler(self):
        while not self._stop.is_set():
            try:
                with self._lock:
                    row = self._db.execute("SELECT job_id FROM jobs WHERE state = 'QUEUED' ORDER BY submitted_at, rowid LIMIT 1").fetchone()
                    if row:
                        self._active_id = row[0]
                        self._update(row[0], "STARTING", started_at=utc_now())
                if row:
                    try:
                        self._execute(row[0])
                    finally:
                        self._active_id = None
                    continue
            except Exception as exc:
                self._last_error = str(exc)
                # A failed durable write must not silently allow the next job.
                self._stop.set()
                break
            self._wake.wait(0.25)
            self._wake.clear()

    def _check_control(self, job_id, deadline):
        if self.get_job(job_id).get("cancel_requested"):
            raise JobFailure("CANCELLED", "Cancellation requested by the control plane")
        if self._stop.is_set():
            raise JobFailure("WORKER_STOPPED", "Worker shut down while this job was active; it was not automatically rerun")
        if time.monotonic() >= deadline:
            raise JobFailure("TIMEOUT", "Job exceeded its timeout, including preparation and execution")

    def _run_capture(self, job_id, argv, cwd, env, deadline, label):
        run_dir = self.root / "runs" / job_id
        stdout_path = run_dir / ("." + label + ".stdout")
        stderr_path = run_dir / ("." + label + ".stderr")
        process = None
        try:
            self._check_control(job_id, deadline)
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = spawn(argv, cwd, env, stdout, stderr)
                while process.poll() is None:
                    self._check_control(job_id, deadline)
                    time.sleep(0.05)
                exit_code = process.poll()
                self._check_control(job_id, deadline)
            output = stdout_path.read_bytes()
            error = stderr_path.read_text(encoding="utf-8", errors="replace")
            if exit_code != 0:
                raise JobFailure("PREPARATION_FAILED", label + " failed", exit_code=exit_code, stderr=error[-16000:], command=argv)
            return output
        finally:
            if process:
                try:
                    process.close()
                except Exception as exc:
                    self._last_error = "Preparation process cleanup could not be verified: " + str(exc)
                    self._stop.set()
                    raise JobFailure("PROCESS_CLEANUP_FAILED", self._last_error) from exc
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)

    def _environment(self, spec, run_dir):
        configuration = self.config["environments"][spec["environment"]]
        env = os.environ.copy()
        if os.name == "nt":
            # Environment names are case-insensitive on Windows.
            env = {k.upper(): v for k, v in env.items()}
        for key, value in {**configuration.get("variables", {}), **spec.get("env", {})}.items():
            env[key.upper() if os.name == "nt" else key] = value
        python_path = str(Path(configuration["python"]))
        env["PATH"] = str(Path(python_path).parent) + os.pathsep + env.get("PATH", "")
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "ZRUN_JOB_ID": run_dir.name, "ZRUN_RUN_DIR": str(run_dir), "ZRUN_ARTIFACT_DIR": str(run_dir / "artifacts")})
        return python_path, env

    def _prepare_workspace(self, job_id, spec, env, deadline):
        run_dir = self.root / "runs" / job_id
        repository = Path(self.config["projects"][spec["project"]])
        git = shutil.which("git", path=env.get("PATH"))
        if not git:
            raise JobFailure("GIT_UNAVAILABLE", "Git is not installed or is not discoverable by the worker")
        commit = spec["git_commit"]
        resolved = self._run_capture(job_id, [git, "-C", str(repository), "rev-parse", "--verify", commit + "^{commit}"], run_dir, env, deadline, "git-commit").decode("ascii").strip()
        if resolved.lower() != commit:
            raise JobFailure("COMMIT_MISMATCH", "git_commit must identify a commit object directly", requested=commit, resolved=resolved)
        tree = self._run_capture(job_id, [git, "-C", str(repository), "ls-tree", "-r", "-z", commit], run_dir, env, deadline, "git-tree")
        for entry in tree.split(b"\0"):
            if entry and entry.split(b" ", 1)[0] in (b"120000", b"160000"):
                raise JobFailure("UNSUPPORTED_SOURCE_ENTRY", "Repository contains a symlink or submodule, which this version does not export", entry=entry.decode("utf-8", errors="replace"))
        archive = run_dir / ".source.tar"
        try:
            self._run_capture(job_id, [git, "-C", str(repository), "archive", "--format=tar", "--output=" + str(archive), commit], run_dir, env, deadline, "git-archive")
            with tarfile.open(archive, "r:") as bundle:
                names_seen = set()
                for member in bundle:
                    self._check_control(job_id, deadline)
                    name = member.name.rstrip("/")
                    if not _safe_relative(name) or not (member.isdir() or member.isfile()):
                        raise JobFailure("UNSUPPORTED_SOURCE_ENTRY", "Unsafe or unsupported entry in Git export", entry=member.name)
                    identity = name.casefold() if os.name == "nt" else name
                    if identity in names_seen:
                        raise JobFailure("UNSUPPORTED_SOURCE_ENTRY", "Duplicate or case-colliding Git export path", entry=member.name)
                    names_seen.add(identity)
                    destination = run_dir / "workspace" / Path(*PurePosixPath(name).parts)
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                    else:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with bundle.extractfile(member) as source, destination.open("wb") as target:
                            while True:
                                self._check_control(job_id, deadline)
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                target.write(chunk)
                        destination.chmod(0o755 if member.mode & 0o111 else 0o644)
        finally:
            archive.unlink(missing_ok=True)
        self._update(job_id, source={"project": spec["project"], "repository": str(repository), "git_commit": resolved, "method": "git archive", "working_tree_used": False})

    def _check_resources(self, spec, host):
        resources = spec.get("resources", {})
        if resources.get("min_ram_mb", 0) * 1024 * 1024 > psutil.virtual_memory().available:
            raise JobFailure("RESOURCE_UNAVAILABLE", "Insufficient available system RAM", requested=resources)
        if resources.get("gpu") or resources.get("min_vram_mb", 0):
            devices = host["gpu"]["devices"]
            if not host["gpu"]["available"]:
                raise JobFailure("GPU_UNAVAILABLE", "A GPU was requested but nvidia-smi cannot report a device", telemetry_error=host["gpu"].get("error"))
            required = resources.get("min_vram_mb", 0)
            if required and not any(isinstance(device.get("vram_total_mb"), (int, float)) and isinstance(device.get("vram_used_mb"), (int, float)) and device["vram_total_mb"] - device["vram_used_mb"] >= required for device in devices):
                raise JobFailure("RESOURCE_UNAVAILABLE", "No GPU has the requested free VRAM", requested=resources, devices=devices)

    def _execute(self, job_id):
        manifest = self.get_job(job_id)
        spec = manifest["spec"]
        run_dir = self.root / "runs" / job_id
        started = time.monotonic()
        deadline = started + spec["timeout"]
        process = None
        metrics_stop = threading.Event()
        metrics_thread = None
        exit_code = None
        failure = None
        final_state = "FAILED"
        gpu_lease = None
        try:
            self._check_control(job_id, deadline)
            python_path, env = self._environment(spec, run_dir)
            needs_gpu = bool(spec.get("resources", {}).get("gpu") or spec.get("resources", {}).get("min_vram_mb", 0))
            if needs_gpu and self.config.get("gpu_lease_path"):
                gpu_lease = GpuLease(self.config["gpu_lease_path"], {"component": "COMPUTE_FABRIC", "job_id": job_id})
                try:
                    gpu_lease.acquire(self.config.get("gpu_lease_wait_seconds", 0))
                except LeaseBusyError as exc:
                    message = ("GPU lease has a stale HELD marker; stop and recover the GPU Runtime before retrying"
                               if exc.stale else "Persistent GPU Runtime currently owns the GPU lease")
                    raise JobFailure("GPU_BUSY", message, lease_path=exc.path, owner=exc.owner,
                                     stale_marker=exc.stale) from exc
            host = host_identity()
            self._update(job_id, host=host)
            self._check_control(job_id, deadline)
            self._check_resources(spec, host)
            self._prepare_workspace(job_id, spec, env, deadline)
            try:
                runtime = json.loads(self._run_capture(job_id, [python_path, "-c", _RUNTIME_PROBE], run_dir, env, min(deadline, time.monotonic() + 30), "runtime-probe"))
            except JobFailure as exc:
                if exc.code == "PREPARATION_FAILED":
                    raise JobFailure("ENVIRONMENT_PROBE_FAILED", "Configured Python environment could not be inspected", cause=str(exc), **exc.details) from exc
                if exc.code == "TIMEOUT" and time.monotonic() < deadline:
                    raise JobFailure("ENVIRONMENT_PROBE_TIMEOUT", "Python environment inspection exceeded its 30-second preparation limit") from exc
                raise
            dependency_hash = hashlib.sha256(_canonical(runtime["packages"]).encode("utf-8")).hexdigest()
            runtime["dependency_identity_sha256"] = dependency_hash
            environment_identity = {
                "python_executable": runtime["executable"],
                "python_version": runtime["python_version"],
                "dependency_identity_sha256": dependency_hash,
                "configured_variables": self.config["environments"][spec["environment"]].get("variables", {}),
                "job_env": spec.get("env", {}),
            }
            runtime["environment_identity_sha256"] = hashlib.sha256(_canonical(environment_identity).encode("utf-8")).hexdigest()
            runtime["environment_alias"] = spec["environment"]
            runtime["configured_variable_names"] = sorted(self.config["environments"][spec["environment"]].get("variables", {}))
            runtime["job_variable_names"] = sorted(spec.get("env", {}))
            _atomic_json(run_dir / "environment.json", runtime)
            command = [python_path if i == 0 and part == "{python}" else part for i, part in enumerate(spec["command"])] + spec["arguments"]
            self._update(job_id, resolved_command=command, runtime={key: value for key, value in runtime.items() if key != "packages"}, dependency_inventory="environment.json")
            self._check_control(job_id, deadline)
            with (run_dir / "logs" / "stdout.log").open("ab", buffering=0) as stdout, (run_dir / "logs" / "stderr.log").open("ab", buffering=0) as stderr:
                process = spawn(command, run_dir / "workspace", env, stdout, stderr)
                try:
                    create_time = psutil.Process(process.pid).create_time()
                except psutil.NoSuchProcess:
                    create_time = None
                self._update(job_id, "RUNNING", execution_started_at=utc_now(), process={"pid": process.pid, "create_time": create_time, "containment": "windows-job-object" if os.name == "nt" else "posix-process-group"})
                metrics_thread = threading.Thread(target=self._sample_loop, args=(run_dir, process.pid, metrics_stop), name="metrics-" + job_id, daemon=True)
                metrics_thread.start()
                while True:
                    exit_code = process.poll()
                    if exit_code is not None:
                        break
                    self._check_control(job_id, deadline)
                    time.sleep(0.05)
                self._check_control(job_id, deadline)
                if exit_code == 0:
                    final_state = "COMPLETED"
                else:
                    failure = self._diagnose_exit(run_dir, exit_code)
        except JobFailure as exc:
            failure = {"code": exc.code, "message": str(exc), **exc.details}
            final_state = "CANCELLED" if exc.code == "CANCELLED" else "FAILED"
        except FileNotFoundError as exc:
            failure = {"code": "COMMAND_NOT_FOUND", "message": str(exc)}
        except OSError as exc:
            failure = {"code": "PROCESS_START_FAILED" if process is None else "WORKER_IO_ERROR", "message": str(exc), "errno": exc.errno}
        except Exception as exc:
            failure = {"code": "WORKER_ERROR", "message": str(exc), "exception_type": type(exc).__name__}
        finally:
            if process:
                try:
                    process.terminate()
                    if exit_code is None:
                        exit_code = process.wait(timeout=5)
                    process.close()
                except Exception as exc:
                    final_state = "FAILED"
                    failure = {"code": "PROCESS_CLEANUP_FAILED", "message": str(exc), "original_failure": failure}
                    self._last_error = "Execution process cleanup could not be verified: " + str(exc)
                    self._stop.set()
            metrics_stop.set()
            if metrics_thread:
                metrics_thread.join(timeout=7)
            if gpu_lease:
                gpu_lease.release()
            try:
                artifacts = self._collect_artifacts(self.get_job(job_id))
            except Exception as exc:
                artifacts = []
                if failure:
                    failure["artifact_collection_error"] = str(exc)
                else:
                    final_state = "FAILED"
                    failure = {"code": "ARTIFACT_COLLECTION_FAILED", "message": str(exc)}
            if failure:
                try:
                    with (run_dir / "logs" / "stderr.log").open("ab") as stderr:
                        stderr.write(("\n[zeyu-worker] " + json.dumps(failure, ensure_ascii=False) + "\n").encode("utf-8"))
                except OSError:
                    pass
            self._finish(job_id, final_state, exit_code, failure, artifacts)

    def _sample_loop(self, run_dir, pid, stop):
        try:
            with (run_dir / "metrics" / "samples.jsonl").open("a", encoding="utf-8", buffering=1) as stream:
                while True:
                    try:
                        sample = sample_metrics(pid)
                    except Exception as exc:
                        sample = {"timestamp": utc_now(), "error": str(exc)}
                    stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
                    if stop.wait(self._interval):
                        break
        except OSError as exc:
            self._last_error = "Metrics could not be written: " + str(exc)
            self._update(run_dir.name, metrics_error=str(exc))

    def _diagnose_exit(self, run_dir, exit_code):
        path = run_dir / "logs" / "stderr.log"
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 16000))
            tail = handle.read().decode("utf-8", errors="replace")
        text = tail.lower()
        if "outofmemoryerror" in text or "cuda out of memory" in text or "cuda error: out of memory" in text:
            return {"code": "GPU_OUT_OF_MEMORY", "message": "Process failed; stderr indicates GPU/CUDA out of memory", "classification": "inferred_from_stderr", "exit_code": exit_code, "stderr_tail": tail}
        if "traceback (most recent call last)" in text:
            return {"code": "PYTHON_EXCEPTION", "message": "Process exited with a Python traceback", "classification": "inferred_from_stderr", "exit_code": exit_code, "stderr_tail": tail}
        return {"code": "PROCESS_EXIT", "message": "Process exited with nonzero status", "exit_code": exit_code, "stderr_tail": tail}

    def _collect_artifacts(self, manifest):
        run_dir = self.root / "runs" / manifest["job_id"]
        workspace = run_dir / "workspace"
        target_root = run_dir / "artifacts"
        if _is_link_or_reparse(target_root) or not target_root.is_dir():
            raise ValueError("Artifact root must be a real directory")
        copied = set()
        for pattern in manifest["spec"].get("artifact_paths", []):
            for candidate in workspace.glob(pattern):
                for source in self._artifact_files(candidate):
                    relative = source.relative_to(workspace)
                    if relative.as_posix() in copied:
                        continue
                    copied.add(relative.as_posix())
                    # All path components are checked because a workload can
                    # create links after source export.
                    self._check_real_path(source, workspace)
                    destination = target_root / relative
                    self._check_real_path(destination, target_root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(source), str(destination))
        inventory = []
        for path in sorted(self._artifact_files(target_root)):
            self._check_real_path(path, target_root)
            if not _safe_relative(path.relative_to(target_root).as_posix()):
                raise ValueError("Artifact name is not portable or safe: " + str(path))
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            inventory.append({"path": path.relative_to(target_root).as_posix(), "size": path.stat().st_size, "sha256": digest.hexdigest()})
        return inventory

    @staticmethod
    def _check_real_path(path, root):
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("Artifact path escaped its root") from exc
        for component in [path] + list(path.parents):
            if _is_link_or_reparse(component):
                raise ValueError("Symlinks and Windows reparse points are not allowed in artifacts: " + str(component))
            if component == root:
                return
        raise ValueError("Artifact path escaped its root")

    @staticmethod
    def _artifact_files(path):
        if _is_link_or_reparse(path):
            raise ValueError("Symlinks and Windows reparse points are not allowed in artifacts: " + str(path))
        if path.is_dir():
            for directory, dirs, files in os.walk(path, followlinks=False):
                for name in dirs:
                    if _is_link_or_reparse(Path(directory) / name):
                        raise ValueError("Symlink directory is not allowed in artifacts")
                for name in files:
                    candidate = Path(directory) / name
                    if _is_link_or_reparse(candidate) or not stat.S_ISREG(candidate.stat().st_mode):
                        raise ValueError("Artifact must be a regular file: " + str(candidate))
                    yield candidate
        elif path.exists():
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Artifact must be a regular file: " + str(path))
            yield path

    def _finish(self, job_id, state, exit_code, failure, artifacts):
        manifest = self.get_job(job_id)
        ended_at = utc_now()
        duration = None
        if manifest["started_at"]:
            duration = max(0.0, (datetime.fromisoformat(ended_at) - datetime.fromisoformat(manifest["started_at"])).total_seconds())
        try:
            self._write_contract_exports(job_id, state)
        except Exception as exc:
            if failure:
                failure["contract_export_error"] = str(exc)
            else:
                state = "FAILED"
                failure = {"code": "CONTRACT_EXPORT_FAILED", "message": str(exc)}
        return self._update(job_id, state, ended_at=ended_at, duration_seconds=duration, exit_code=exit_code, failure=failure, artifacts=artifacts)

    def _write_contract_exports(self, job_id, state):
        run_dir = self.root / "runs" / job_id
        for name in ("stdout.log", "stderr.log"):
            shutil.copyfile(run_dir / "logs" / name, run_dir / name)
        samples = []
        sample_errors = []
        with (run_dir / "metrics" / "samples.jsonl").open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    samples.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    sample_errors.append({"line": number, "error": str(exc)})
        def maximum(path):
            values = []
            for sample in samples:
                value = sample
                for key in path:
                    value = value.get(key) if isinstance(value, dict) else None
                if isinstance(value, (int, float)):
                    values.append(value)
            return max(values) if values else None
        gpu_utilization = []
        gpu_vram_used = []
        for sample in samples:
            for device in sample.get("gpu", {}).get("devices", []):
                if isinstance(device.get("utilization_percent"), (int, float)):
                    gpu_utilization.append(device["utilization_percent"])
                if isinstance(device.get("vram_used_mb"), (int, float)):
                    gpu_vram_used.append(device["vram_used_mb"])
        summary = {
            "status": state, "detail_path": "metrics/samples.jsonl", "sample_count": len(samples),
            "first_sample_at": samples[0].get("timestamp") if samples else None,
            "last_sample_at": samples[-1].get("timestamp") if samples else None,
            "peaks": {"cpu_utilization_percent": maximum(("cpu_utilization_percent",)),
                      "system_memory_used_bytes": maximum(("memory", "used_bytes")),
                      "process_tree_rss_bytes": maximum(("process_tree", "rss_bytes")),
                      "gpu_utilization_percent": max(gpu_utilization) if gpu_utilization else None,
                      "gpu_vram_used_mb": max(gpu_vram_used) if gpu_vram_used else None},
            "parse_errors": sample_errors,
        }
        _atomic_json(run_dir / "metrics.json", summary)
