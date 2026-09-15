"""Native BentoML service for the ZeYu Windows GPU integration.

This module deliberately contains only serving glue.  The audited UNet and
TIGER adapters remain in the existing runtime POC and are loaded through the
``module:factory`` contract at request time.  No CUDA/model import happens at
module import or service construction, so ``/gpu_health`` is a CPU-only state
probe.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import hashlib
import hmac
import importlib
import json
import logging
import os
import platform
from pathlib import Path
import sys
import threading
import time
import uuid
from typing import Annotated, Any

import bentoml
from bentoml import Context
from pydantic import Field
from starlette.responses import JSONResponse


LOG = logging.getLogger("zeyu.native_bento")
MODEL_ALIASES = ("unet", "tiger")
ModelAlias = Annotated[str, Field(json_schema_extra={"enum": list(MODEL_ALIASES)})]
RunId = Annotated[str, Field(json_schema_extra={"format": "uuid"})]
INFRA_PATHS = frozenset({"/healthz", "/livez", "/readyz", "/metrics"})
DEFAULT_TIMEOUT_SECONDS = 300.0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime_metadata() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "bentoml": getattr(bentoml, "__version__", "unknown"),
    }


def _configured_token() -> str:
    """Read the protected token file first, with env fallback for local runs."""
    token_file = os.environ.get("ZEYU_BENTO_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get("ZEYU_BENTO_TOKEN", "").strip()


def _header(scope: dict[str, Any], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def _context_header(ctx: Context | Any, name: str) -> str | None:
    try:
        headers = ctx.request.headers
    except Exception:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
        wanted = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == wanted:
                value = candidate
                break
    return str(value) if value is not None else None


def _set_response_status(ctx: Context | Any, status_code: int) -> None:
    try:
        ctx.response.status_code = status_code
    except (AttributeError, LookupError):
        # A timed-out worker can outlive Bento's request context. Its lease and
        # gate still unwind in the service finally block; no late error should
        # turn that honest timeout into a second failure.
        pass


def _set_response_header(ctx: Context | Any, name: str, value: str) -> None:
    try:
        ctx.response.headers[name] = value
    except (AttributeError, LookupError):
        pass


def _run_id_from_text(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return value


def _error_payload(
    code: str,
    message: str,
    *,
    status_code: int,
    run_id: str | None = None,
    model: Any = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    details = dict(details or {})
    resolved_run_id = run_id if run_id is not None else details.pop("run_id", None)
    resolved_model = model if model is not None else details.pop("model", None)
    metadata = {
        "schema_version": 1,
        "ok": False,
        "error_code": code,
        "message": message,
        "run_id": resolved_run_id,
        "model": resolved_model,
        "checkpoint": details.pop("checkpoint", None),
        "git_commit": details.pop("git_commit", None),
        "hardware": details.pop("hardware", {"cuda_probe": "deferred"}),
        "runtime": details.pop("runtime", _runtime_metadata()),
        "parameters": details.pop("parameters", {}),
        "artifact_sha256": details.pop("artifact_sha256", None),
        "timing": details.pop("timing", {}),
        "details": details,
    }
    # HTTP status is useful to the ASGI layer but is not duplicated into the
    # operation metadata consumed by the controller.
    del status_code
    return metadata


class ZeYuBentoError(RuntimeError):
    """An expected contract error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 500,
        run_id: str | None = None,
        model: Any = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.run_id = run_id
        self.model = model
        self.details = dict(details or {})

    @property
    def payload(self) -> dict[str, Any]:
        return _error_payload(
            self.code,
            str(self),
            status_code=self.status_code,
            run_id=self.run_id,
            model=self.model,
            details=self.details,
        )


def _json_error_response(error: ZeYuBentoError) -> JSONResponse:
    return JSONResponse(error.payload, status_code=error.status_code)


class BearerAuthMiddleware:
    """Require the configured bearer token for every custom service route."""

    def __init__(self, app: Any) -> None:
        self.app = app
        # Read the protected worker token when Bento constructs the ASGI
        # boundary.  The inline environment value remains only a local-test
        # fallback; production does not need a secret config variable.
        self.expected = _configured_token()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") in INFRA_PATHS:
            await self.app(scope, receive, send)
            return

        expected = self.expected
        run_id = _run_id_from_text(_header(scope, "x-zeyu-run-id"))
        if not expected:
            await _json_error_response(
                ZeYuBentoError(
                    "AUTH_NOT_CONFIGURED",
                    "ZEYU_BENTO_TOKEN_FILE (or local ZEYU_BENTO_TOKEN fallback) is not configured",
                    status_code=503,
                    run_id=run_id,
                )
            )(scope, receive, send)
            return

        authorization = _header(scope, "authorization") or ""
        prefix, _, supplied = authorization.partition(" ")
        if prefix.casefold() != "bearer" or not supplied or not hmac.compare_digest(
            supplied, expected
        ):
            await _json_error_response(
                ZeYuBentoError(
                    "AUTH_REQUIRED",
                    "a valid bearer token is required",
                    status_code=401,
                    run_id=run_id,
                )
            )(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except ZeYuBentoError as exc:
            await _json_error_response(exc)(scope, receive, send)
        except Exception as exc:  # pragma: no cover - defensive server boundary
            LOG.exception("unhandled Bento service error")
            error = ZeYuBentoError(
                "INTERNAL_ERROR",
                "the Bento service failed while processing the request",
                status_code=500,
                run_id=run_id,
                details={"exception_type": type(exc).__name__},
            )
            await _json_error_response(error)(scope, receive, send)


class HonestTimeoutMiddleware:
    """Return 504 without cancelling the in-flight model call.

    BentoML's stock traffic timeout cancels the ASGI task.  A synchronous
    adapter can continue running in a worker thread after that cancellation,
    but the lifecycle becomes ambiguous.  This boundary keeps the task alive,
    drops late response bytes, and lets the service's lease/gate remain held
    until the adapter returns.  A supervisor may terminate a verified process
    tree later; this middleware never attempts cancellation or process control.
    """

    def __init__(self, app: Any, timeout: float) -> None:
        self.app = app
        self.timeout = float(timeout)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/infer_audio":
            await self.app(scope, receive, send)
            return

        timed_out = False

        async def guarded_send(message: dict[str, Any]) -> None:
            if not timed_out:
                await send(message)

        task = asyncio.create_task(self.app(scope, receive, guarded_send))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.timeout)
        except asyncio.TimeoutError:
            timed_out = True
            run_id = _run_id_from_text(_header(scope, "x-zeyu-run-id"))
            error = ZeYuBentoError(
                "SERVICE_TIMEOUT",
                "request timed out; worker continues and lease remains held until work exits",
                status_code=504,
                run_id=run_id,
                details={
                    "timeout_seconds": self.timeout,
                    "cancellation": "not_requested",
                },
            )
            await _json_error_response(error)(scope, receive, send)

            def consume_late_result(done: asyncio.Task[Any]) -> None:
                try:
                    done.result()
                except asyncio.CancelledError:
                    LOG.warning("timed-out inference task was cancelled externally")
                except Exception:
                    LOG.exception("timed-out inference task failed after response")

            task.add_done_callback(consume_late_result)


def _adapter_target(alias: str) -> str:
    env_name = "ZEYU_UNET_ADAPTER" if alias == "unet" else "ZEYU_TIGER_ADAPTER"
    default = (
        "speech_denoise_adapter:create_adapter"
        if alias == "unet"
        else "tiger_adapter:create_adapter"
    )
    return os.environ.get(env_name, default)


@contextlib.contextmanager
def _alias_adapter_environment(alias: str):
    """Apply alias-specific audited adapter paths only while loading a model."""
    prefix = "ZEYU_UNET_" if alias == "unet" else "ZEYU_TIGER_"
    mapping = {
        "PROJECT_ROOT": "ZEYU_REAL_PROJECT_ROOT",
        "CONFIG": "ZEYU_REAL_CONFIG",
        "CHECKPOINT": "ZEYU_REAL_CHECKPOINT",
        "MODE": "ZEYU_REAL_MODE",
        "GIT_COMMIT": "ZEYU_REAL_GIT_COMMIT",
        "GIT_DIRTY": "ZEYU_REAL_GIT_DIRTY",
    }
    previous: dict[str, str | None] = {}
    try:
        for suffix, common_name in mapping.items():
            alias_name = f"{prefix}{suffix}"
            if alias_name not in os.environ:
                continue
            previous[common_name] = os.environ.get(common_name)
            os.environ[common_name] = os.environ[alias_name]
        yield
    finally:
        for common_name, old_value in previous.items():
            if old_value is None:
                os.environ.pop(common_name, None)
            else:
                os.environ[common_name] = old_value


def _configured_timeout() -> float:
    value = os.environ.get("ZEYU_BENTO_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        parsed = float(value)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return parsed if parsed > 0 else DEFAULT_TIMEOUT_SECONDS


def _model_summary(alias: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
    if metadata is None:
        return {"alias": alias}
    return {"alias": alias, **metadata}


def _add_runtime_paths() -> None:
    common = os.environ.get("ZEYU_RUNTIME_COMMON")
    if common:
        path = str(Path(common).expanduser().resolve())
    else:
        path = str((Path(__file__).resolve().parent / "common").resolve())
    if Path(path).is_dir() and path not in sys.path:
        sys.path.insert(0, path)


def _load_lease_types() -> tuple[type[Any], type[BaseException]]:
    fabric_root = os.environ.get("ZEYU_FABRIC_ROOT")
    if fabric_root:
        package = Path(fabric_root).expanduser().resolve() / "zeyu_fabric"
        if package.is_dir() and str(package) not in sys.path:
            sys.path.insert(0, str(package))
    try:
        module = importlib.import_module("gpu_lease")
    except (ImportError, AttributeError):
        try:
            module = importlib.import_module("zeyu_fabric.gpu_lease")
        except (ImportError, AttributeError) as exc:
            raise ZeYuBentoError(
                "LEASE_IMPLEMENTATION_UNAVAILABLE",
                "the audited ZeYuComputeFabric GpuLease implementation is unavailable",
                status_code=503,
                details={"exception_type": type(exc).__name__},
            ) from exc
    try:
        return module.GpuLease, module.LeaseBusyError
    except AttributeError as exc:
        raise ZeYuBentoError(
            "LEASE_IMPLEMENTATION_UNAVAILABLE",
            "the audited ZeYuComputeFabric GpuLease implementation is unavailable",
            status_code=503,
            details={"exception_type": type(exc).__name__},
        ) from exc


def _validate_adapter_metadata(alias: str, metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict) or metadata.get("real_audio_model") is not True:
        raise ValueError("real adapter metadata must set real_audio_model=true")
    for key in ("model", "project", "git_commit", "checkpoint_sha256"):
        if not isinstance(metadata.get(key), str) or not metadata[key]:
            raise ValueError(f"real adapter metadata must include {key}")
    if len(metadata["git_commit"]) not in (40, 64) or any(
        c not in "0123456789abcdefABCDEF" for c in metadata["git_commit"]
    ):
        raise ValueError("real adapter git_commit must be a full hexadecimal object ID")
    if len(metadata["checkpoint_sha256"]) != 64 or any(
        c not in "0123456789abcdefABCDEF" for c in metadata["checkpoint_sha256"]
    ):
        raise ValueError("real adapter checkpoint_sha256 must be a 64-character digest")
    return {"alias": alias, **metadata}


def _hardware_metadata(adapter: Any) -> dict[str, Any]:
    torch = getattr(adapter, "torch", None)
    if torch is None:
        return {"cuda_probe": "deferred_or_unavailable", "device": None, "gpu_name": None}
    try:
        available = bool(torch.cuda.is_available())
        if not available:
            return {"cuda_probe": "completed", "cuda_available": False, "device": None, "gpu_name": None}
        device = torch.device(getattr(adapter, "device", "cuda:0"))
        return {
            "cuda_probe": "completed",
            "cuda_available": True,
            "device": str(device),
            "gpu_name": str(torch.cuda.get_device_name(device)),
            "device_count": int(torch.cuda.device_count()),
        }
    except Exception as exc:  # pragma: no cover - hardware dependent
        return {"cuda_probe": "failed", "error_type": type(exc).__name__}


@bentoml.service(
    name="zeyu_native_audio",
    workers=1,
    traffic={"max_concurrency": 1, "timeout": _configured_timeout() + 30},
    metrics={"enabled": True},
)
class ZeYuNativeBento:
    """Thin BentoML boundary around the audited real audio adapters."""

    def __init__(self) -> None:
        self._gate = threading.Lock()
        # The service instance is constructed after the protected worker token
        # file is installed.  Cache it with the service rather than reading a
        # secret environment value for each request.
        self._token = _configured_token()
        self._adapter: Any = None
        self._model_alias: str | None = None
        self._model_metadata: dict[str, Any] | None = None
        self._model_state: str | None = None
        self._lease: Any = None
        self._active_run_id: str | None = None
        root = os.environ.get("ZEYU_BENTO_ARTIFACT_ROOT")
        self._artifact_root = Path(root).expanduser().resolve() if root else Path(
            os.environ.get("TEMP", "/tmp")
        ) / "zeyu-native-bento-artifacts"
        self._artifact_root.mkdir(parents=True, exist_ok=True)

    def _authorize(self, ctx: Context | Any, run_id: str | None = None) -> None:
        expected = self._token
        supplied = _context_header(ctx, "authorization") or ""
        prefix, _, token = supplied.partition(" ")
        if not expected:
            raise ZeYuBentoError(
                "AUTH_NOT_CONFIGURED",
                "ZEYU_BENTO_TOKEN_FILE (or local ZEYU_BENTO_TOKEN fallback) is not configured",
                status_code=503,
                run_id=run_id,
            )
        if prefix.casefold() != "bearer" or not token or not hmac.compare_digest(
            token, expected
        ):
            raise ZeYuBentoError(
                "AUTH_REQUIRED",
                "a valid bearer token is required",
                status_code=401,
                run_id=run_id,
            )

    def _base_details(
        self,
        *,
        run_id: str | None,
        alias: str | None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        model = (
            _model_summary(alias, self._model_metadata)
            if alias is not None
            else None
        )
        return {
            "run_id": run_id,
            "model": model,
            "checkpoint": (
                self._model_metadata.get("checkpoint") if self._model_metadata else None
            ),
            "git_commit": (
                self._model_metadata.get("git_commit") if self._model_metadata else None
            ),
            "hardware": _hardware_metadata(self._adapter),
            "runtime": _runtime_metadata(),
            "parameters": parameters or {},
        }

    def _acquire_gate(self, run_id: str | None, alias: str | None) -> None:
        if self._gate.acquire(blocking=False):
            self._active_run_id = run_id
            return
        raise ZeYuBentoError(
            "GPU_BUSY",
            "another inference or model release is in progress",
            status_code=409,
            run_id=run_id,
            model=_model_summary(self._model_alias, self._model_metadata)
            if self._model_alias
            else ({"alias": alias} if alias else None),
            details={"active_run_id": self._active_run_id},
        )

    def _lease_path(self) -> Path:
        value = os.environ.get("ZEYU_GPU_LEASE_PATH", "")
        if not value:
            raise ZeYuBentoError(
                "LEASE_NOT_CONFIGURED",
                "ZEYU_GPU_LEASE_PATH must point to the shared ZeYu GPU lease file",
                status_code=503,
            )
        return Path(value).expanduser().resolve()

    def _acquire_lease(self, alias: str, run_id: str) -> None:
        lease_cls, busy_cls = _load_lease_types()
        path = self._lease_path()
        owner = {
            "owner": "zeyu-native-bento",
            "model_alias": alias,
            "run_id": run_id,
            "service_pid": os.getpid(),
        }
        lease = lease_cls(path, owner)
        try:
            lease.acquire(timeout=0)
        except busy_cls as exc:
            code = "GPU_LEASE_STALE" if getattr(exc, "stale", False) else "GPU_BUSY"
            message = (
                "the shared GPU lease contains an unrecovered stale marker"
                if code == "GPU_LEASE_STALE"
                else "the GPU is reserved by another ZeYu execution path"
            )
            raise ZeYuBentoError(
                code,
                message,
                status_code=409,
                run_id=run_id,
                model={"alias": alias},
                details={"lease_path": str(path), "lease_owner": getattr(exc, "owner", None)},
            ) from exc
        except OSError as exc:
            raise ZeYuBentoError(
                "LEASE_UNAVAILABLE",
                "the shared GPU lease could not be opened",
                status_code=503,
                run_id=run_id,
                model={"alias": alias},
                details={"lease_path": str(path), "exception_type": type(exc).__name__},
            ) from exc
        self._lease = lease

    def _load_model(self, alias: str, run_id: str) -> float:
        if self._model_state == "load_failed":
            raise ZeYuBentoError(
                "MODEL_LOAD_FAILED",
                "a previous model load failed; call /release_model before retrying",
                status_code=500,
                run_id=run_id,
                model=_model_summary(self._model_alias or alias, self._model_metadata),
                details={"requested_model": alias, "load_state": "failed"},
            )
        if self._model_state == "loaded" and self._model_alias == alias and self._adapter is not None:
            return 0.0
        if self._adapter is not None or self._model_alias is not None:
            raise ZeYuBentoError(
                "MODEL_BUSY",
                "a different model is resident; call /release_model before switching",
                status_code=409,
                run_id=run_id,
                model=_model_summary(self._model_alias or alias, self._model_metadata),
                details={"requested_model": alias},
            )

        self._acquire_lease(alias, run_id)
        started = time.perf_counter()
        try:
            _add_runtime_paths()
            target = _adapter_target(alias)
            if ":" not in target:
                raise ValueError(f"adapter target must use module:factory syntax: {target!r}")
            with _alias_adapter_environment(alias):
                module_name, factory_name = target.split(":", 1)
                factory = getattr(importlib.import_module(module_name), factory_name)
                adapter = factory(device=os.environ.get("ZEYU_CUDA_DEVICE", "cuda:0"))
                metadata = _validate_adapter_metadata(alias, adapter.metadata())
                if not callable(getattr(adapter, "infer_wav", None)):
                    raise TypeError("real adapter must implement infer_wav()")
        except Exception as exc:
            partial_adapter = locals().get("adapter")
            cleanup_error: Exception | None = None
            if partial_adapter is not None and callable(getattr(partial_adapter, "close", None)):
                try:
                    partial_adapter.close()
                except Exception as close_exc:  # keep the lease fail-closed
                    cleanup_error = close_exc
            # The factory may have touched CUDA before raising and may not
            # expose a close hook. Retain the lease and failed state until a
            # controlled release (or supervisor-verified process termination).
            self._adapter = partial_adapter
            self._model_alias = alias
            self._model_metadata = None
            self._model_state = "load_failed"
            if isinstance(exc, ZeYuBentoError):
                raise
            raise ZeYuBentoError(
                "MODEL_LOAD_FAILED",
                "the requested audited adapter could not be loaded",
                status_code=500,
                run_id=run_id,
                model={"alias": alias},
                details={
                    "adapter_target": target if "target" in locals() else None,
                    "exception_type": type(exc).__name__,
                    "error": str(exc),
                    "cleanup_exception_type": (
                        type(cleanup_error).__name__ if cleanup_error else None
                    ),
                },
            ) from exc
        self._adapter = adapter
        self._model_alias = alias
        self._model_metadata = metadata
        self._model_state = "loaded"
        return (time.perf_counter() - started) * 1000

    def _release_model_locked(self, run_id: str | None = None) -> dict[str, Any]:
        alias = self._model_alias
        if self._adapter is None and self._lease is None:
            return {
                "schema_version": 1,
                "released": False,
                "model_alias": None,
                "run_id": run_id,
                "timing": {"unload_wall_ms": 0.0},
            }

        # A factory can allocate CUDA state and then raise before returning an
        # adapter object.  In that case there is no object reference whose
        # close/drop path can prove cleanup.  Keep the cross-process lease held
        # and require the supervisor's verified process-tree recovery path.
        if self._model_state == "load_failed" and self._adapter is None:
            raise ZeYuBentoError(
                "MODEL_UNLOAD_FAILED",
                "model load failed before an adapter reference was available; the lease remains protected",
                status_code=500,
                run_id=run_id,
                model={"alias": alias} if alias else None,
                details={
                    "cleanup_proof": "unavailable",
                    "required_action": "supervisor_verified_process_recovery",
                },
            )

        started = time.perf_counter()
        adapter = self._adapter
        try:
            if adapter is not None and callable(getattr(adapter, "close", None)):
                adapter.close()
            self._adapter = None
            self._model_alias = None
            self._model_metadata = None
            self._model_state = None
            torch = getattr(adapter, "torch", None)
            adapter = None
            gc.collect()
            if torch is not None and getattr(torch, "cuda", None) is not None:
                torch.cuda.synchronize()
                torch.cuda.empty_cache()
            if self._lease is not None:
                self._lease.release()
                self._lease = None
        except Exception as exc:
            raise ZeYuBentoError(
                "MODEL_UNLOAD_FAILED",
                "the resident model could not be unloaded safely; the lease remains protected",
                status_code=500,
                run_id=run_id,
                model={"alias": alias} if alias else None,
                details={"exception_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        return {
            "schema_version": 1,
            "released": True,
            "model_alias": alias,
            "run_id": run_id,
            "timing": {"unload_wall_ms": (time.perf_counter() - started) * 1000},
        }

    @bentoml.api
    def gpu_health(self, ctx: Context) -> dict[str, Any]:
        """CPU-only service/lease state; does not import torch or load a model."""
        self._authorize(ctx)
        lease_path = os.environ.get("ZEYU_GPU_LEASE_PATH")
        return {
            "schema_version": 1,
            "ok": True,
            "service": "zeyu_native_audio",
            "model_loaded": self._adapter is not None,
            "model_alias": self._model_alias,
            "model_state": self._model_state,
            "lease_held": self._lease is not None,
            "lease_path_configured": bool(lease_path),
            "cuda_probe": "deferred",
            "hardware": {"gpu_name": None, "device": None},
            "runtime": _runtime_metadata(),
        }

    def _infer_audio_impl(
        self,
        audio: Path,
        model: ModelAlias,
        run_id: RunId,
        ctx: Context = None,
    ) -> Path:
        """Run one audited model and return the persisted WAV artifact."""
        normalized_run_id = _run_id_from_text(run_id)
        if ctx is None:
            raise ZeYuBentoError(
                "AUTH_REQUIRED",
                "a request context with bearer authorization is required",
                status_code=401,
                run_id=normalized_run_id,
            )
        self._authorize(ctx, normalized_run_id)
        try:
            normalized_run_id = str(uuid.UUID(run_id))
        except (ValueError, AttributeError, TypeError):
            raise ZeYuBentoError(
                "INVALID_RUN_ID",
                "run_id must be a UUID",
                status_code=400,
                run_id=run_id or None,
                model={"alias": model},
            ) from None
        alias = str(model).casefold()
        if alias not in MODEL_ALIASES:
            raise ZeYuBentoError(
                "INVALID_MODEL",
                "model must be one of: unet, tiger",
                status_code=400,
                run_id=normalized_run_id,
                model={"alias": str(model)},
            )
        self._acquire_gate(normalized_run_id, alias)
        started = time.perf_counter()
        try:
            source = Path(audio)
            if not source.is_file():
                raise ZeYuBentoError(
                    "INVALID_AUDIO",
                    "audio must reference a readable uploaded file",
                    status_code=400,
                    run_id=normalized_run_id,
                    model={"alias": alias},
                    details={"path": str(source)},
                )
            try:
                data = source.read_bytes()
            except OSError as exc:
                raise ZeYuBentoError(
                    "INVALID_AUDIO",
                    "audio file could not be read",
                    status_code=400,
                    run_id=normalized_run_id,
                    model={"alias": alias},
                    details={"exception_type": type(exc).__name__},
                ) from exc
            if not data:
                raise ZeYuBentoError(
                    "INVALID_AUDIO",
                    "audio file is empty",
                    status_code=400,
                    run_id=normalized_run_id,
                    model={"alias": alias},
                )
            input_hash = _sha256(data)
            parameters = {
                "model": alias,
                "run_id": normalized_run_id,
                "input_sha256": input_hash,
                "input_bytes": len(data),
            }
            load_ms = self._load_model(alias, normalized_run_id)
            infer_started = time.perf_counter()
            try:
                result = self._adapter.infer_wav(data)
            except ZeYuBentoError:
                raise
            except Exception as exc:
                raise ZeYuBentoError(
                    "INFERENCE_FAILED",
                    "the resident adapter failed during inference",
                    status_code=500,
                    run_id=normalized_run_id,
                    model=_model_summary(alias, self._model_metadata),
                    details={"exception_type": type(exc).__name__, "error": str(exc)},
                ) from exc
            adapter_metrics: dict[str, Any] = {}
            if isinstance(result, tuple) and len(result) == 2:
                output, adapter_metrics = result
            else:
                output = result
            if not isinstance(output, bytes):
                raise ZeYuBentoError(
                    "INFERENCE_FAILED",
                    "the adapter must return bytes or (bytes, metrics)",
                    status_code=500,
                    run_id=normalized_run_id,
                    model=_model_summary(alias, self._model_metadata),
                )
            artifact_hash = _sha256(output)
            target = self._artifact_root / (
                f"{normalized_run_id.replace('-', '')}-{alias}-{artifact_hash[:16]}.wav"
            )
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(output)
            temporary.replace(target)
            total_ms = (time.perf_counter() - started) * 1000
            timing = {
                "model_load_ms": load_ms,
                "adapter_wall_ms": (time.perf_counter() - infer_started) * 1000,
                "total_wall_ms": total_ms,
                **{
                    key: value
                    for key, value in adapter_metrics.items()
                    if key.endswith("_ms") or key in {"input_frames", "output_sources"}
                },
            }
            operation = {
                "schema_version": 1,
                "ok": True,
                "operation": "infer_audio",
                "run_id": normalized_run_id,
                "model": _model_summary(alias, self._model_metadata),
                "checkpoint": self._model_metadata.get("checkpoint") if self._model_metadata else None,
                "git_commit": self._model_metadata.get("git_commit") if self._model_metadata else None,
                "hardware": _hardware_metadata(self._adapter),
                "runtime": _runtime_metadata(),
                "parameters": parameters,
                "artifact": {
                    "path": str(target),
                    "sha256": artifact_hash,
                    "bytes": len(output),
                },
                "artifact_sha256": artifact_hash,
                "timing": timing,
            }
            _set_response_header(ctx, "X-ZeYu-Operation", _json(operation))
            return target
        except ZeYuBentoError:
            raise
        except Exception as exc:
            raise ZeYuBentoError(
                "INFERENCE_FAILED",
                "the Bento inference request failed",
                status_code=500,
                run_id=normalized_run_id,
                model=_model_summary(alias, self._model_metadata),
                details={"exception_type": type(exc).__name__, "error": str(exc)},
            ) from exc
        finally:
            self._active_run_id = None
            self._gate.release()

    @bentoml.api
    def infer_audio(
        self,
        audio: Path,
        model: ModelAlias,
        run_id: RunId,
        ctx: Context = None,
    ) -> Path:
        """Run one audited model and return a persisted WAV or contract error."""
        try:
            return self._infer_audio_impl(audio, model, run_id, ctx)
        except ZeYuBentoError as exc:
            # BentoML's endpoint wrapper converts uncaught exceptions into a
            # generic 500. Returning a Response keeps the stable error code
            # and metadata visible to the controller while preserving the
            # declared Path output schema for successful requests. Unit
            # contract callers still receive the exception for assertions.
            if isinstance(ctx, Context):
                _set_response_status(ctx, exc.status_code)
                return _json_error_response(exc)  # type: ignore[return-value]
            raise

    def _release_model_impl(self, ctx: Context) -> dict[str, Any]:
        """Unload the resident model and release the shared GPU lease."""
        self._authorize(ctx)
        self._acquire_gate(None, self._model_alias)
        try:
            return self._release_model_locked()
        finally:
            self._active_run_id = None
            self._gate.release()

    @bentoml.api
    def release_model(self, ctx: Context) -> dict[str, Any]:
        """Unload the resident model and release the shared GPU lease."""
        try:
            return self._release_model_impl(ctx)
        except ZeYuBentoError as exc:
            if isinstance(ctx, Context):
                _set_response_status(ctx, exc.status_code)
                return _json_error_response(exc)  # type: ignore[return-value]
            raise

    @bentoml.on_shutdown
    def shutdown(self) -> None:
        """Release residency during orderly Bento shutdown."""
        self._gate.acquire()
        try:
            try:
                self._release_model_locked()
            except ZeYuBentoError:
                LOG.exception("failed to release model during Bento shutdown")
        finally:
            self._gate.release()


svc = ZeYuNativeBento
svc.add_asgi_middleware(BearerAuthMiddleware)
svc.add_asgi_middleware(HonestTimeoutMiddleware, timeout=_configured_timeout())
