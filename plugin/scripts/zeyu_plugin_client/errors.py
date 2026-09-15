"""Stable, JSON-safe errors for the local Windows GPU client layer."""

from __future__ import annotations

from typing import Any, Mapping


WINDOWS_GPU_OFFLINE = "WINDOWS_GPU_OFFLINE"


class ToolError(Exception):
    """An expected tool failure with a deterministic envelope.

    The MCP wrapper can expose ``envelope`` directly without parsing a
    traceback.  ``metadata`` is kept separate from ``error`` so an unknown
    remote outcome remains explicit and cannot be mistaken for a failure.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        status: str = "ERROR",
    ) -> None:
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})
        self.metadata = {
            "run_id": None,
            "model": None,
            "checkpoint": None,
            "git_commit": None,
            "hardware": None,
            "runtime": None,
            "parameters": {},
            "status": status,
            "error": None,
            "artifact_hashes": {},
            **dict(metadata or {}),
        }
        self.status = str(status)
        error = {"code": self.code, "message": self.message, **self.details}
        if self.metadata["error"] is None:
            self.metadata["error"] = dict(error)
        self.envelope = {"status": self.status, "error": error, "metadata": self.metadata}
        super().__init__(self.message)

    def with_metadata(self, metadata: Mapping[str, Any], *, status: str | None = None) -> "ToolError":
        merged = dict(self.metadata)
        merged.update(metadata)
        return ToolError(
            self.code,
            self.message,
            details=self.details,
            metadata=merged,
            status=status or self.status,
        )


class OfflineError(ToolError):
    def __init__(self, message: str = "Windows GPU endpoint is unreachable", **kwargs: Any) -> None:
        super().__init__(WINDOWS_GPU_OFFLINE, message, **kwargs)


class OutcomeUnknownError(ToolError):
    def __init__(self, message: str = "Remote inference outcome is unknown", **kwargs: Any) -> None:
        details = {"outcome_unknown": True, **dict(kwargs.pop("details", {}) or {})}
        metadata = dict(kwargs.pop("metadata", {}) or {})
        metadata.setdefault("status", "UNKNOWN")
        metadata.setdefault("error", {"code": "OUTCOME_UNKNOWN", **details})
        super().__init__("OUTCOME_UNKNOWN", message, details=details, metadata=metadata, status="UNKNOWN", **kwargs)
