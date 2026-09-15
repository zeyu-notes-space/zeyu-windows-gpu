"""Small cross-platform process-tree containment adapter."""

import os
import signal
import subprocess


class PosixProcess:
    def __init__(self, argv, cwd, env, stdout, stderr):
        self._process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True, close_fds=True)
        self.pid = self._process.pid
        self._terminated = False

    def poll(self):
        return self._process.poll()

    def terminate(self):
        # Kill the group even when the direct child already exited: its children
        # may still be running and holding the logs open.
        if self._terminated:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._terminated = True

    def wait(self, timeout=5):
        return self._process.wait(timeout=timeout)

    def close(self):
        self.terminate()
        self.wait(timeout=5)


def spawn(argv, cwd, env, stdout, stderr):
    if os.name == "nt":
        from .windows_job import WindowsJobProcess
        return WindowsJobProcess(argv, cwd, env, stdout, stderr)
    return PosixProcess(argv, cwd, env, stdout, stderr)
