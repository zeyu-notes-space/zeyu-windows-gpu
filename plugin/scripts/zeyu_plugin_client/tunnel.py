"""Lazy private SSH forwarding for the two loopback services."""

from __future__ import annotations

import subprocess
import socket
import threading
import time
from typing import Any, Callable

from .config import PluginConfig
from .errors import OfflineError, ToolError


class SSHTunnel:
    def __init__(
        self,
        config: PluginConfig,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        probe: bool = True,
    ) -> None:
        self.config = config
        self._popen = popen
        self._clock = clock
        self._sleep = sleeper
        self._probe = bool(probe)
        self._lock = threading.RLock()
        self.process: Any | None = None

    def command(self) -> list[str]:
        bento_port = _port(self.config.bento_url)
        fabric_port = _port(self.config.fabric_url)
        if not self.config.ssh_config.is_file() or self.config.ssh_config.is_symlink():
            raise ToolError("BAD_CONFIG", "ssh_config must be an existing ordinary file")
        return [
            "ssh",
            "-N",
            "-T",
            "-F",
            str(self.config.ssh_config),
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ForwardAgent=no",
            "-o",
            "GatewayPorts=no",
            "-L",
            f"127.0.0.1:{bento_port}:127.0.0.1:{self.config.bento_remote_port}",
            "-L",
            f"127.0.0.1:{fabric_port}:127.0.0.1:{self.config.fabric_remote_port}",
            self.config.ssh_host,
        ]

    def ensure(self) -> None:
        with self._lock:
            self._ensure()

    def _ensure(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        command = self.command()
        ports = (_port(self.config.bento_url), _port(self.config.fabric_url))
        if self._probe:
            self._assert_ports_free(ports)
        try:
            self.process = self._popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise OfflineError("unable to start the private SSH tunnel") from exc
        deadline = self._clock() + self.config.offline_timeout_seconds
        # Test doubles can skip socket probing; production always waits for
        # both local forwards and confirms ssh remains alive.
        if not self._probe:
            if self.process.poll() is not None:
                raise OfflineError("private SSH tunnel exited before forwarding")
            return
        while self._clock() < deadline:
            code = self.process.poll()
            if code is not None:
                detail = "private SSH tunnel exited with status " + str(code)
                raise OfflineError(detail)
            ready = True
            for port in ports:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=min(0.25, max(0.01, deadline - self._clock()))):
                        pass
                except (OSError, socket.timeout):
                    ready = False
                    break
            if ready and self.process.poll() is None:
                return
            self._sleep(min(0.05, max(0.001, deadline - self._clock())))
        self.close()
        raise OfflineError("private SSH tunnel did not become ready before the offline deadline")

    def _assert_ports_free(self, ports: tuple[int, int]) -> None:
        """Reject a local listener before SSH is allowed to claim a forward."""
        for port in ports:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                # Like OpenSSH, allow re-binding after accepted connections
                # enter TIME_WAIT; an active listener still fails this bind.
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise ToolError("LOCAL_TUNNEL_BUSY", f"local tunnel port {port} is occupied or unavailable; another Windows GPU connection may be active") from exc
            finally:
                probe.close()

    def close(self) -> None:
        with self._lock:
            process, self.process = self.process, None
            if process is None or process.poll() is not None:
                return
            try:
                process.terminate()
                process.wait(timeout=1)
            except (AttributeError, OSError, TimeoutError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass


def _port(url: str) -> int:
    return int(url.rsplit(":", 1)[1])
