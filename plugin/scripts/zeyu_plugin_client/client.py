"""Seven-operation local client for the explicitly selected Windows GPU plugin."""

from __future__ import annotations

import atexit
import hashlib
import io
import json
import math
from pathlib import Path
import queue
import re
import sys
import threading
import time
import uuid
from typing import Any, Mapping

from .bento import BentoClient, _write_json_new_file
from .config import PluginConfig, load_config, validate_run_id
from .errors import OutcomeUnknownError, ToolError, WINDOWS_GPU_OFFLINE
from .tunnel import SSHTunnel


_MODEL_NAMES = frozenset(("unet", "tiger"))
_JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_COMMIT = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_KNOWN_SPEC_FIELDS = frozenset(
    ("project", "git_commit", "command", "arguments", "environment", "timeout", "artifact_paths", "resources", "env")
)


class Controller:
    """Lazy, local control plane. Selection is a cooperative host-policy guard."""

    def __init__(
        self,
        config: PluginConfig | Mapping[str, Any] | str | Path,
        *,
        selected: bool = False,
        require_selected: bool = False,
        popen: Any = None,
        tunnel: SSHTunnel | None = None,
        probe_tunnel: bool = True,
    ) -> None:
        if isinstance(config, PluginConfig):
            self.config = config
        elif isinstance(config, (str, Path)):
            self.config = load_config(config)
        else:
            self.config = PluginConfig.from_mapping(dict(config), Path.cwd() / "client.json")
        self.selected = bool(selected)
        self.require_selected = bool(require_selected)
        self._tunnel = tunnel or SSHTunnel(
            self.config,
            **({"popen": popen} if popen else {}),
            probe=probe_tunnel,
        )
        atexit.register(self.close)
        self._bento_client: BentoClient | Any | None = None
        self._fabric_client: Any | None = None

    def close(self) -> None:
        self._tunnel.close()

    def invoke(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        """Return a stable error envelope for a wrapper that wants one call site."""
        try:
            result = getattr(self, operation)(**kwargs)
            return {"ok": True, "operation": operation, "result": result}
        except ToolError as exc:
            return {"ok": False, "operation": operation, **exc.envelope}

    def gpu_status(self) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        deadline = time.monotonic() + min(self.config.offline_timeout_seconds, 8.0)
        self._authorized()
        errors: dict[str, Any] = {}
        if time.monotonic() >= deadline:
            raise ToolError(WINDOWS_GPU_OFFLINE, "GPU status exceeded the offline deadline",
                            metadata=_metadata(run_id=run_id, model=None, status="ERROR"))

        bento_health = None
        fabric_health = None
        fabric_workers = None
        result_queue: queue.Queue[tuple[str, Any, ToolError | None]] = queue.Queue()
        pending: set[str] = set()

        def launch(name: str, operation: Any) -> None:
            pending.add(name)

            def worker() -> None:
                try:
                    result_queue.put((name, operation(), None))
                except ToolError as exc:
                    result_queue.put((name, None, exc))
                except Exception as exc:
                    result_queue.put((name, None, _fabric_error(exc, operation="gpu_status", run_id=run_id)))

            threading.Thread(target=worker, name=f"zeyu-gpu-status-{name}", daemon=True).start()

        try:
            bento = self._bento()
            launch("bento", lambda: bento.gpu_health(timeout=max(0.1, deadline - time.monotonic())))
        except ToolError as exc:
            errors["bento"] = exc.envelope["error"]

        try:
            fabric = self._fabric()
            launch("fabric_health", lambda: fabric.json("/v1/health"))
            launch("fabric_workers", lambda: fabric.json("/v1/workers"))
        except Exception as exc:
            fabric_error = _fabric_error(exc, operation="gpu_status", run_id=run_id)
            errors["fabric"] = fabric_error.envelope["error"]

        fabric_errors: list[dict[str, Any]] = []
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                name, value, error = result_queue.get(timeout=remaining)
            except queue.Empty:
                break
            pending.discard(name)
            if error is not None:
                if name == "bento":
                    errors["bento"] = error.envelope["error"]
                else:
                    fabric_errors.append(error.envelope["error"])
            elif name == "bento":
                bento_health = value
            elif name == "fabric_health":
                fabric_health = value
            elif name == "fabric_workers":
                fabric_workers = value

        for name in pending:
            timeout_error = {
                "code": WINDOWS_GPU_OFFLINE,
                "message": f"{name} status did not complete before the offline deadline",
            }
            if name == "bento":
                errors["bento"] = timeout_error
            else:
                fabric_errors.append(timeout_error)
        if fabric_errors:
            errors["fabric"] = fabric_errors[0] if len(fabric_errors) == 1 else {
                "code": WINDOWS_GPU_OFFLINE,
                "message": "one or more Compute Fabric status requests did not complete",
                "branches": fabric_errors,
            }
        if bento_health is None and fabric_health is None:
            raise ToolError(
                WINDOWS_GPU_OFFLINE,
                "both Bento and Compute Fabric endpoints are unreachable",
                metadata=_metadata(run_id=run_id, model=None, status="ERROR", error=errors),
            )
        status = "COMPLETED" if bento_health is not None and fabric_health is not None else "PARTIAL"
        health = {
            "ssh": {"host": self.config.ssh_host, "strict_host_key_checking": True},
            "bento": bento_health,
            "fabric": {"health": fabric_health, "workers": fabric_workers},
            "errors": errors,
        }
        metadata = _metadata(
            run_id=run_id,
            model=bento_health.get("model") if isinstance(bento_health, dict) and isinstance(bento_health.get("model"), str) else None,
            status=status,
            backend=bento_health or {},
            parameters={"fabric_health": fabric_health, "fabric_workers": fabric_workers},
            error=errors or None,
        )
        return {"operation": "gpu_status", "run_id": run_id, "status": status, "health": health, "metadata": metadata}

    def infer_audio(
        self,
        input_path: str | Path | None = None,
        model: str = "unet",
        run_id: str | None = None,
        timeout: float | None = None,
        *,
        file: str | Path | None = None,
    ) -> dict[str, Any]:
        if input_path is None:
            input_path = file
        source = _ordinary_file(input_path, "audio file")
        if not isinstance(model, str) or model not in _MODEL_NAMES:
            raise ToolError("BAD_REQUEST", "model must be one of: unet, tiger")
        run_id = str(uuid.uuid4()) if run_id is None else validate_run_id(run_id)
        timeout = _validate_timeout(timeout, self.config.request_timeout_seconds)
        if source.stat().st_size > self.config.max_audio_bytes:
            raise ToolError("BAD_REQUEST", "audio file exceeds the configured size limit")
        output = _new_audio_output(self.config.artifact_root, run_id, model)
        self._authorized()
        try:
            try:
                result = self._bento().infer_audio(source, output, model=model, run_id=run_id, timeout=timeout)
            except ToolError as rejected:
                # MODEL_BUSY is an explicit pre-execution rejection, never an
                # unknown outcome. Release only an idle resident model, once.
                if rejected.code != "MODEL_BUSY":
                    raise
                self._bento().release_model(timeout=min(timeout, self.config.offline_timeout_seconds))
                result = self._bento().infer_audio(source, output, model=model, run_id=run_id, timeout=timeout)
        except OutcomeUnknownError as exc:
            metadata = _metadata(
                run_id=run_id,
                model=model,
                status="UNKNOWN",
                parameters={"input": str(source)},
                error={"code": "OUTCOME_UNKNOWN", "message": exc.message, "outcome_unknown": True},
                artifact_hashes={"input": _sha256_file(source)},
            )
            raise OutcomeUnknownError(
                exc.message,
                details=exc.details,
                metadata=metadata,
            ) from exc
        except ToolError as exc:
            metadata = _metadata(
                run_id=run_id,
                model=model,
                status="ERROR",
                parameters={"input": str(source)},
                error={"code": exc.code, "message": exc.message},
            )
            raise exc.with_metadata(metadata) from exc
        operation = result.get("operation", {}) if isinstance(result, dict) else {}
        metadata = _metadata(
            run_id=run_id,
            model=model,
            status="COMPLETED",
            backend=operation,
            parameters={"input": str(source), "input_bytes": result.get("input_bytes")},
            artifact_hashes={"input": result.get("input_sha256"), "output": result.get("output_sha256")},
        )
        manifest_path = output.parent / "manifest.json"
        manifest = {"schema_version": 1, "operation": "infer_audio", "run_id": run_id,
                    "model": model, "status": "COMPLETED", "metadata": metadata,
                    "operation_metadata": operation,
                    "operation_metadata_path": result.get("operation_metadata_path"),
                    "artifacts": {"input": {"path": str(source), "bytes": result.get("input_bytes"), "sha256": result.get("input_sha256")},
                                  "output": {"path": str(output.resolve()), "bytes": result.get("output_bytes"), "sha256": result.get("output_sha256")}}}
        _write_json_new_file(manifest_path, manifest)
        return {"operation": "infer_audio", "run_id": run_id, "model": model, "status": "COMPLETED", **result,
                "manifest_path": str(manifest_path.resolve()), "metadata": metadata}

    def submit_job(self, spec: Mapping[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        payload = _validate_job_spec(spec, self.config)
        key = str(uuid.uuid4()) if idempotency_key is None else _validate_key(idempotency_key)
        self._authorized()
        self._bento().release_model(timeout=min(self.config.request_timeout_seconds, self.config.offline_timeout_seconds))
        try:
            response = self._fabric().submit(payload, key)
        except Exception as exc:
            if exc.__class__.__name__ == "OfflineError":
                raise OutcomeUnknownError(
                    "Fabric submission outcome is unknown; retry only with this same idempotency_key",
                    details={"idempotency_key": key},
                    metadata=_metadata(run_id=None, model="batch", status="UNKNOWN",
                                       parameters={"idempotency_key": key, "project": payload["project"]}),
                ) from exc
            raise _fabric_error(exc, operation="submit_job") from exc
        if not isinstance(response, dict) or not _JOB_ID.fullmatch(str(response.get("job_id", ""))):
            raise ToolError("INVALID_RESPONSE", "Fabric returned an invalid job response")
        job_id = str(response["job_id"])
        state = str(response.get("state", "QUEUED"))
        metadata = _metadata(
            run_id=job_id,
            model="batch",
            status=state,
            backend=response,
            parameters={"project": payload["project"], "environment": payload["environment"], "idempotency_key": key},
        )
        return {"operation": "submit_job", "job_id": job_id, "run_id": job_id, "status": state, "idempotency_key": key,
                "job": response, "metadata": metadata}

    def job_status(self, job_id: str) -> dict[str, Any]:
        job_id = _validate_job_id(job_id)
        self._authorized()
        try:
            response = self._fabric().status(job_id)
        except Exception as exc:
            raise _fabric_error(exc, operation="job_status", run_id=job_id) from exc
        if not isinstance(response, dict):
            raise ToolError("INVALID_RESPONSE", "Fabric returned an invalid job status")
        state = str(response.get("state", "UNKNOWN"))
        metadata = _metadata(run_id=job_id, model="batch", status=state, backend=response, parameters={})
        return {"operation": "job_status", "job_id": job_id, "run_id": job_id, "status": state, "job": response, "metadata": metadata}

    def job_logs(self, job_id: str, stream: str = "stdout", follow: bool = False, offset: int = 0) -> dict[str, Any]:
        job_id = _validate_job_id(job_id)
        if stream not in ("stdout", "stderr") or type(follow) is not bool or type(offset) is not int or offset < 0:
            raise ToolError("BAD_REQUEST", "stream, follow, or offset is invalid")
        self._authorized()
        output = io.StringIO()
        try:
            next_offset = self._fabric().logs(job_id, stream=stream, follow=follow, offset=offset, output=output)
        except Exception as exc:
            raise _fabric_error(exc, operation="job_logs", run_id=job_id) from exc
        metadata = _metadata(run_id=job_id, model="batch", status="COMPLETED", parameters={"stream": stream, "offset": offset})
        return {"operation": "job_logs", "job_id": job_id, "run_id": job_id, "status": "COMPLETED", "stream": stream,
                "text": output.getvalue(), "next_offset": next_offset, "metadata": metadata}

    def fetch_artifacts(self, job_id: str, output: str | Path | None = None) -> dict[str, Any]:
        job_id = _validate_job_id(job_id)
        parent = self.config.artifact_root if output is None else _within_artifact_root(output, self.config.artifact_root)
        if parent.exists() and parent.is_symlink():
            raise ToolError("BAD_REQUEST", "artifact destination cannot be a symlink")
        if parent.exists() and not parent.is_dir():
            raise ToolError("BAD_REQUEST", "artifact destination must be a directory")
        self._authorized()
        try:
            # Client.artifacts() calls the existing hash-verified
            # verify_extract_bundle() implementation.  We only read the
            # resulting inventory to surface its verified hashes.
            result = self._fabric().artifacts(job_id, parent)
        except Exception as exc:
            raise _fabric_error(exc, operation="fetch_artifacts", run_id=job_id) from exc
        artifact_hashes = {}
        if isinstance(result, dict) and isinstance(result.get("directory"), str):
            inventory_path = Path(result["directory"]) / "inventory.json"
            try:
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
                artifact_hashes = {
                    item["path"]: item["sha256"]
                    for item in inventory.get("files", [])
                    if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("sha256"), str)
                }
            except (OSError, ValueError, UnicodeError, AttributeError):
                raise ToolError("INVALID_ARTIFACT", "verified artifact inventory is unreadable")
        try:
            manifest = json.loads((Path(result["directory"]) / "manifest.json").read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ToolError("INVALID_ARTIFACT", "verified job manifest is unreadable") from exc
        metadata = _metadata(run_id=job_id, model="batch", status=str(manifest.get("state", "UNKNOWN")),
                             backend=manifest, parameters={"output": str(parent)}, artifact_hashes=artifact_hashes)
        return {"operation": "fetch_artifacts", "job_id": job_id, "run_id": job_id, "status": "COMPLETED", "artifacts": result,
                "metadata": metadata}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        job_id = _validate_job_id(job_id)
        self._authorized()
        try:
            response = self._fabric().json(f"/v1/jobs/{job_id}/cancel", {})
        except Exception as exc:
            raise _fabric_error(exc, operation="cancel_job", run_id=job_id) from exc
        if not isinstance(response, dict):
            raise ToolError("INVALID_RESPONSE", "Fabric returned an invalid cancellation response")
        state = str(response.get("state", "CANCELLED"))
        metadata = _metadata(run_id=job_id, model="batch", status=state, backend=response, parameters={})
        return {"operation": "cancel_job", "job_id": job_id, "run_id": job_id, "status": state, "job": response, "metadata": metadata}

    def _authorized(self) -> None:
        if self.require_selected and not self.selected:
            raise ToolError("PLUGIN_NOT_SELECTED", "Windows GPU plugin is not explicitly selected by the host")
        self._tunnel.ensure()

    def _bento(self) -> BentoClient:
        if self._bento_client is None:
            self._bento_client = BentoClient(self.config)
        return self._bento_client

    def _fabric(self) -> Any:
        if self._fabric_client is None:
            try:
                # Import the existing audited Fabric client; this package does
                # not copy its HTTP, retry, or extraction implementation.
                if self.config.fabric_root is not None:
                    root = str(self.config.fabric_root)
                    if root not in sys.path:
                        sys.path.insert(0, root)
                from zeyu_fabric.cli import Client as FabricClient, verify_extract_bundle
            except ImportError as exc:
                raise ToolError("BAD_CONFIG", "zeyu_fabric is not importable; install the existing Fabric package") from exc
            self._fabric_extraction_verifier = verify_extract_bundle
            self._fabric_client = FabricClient(
                {"url": self.config.fabric_url, "token_file": str(self.config.fabric_token_file)},
                timeout=min(self.config.request_timeout_seconds, self.config.offline_timeout_seconds),
                # Keep status/health/log calls within the eight-second
                # offline budget. submit_job still sends a caller/server
                # idempotency key so a caller can safely retry explicitly.
                attempts=1,
            )
        return self._fabric_client


def _ordinary_file(value: str | Path, label: str) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ToolError("BAD_REQUEST", f"{label} must be an existing ordinary file") from exc
    if path.is_symlink() or not path.is_file():
        raise ToolError("BAD_REQUEST", f"{label} must be an existing ordinary file")
    return path.resolve()


def _new_audio_output(root: Path, run_id: str, model: str) -> Path:
    if root.exists() and root.is_symlink():
        raise ToolError("BAD_REQUEST", "artifact_root cannot be a symlink")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError("BAD_REQUEST", "unable to create artifact_root") from exc
    run_dir = root / run_id
    if run_dir.exists() and run_dir.is_symlink():
        raise ToolError("BAD_REQUEST", "run artifact directory cannot be a symlink")
    try:
        run_dir.mkdir(exist_ok=True)
    except OSError as exc:
        raise ToolError("BAD_REQUEST", "unable to create the run artifact directory") from exc
    output = run_dir / f"{model}.wav"
    if any(path.exists() or path.is_symlink() for path in (output, output.with_suffix(".operation.json"), run_dir / "manifest.json")):
        raise ToolError("OUTPUT_EXISTS", "refusing to overwrite an existing artifact")
    return output


def _within_artifact_root(value: str | Path, root: Path) -> Path:
    try:
        candidate = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise ToolError("BAD_REQUEST", "artifact output must be a path") from exc
    # Check the lexical path before resolving so a symlink under artifact_root
    # cannot silently redirect extraction, then check the canonical path too.
    candidate = candidate if candidate.is_absolute() else Path.cwd() / candidate
    candidate = candidate.absolute()
    root = root.absolute()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ToolError("BAD_REQUEST", "artifact output must stay within artifact_root") from exc
    current = root
    if current.is_symlink():
        raise ToolError("BAD_REQUEST", "artifact_root cannot be a symlink")
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ToolError("BAD_REQUEST", "artifact destination cannot contain symlinks")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise ToolError("BAD_REQUEST", "artifact output must stay within artifact_root") from exc
    return resolved


def _validate_job_id(value: Any) -> str:
    if not isinstance(value, str) or not _JOB_ID.fullmatch(value):
        raise ToolError("BAD_REQUEST", "job_id must be a lowercase UUID")
    return value


def _validate_key(value: Any) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ToolError("BAD_REQUEST", "idempotency_key must be 1..128 letters, digits, or ._:-")
    return value


def _validate_timeout(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.1 <= value <= 3600:
        raise ToolError("BAD_REQUEST", "timeout must be between 0.1 and 3600 seconds")
    return float(value)


def _validate_job_spec(spec: Mapping[str, Any], config: PluginConfig) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ToolError("BAD_REQUEST", "job specification must be a JSON object")
    unknown = [key for key in spec if key not in _KNOWN_SPEC_FIELDS]
    if unknown:
        raise ToolError("BAD_REQUEST", "unknown job fields: " + ", ".join(sorted(map(str, unknown))))
    payload = dict(spec)
    if payload.get("project") not in config.known_projects:
        raise ToolError("BAD_REQUEST", "project is not in known_projects")
    if payload.get("environment") not in config.known_environments:
        raise ToolError("BAD_REQUEST", "environment is not in known_environments")
    if not isinstance(payload.get("git_commit"), str) or not _COMMIT.fullmatch(payload["git_commit"]):
        raise ToolError("BAD_REQUEST", "git_commit must be a full immutable 40- or 64-character hash")
    if not isinstance(payload.get("command"), list) or not payload["command"] or any(not isinstance(item, str) or "\x00" in item for item in payload["command"]):
        raise ToolError("BAD_REQUEST", "command must be a non-empty string array")
    for field in ("arguments", "artifact_paths"):
        value = payload.setdefault(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) or "\x00" in item for item in value):
            raise ToolError("BAD_REQUEST", f"{field} must be a string array")
    for item in payload["artifact_paths"]:
        if item.startswith(("/", "\\")) or "\\" in item or any(part == ".." for part in item.split("/")):
            raise ToolError("BAD_REQUEST", "artifact_paths must stay relative to the run")
    timeout = payload.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 604800:
        raise ToolError("BAD_REQUEST", "timeout must be a positive number no greater than 604800 seconds")
    try:
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolError("BAD_REQUEST", "job specification must be JSON-safe") from exc
    return payload


def _fabric_error(exc: Exception, *, operation: str, run_id: str | None = None) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    message = str(exc) or "Fabric request failed"
    code = WINDOWS_GPU_OFFLINE if exc.__class__.__name__ == "OfflineError" else "REMOTE_ERROR"
    metadata = _metadata(run_id=run_id, model="batch", status="ERROR", parameters={"operation": operation},
                         error={"code": code, "message": message})
    return ToolError(code, message, metadata=metadata)


def _metadata(
    *,
    run_id: str | None,
    model: str | None,
    status: str,
    backend: Mapping[str, Any] | None = None,
    parameters: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
    artifact_hashes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    backend = dict(backend or {})
    nested = backend.get("model") if isinstance(backend.get("model"), Mapping) else {}
    adapter = backend.get("real_adapter_metadata") if isinstance(backend.get("real_adapter_metadata"), Mapping) else {}
    checkpoint = backend.get("checkpoint") or nested.get("checkpoint") or adapter.get("checkpoint")
    git_commit = backend.get("git_commit") or nested.get("git_commit") or adapter.get("git_commit")
    hardware = backend.get("hardware") or backend.get("gpu") or backend.get("host")
    runtime = backend.get("runtime")
    remote_parameters = backend.get("parameters") if isinstance(backend.get("parameters"), Mapping) else {}
    if isinstance(backend.get("parameters"), list):
        remote_parameters = {"arguments": backend["parameters"]}
    if error is None and isinstance(backend.get("failure"), Mapping):
        error = backend["failure"]
    merged_parameters = {**remote_parameters, **dict(parameters or {})}
    hashes = {key: value for key, value in dict(artifact_hashes or {}).items() if isinstance(value, str) and value}
    return {
        "run_id": run_id,
        "model": model or (backend.get("model") if isinstance(backend.get("model"), str) else None),
        "checkpoint": checkpoint,
        "git_commit": git_commit,
        "hardware": hardware,
        "runtime": runtime,
        "parameters": merged_parameters,
        "status": status,
        "error": dict(error) if error else None,
        "artifact_hashes": hashes,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
