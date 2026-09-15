"""Local-only configuration for the Windows GPU plugin client."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

from .errors import ToolError


_SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:%+\-]{0,254}$")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _path(value: Any, name: str, source: Path) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ToolError("BAD_CONFIG", f"{name} must be a non-empty path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = source.parent / candidate
    return candidate.resolve()


def _origin(value: Any, name: str) -> tuple[str, int]:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ToolError("BAD_CONFIG", f"{name} must be an HTTP 127.0.0.1 URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ToolError("BAD_CONFIG", f"{name} must be an HTTP 127.0.0.1 URL") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or not port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.netloc != f"127.0.0.1:{port}"
    ):
        raise ToolError("BAD_CONFIG", f"{name} must be an HTTP 127.0.0.1 URL")
    return f"http://127.0.0.1:{port}", port


def _aliases(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        value = list(value.keys())
    if not isinstance(value, list) or not value:
        raise ToolError("BAD_CONFIG", f"{name} must be a non-empty list or object")
    result = []
    for alias in value:
        if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
            raise ToolError("BAD_CONFIG", f"{name} contains an invalid alias")
        result.append(alias)
    if len(set(result)) != len(result):
        raise ToolError("BAD_CONFIG", f"{name} contains duplicate aliases")
    return tuple(result)


def _token_paths(value: Mapping[str, Any], source: Path) -> tuple[Path, Path]:
    shared = value.get("token_file")
    if isinstance(shared, Mapping):
        bento = shared.get("bento") or shared.get("bento_token_file")
        fabric = shared.get("fabric") or shared.get("fabric_token_file")
    else:
        bento = value.get("bento_token_file", shared)
        fabric = value.get("fabric_token_file", shared)
    if bento is None or fabric is None:
        raise ToolError("BAD_CONFIG", "token_file must provide Bento and Fabric token paths")
    return _path(bento, "bento_token_file", source), _path(fabric, "fabric_token_file", source)


@dataclass(frozen=True)
class PluginConfig:
    config_path: Path
    ssh_config: Path
    ssh_host: str
    bento_url: str
    fabric_url: str
    bento_token_file: Path
    fabric_token_file: Path
    artifact_root: Path
    known_projects: tuple[str, ...]
    known_environments: tuple[str, ...]
    fabric_root: Path | None = None
    bento_remote_port: int = 9876
    fabric_remote_port: int = 8765
    offline_timeout_seconds: float = 8.0
    request_timeout_seconds: float = 8.0
    max_audio_bytes: int = 128 * 1024 * 1024

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], source: Path) -> "PluginConfig":
        if not isinstance(raw, Mapping):
            raise ToolError("BAD_CONFIG", "client config must be a JSON object")
        bento_url, _ = _origin(raw.get("bento_url", "http://127.0.0.1:19876"), "bento_url")
        fabric_url, _ = _origin(raw.get("fabric_url", "http://127.0.0.1:18765"), "fabric_url")
        if bento_url == fabric_url:
            raise ToolError("BAD_CONFIG", "bento_url and fabric_url must use different local ports")
        ssh_config = _path(raw.get("ssh_config"), "ssh_config", source)
        ssh_host = raw.get("ssh_host")
        if not isinstance(ssh_host, str) or not _SSH_TARGET.fullmatch(ssh_host):
            raise ToolError("BAD_CONFIG", "ssh_host must be a configured SSH alias or host")
        bento_token_file, fabric_token_file = _token_paths(raw, source)
        artifact_root = _path(raw.get("artifact_root"), "artifact_root", source)
        if artifact_root.exists() and artifact_root.is_symlink():
            raise ToolError("BAD_CONFIG", "artifact_root cannot be a symlink")
        projects = _aliases(raw.get("known_projects"), "known_projects")
        environments = _aliases(raw.get("known_environments"), "known_environments")
        fabric_root = _path(raw["fabric_root"], "fabric_root", source) if raw.get("fabric_root") is not None else None
        bento_remote_port = raw.get("bento_remote_port", 9876)
        fabric_remote_port = raw.get("fabric_remote_port", 8765)
        if any(type(port) is not int or not 1 <= port <= 65535 for port in (bento_remote_port, fabric_remote_port)):
            raise ToolError("BAD_CONFIG", "remote service ports must be integers in 1..65535")
        offline = raw.get("offline_timeout_seconds", 8.0)
        request = raw.get("request_timeout_seconds", 8.0)
        max_audio = raw.get("max_audio_bytes", 128 * 1024 * 1024)
        if isinstance(offline, bool) or not isinstance(offline, (int, float)) or not 0.1 <= offline <= 8:
            raise ToolError("BAD_CONFIG", "offline_timeout_seconds must be between 0.1 and 8")
        if isinstance(request, bool) or not isinstance(request, (int, float)) or not 0.1 <= request <= 3600:
            raise ToolError("BAD_CONFIG", "request_timeout_seconds must be between 0.1 and 3600")
        if type(max_audio) is not int or not 1024 <= max_audio <= 2 * 1024**30:
            raise ToolError("BAD_CONFIG", "max_audio_bytes is outside the supported range")
        return cls(
            config_path=source,
            ssh_config=ssh_config,
            ssh_host=ssh_host,
            bento_url=bento_url,
            fabric_url=fabric_url,
            bento_token_file=bento_token_file,
            fabric_token_file=fabric_token_file,
            artifact_root=artifact_root,
            known_projects=projects,
            known_environments=environments,
            fabric_root=fabric_root,
            bento_remote_port=bento_remote_port,
            fabric_remote_port=fabric_remote_port,
            offline_timeout_seconds=float(offline),
            request_timeout_seconds=float(request),
            max_audio_bytes=max_audio,
        )


def load_config(path: str | Path) -> PluginConfig:
    source = Path(path).expanduser().resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ToolError("BAD_CONFIG", f"unable to read client config: {source}") from exc
    return PluginConfig.from_mapping(raw, source)


def validate_run_id(value: Any) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ToolError("BAD_REQUEST", "run_id must be a lowercase UUIDv4")
    return value
