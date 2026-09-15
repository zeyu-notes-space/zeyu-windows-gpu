"""Small synchronous BentoML HTTP adapter used by the local controller."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Mapping

from .config import PluginConfig
from .errors import OfflineError, OutcomeUnknownError, ToolError


MAX_RESPONSE_BYTES = 256 * 1024 * 1024


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class BentoClient:
    def __init__(self, config: PluginConfig):
        self.config = config
        self.base_url = config.bento_url
        self.token = _read_token(config.bento_token_file)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: float | None = None,
        run_id: str | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/octet-stream"
        if run_id:
            headers["X-ZeYu-Run-ID"] = run_id
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout or self.config.request_timeout_seconds) as response:
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_RESPONSE_BYTES:
                            raise ToolError("REMOTE_RESPONSE_TOO_LARGE", "Bento response exceeds the safety limit")
                    except ValueError as exc:
                        raise ToolError("INVALID_RESPONSE", "Bento returned an invalid Content-Length") from exc
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ToolError("REMOTE_RESPONSE_TOO_LARGE", "Bento response exceeds the safety limit")
                return payload, {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read(16 * 1024)
            finally:
                exc.close()
            code, message, remote = _structured_remote_error(payload, exc.code)
            raise ToolError(code, message, details={"http_status": exc.code, "remote": remote}) from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as exc:
            raise OfflineError("Bento endpoint is unreachable") from exc

    def gpu_health(self, *, timeout: float | None = None) -> dict[str, Any]:
        payload, headers = self._request(
            "POST",
            "/gpu_health",
            body=b"{}",
            content_type="application/json",
            timeout=timeout,
        )
        value = _json(payload, "gpu_health")
        operation = _operation_header(headers.get("x-zeyu-operation"))
        return {**value, **operation, "response_headers": dict(headers)}

    def release_model(self, *, timeout: float | None = None) -> dict[str, Any]:
        payload, _ = self._request(
            "POST",
            "/release_model",
            body=b"{}",
            content_type="application/json",
            timeout=timeout,
        )
        value = _json(payload, "release_model")
        return value

    def infer_audio(
        self,
        source: Path,
        output: Path,
        *,
        model: str,
        run_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            data = source.read_bytes()
            input_sha = hashlib.sha256(data).hexdigest()
            body, content_type = _multipart_audio(data, source.name, model=model, run_id=run_id)
            payload, headers = self._request(
                "POST",
                "/infer_audio",
                body=body,
                content_type=content_type,
                timeout=timeout or self.config.request_timeout_seconds,
                run_id=run_id,
            )
        except OfflineError as exc:
            raise OutcomeUnknownError(
                "Bento inference transport failed; the remote result may still exist",
                metadata={"run_id": run_id, "model": model, "status": "UNKNOWN"},
            ) from exc
        except OutcomeUnknownError:
            raise
        except ToolError:
            raise
        except (OSError, ValueError) as exc:
            raise ToolError("BAD_REQUEST", "unable to read audio input") from exc
        except (socket.timeout, TimeoutError, ConnectionError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise OutcomeUnknownError(
                "Bento inference transport failed; the remote result may still exist",
                metadata={"run_id": run_id, "model": model, "status": "UNKNOWN"},
            ) from exc
        response_content_type = headers.get("content-type", "")
        if not response_content_type.lower().startswith(("audio/wav", "audio/x-wav", "application/octet-stream")):
            raise ToolError("INVALID_RESPONSE", "Bento inference did not return an audio file")
        output_sha = hashlib.sha256(payload).hexdigest()
        operation = _validated_operation(headers.get("x-zeyu-operation"), run_id, model, input_sha, output_sha, len(payload), len(data))
        operation_path = output.with_suffix(".operation.json")
        if operation_path.exists() or operation_path.is_symlink():
            raise ToolError("OUTPUT_EXISTS", "refusing to overwrite operation metadata")
        _write_new_file(output, payload)
        _write_json_new_file(operation_path, operation)
        return {
            "output_path": str(output.resolve()),
            "output_bytes": len(payload),
            "input_bytes": len(data),
            "input_sha256": input_sha,
            "output_sha256": output_sha,
            "operation": operation,
            "operation_metadata_path": str(operation_path.resolve()),
        }


def _read_token(path: Path) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("token_file must be an existing ordinary file")
        token = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise ToolError("BAD_CONFIG", "unable to read token_file") from exc
    if len(token) < 32 or any(char.isspace() for char in token):
        raise ToolError("BAD_CONFIG", "token must contain at least 32 non-whitespace characters")
    # Match the existing Fabric client policy on POSIX without requiring a
    # second copy of its token implementation.
    try:
        import os
        import stat

        info = path.stat()
        if os.name != "nt" and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid()):
            raise ToolError("BAD_CONFIG", "token_file must be owned by the current user and private (chmod 600)")
    except OSError as exc:
        raise ToolError("BAD_CONFIG", "unable to inspect token_file") from exc
    return token


def _multipart_audio(data: bytes, filename: str, *, model: str, run_id: str) -> tuple[bytes, str]:
    boundary = "----ZeYuBoundary" + uuid.uuid4().hex
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"run_id\"\r\n\r\n{run_id}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
            f"filename=\"{_safe_filename(filename)}\"\r\nContent-Type: audio/wav\r\n\r\n"
        ).encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _safe_filename(value: str) -> str:
    name = Path(value).name
    if not name or name in {".", ".."} or any(char in name for char in '\r\n\x00"'):
        return "audio.wav"
    return name


def _write_new_file(destination: Path, data: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ToolError("OUTPUT_EXISTS", "refusing to overwrite an existing artifact")
    temporary = destination.with_name(destination.name + ".partial-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            destination.hardlink_to(temporary)
            _sync_directory(destination.parent)
        except FileExistsError as exc:
            raise ToolError("OUTPUT_EXISTS", "refusing to overwrite an existing artifact") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _json(payload: bytes, operation: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ToolError("INVALID_RESPONSE", f"Bento {operation} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ToolError("INVALID_RESPONSE", f"Bento {operation} returned a JSON object")
    return value


def _operation_header(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (ValueError, UnicodeError):
        return {"raw": value[:2048]}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _remote_detail(payload: bytes) -> str:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        return payload.decode("utf-8", "replace")[:512] or "request rejected"
    if isinstance(value, dict):
        for key in ("error", "message", "detail"):
            if key in value:
                return str(value[key])[:512]
    return "request rejected"


def _structured_remote_error(payload: bytes, status: int) -> tuple[str, str, Any]:
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError):
        value = None
    remote = value if isinstance(value, (dict, list, str, int, float, bool)) else None
    error = value.get("error") if isinstance(value, dict) else None
    if isinstance(error, dict):
        code = error.get("code") or value.get("error_code") or value.get("code")
        message = error.get("message") or error.get("detail") or value.get("message")
    else:
        code = value.get("error_code") or value.get("code") if isinstance(value, dict) else None
        message = value.get("message") if isinstance(value, dict) else None
    if not isinstance(code, str) or not code:
        code = "REMOTE_BAD_REQUEST" if 400 <= status < 500 else "REMOTE_ERROR"
    if not isinstance(message, str) or not message:
        message = _remote_detail(payload)
    return code, f"Bento HTTP {status}: {message}", remote


def _sync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_json_new_file(destination: Path, value: Mapping[str, Any]) -> None:
    try:
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ToolError("INVALID_RESPONSE", "operation metadata is not JSON-safe") from exc
    _write_new_file(destination, data)


def _validated_operation(value, run_id, model, input_sha, output_sha, output_bytes, input_bytes):
    try:
        operation = json.loads(value) if value else None
        if not isinstance(operation, dict):
            raise ValueError("missing operation object")
        # Reject NaN/Infinity anywhere before publishing the WAV.
        json.dumps(operation, allow_nan=False)
        remote_model = operation.get("model")
        parameters = operation.get("parameters")
        artifact = operation.get("artifact")
        if not all(isinstance(item, dict) for item in (remote_model, parameters, artifact)):
            raise ValueError("missing model, parameters or artifact identity")
        matches = (
            operation.get("ok") is True,
            operation.get("operation") == "infer_audio",
            operation.get("run_id") == run_id,
            remote_model.get("alias") == model,
            parameters.get("input_sha256") == input_sha,
            type(parameters.get("input_bytes")) is int and parameters["input_bytes"] == input_bytes,
            operation.get("artifact_sha256") == output_sha,
            artifact.get("sha256") == output_sha,
            type(artifact.get("bytes")) is int and artifact["bytes"] == output_bytes,
        )
        if not all(matches):
            raise ValueError("response identity, byte count or hash mismatch")
        return operation
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ToolError("INVALID_RESPONSE", "Bento inference metadata is missing, malformed or does not match the request and WAV") from exc
