"""Cross-process GPU lease shared with ZeYu Personal GPU Runtime."""

import json
import os
import time
from pathlib import Path


class LeaseBusyError(RuntimeError):
    def __init__(self, path, owner=None, stale=False):
        super().__init__("GPU lease has a stale HELD marker and requires recovery" if stale else
                         "GPU is reserved by another ZeYu execution path")
        self.path = str(path)
        self.owner = owner
        self.stale = stale


class GpuLease:
    def __init__(self, path, owner):
        self.path = Path(path)
        self.owner = owner
        self.handle = None

    def acquire(self, timeout=0):
        if self.handle is not None:
            return self
        if not self.path.parent.is_dir():
            raise FileNotFoundError("GPU lease parent does not exist: " + str(self.path.parent))
        deadline = time.monotonic() + timeout
        while True:
            handle = self.path.open("a+b")
            if self.path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                if time.monotonic() >= deadline:
                    raise LeaseBusyError(self.path, self.read_owner())
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                continue
            handle.seek(0)
            try:
                lines = handle.read().decode("utf-8").splitlines()
                previous = json.loads(lines[1]) if len(lines) > 1 else None
            except (UnicodeError, ValueError, json.JSONDecodeError):
                previous = None
            if isinstance(previous, dict) and previous.get("state") == "HELD":
                handle.close()
                raise LeaseBusyError(self.path, previous, stale=True)
            self.handle = handle
            encoded = ("0\n" + json.dumps({**self.owner, "state": "HELD", "pid": os.getpid(),
                                             "acquired_at_unix_ns": time.time_ns()},
                                           ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            handle.seek(0)
            handle.truncate()
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            return self

    def read_owner(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            return json.loads(lines[1]) if len(lines) > 1 else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            encoded = ("0\n" + json.dumps({**self.owner, "state": "RELEASED", "pid": os.getpid(),
                                             "released_at_unix_ns": time.time_ns()},
                                            ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            handle.seek(0)
            handle.truncate()
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
