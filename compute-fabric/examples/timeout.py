"""Heartbeat job for timeout/cancel/restart/network tests; set job timeout shorter."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

parser = argparse.ArgumentParser()
parser.add_argument("--seconds", type=float, default=120)
parser.add_argument("--spawn-child", action="store_true", help="Probe worker process-tree containment")
args = parser.parse_args()
if not 0 < args.seconds <= 604800:
    parser.error("seconds must be positive and <= 604800")
artifact_dir = Path(os.environ["ZRUN_ARTIFACT_DIR"])
artifact_dir.mkdir(parents=True, exist_ok=True)


def windows_create_time(process_id):
    """Query our own processes without requiring psutil in the workload environment."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, process_id)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created, exited, kernel, user = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return (ticks - 116444736000000000) / 10000000
    finally:
        kernel32.CloseHandle(handle)


child = None
if args.spawn_child:
    child_nonce = "zeyu-child-" + uuid.uuid4().hex
    child_code = "import sys,time; print('CHILD_STARTED '+sys.argv[1],flush=True); time.sleep(float(sys.argv[2]))"
    # The child deliberately outlives the parent. Worker containment must reap it, including on restart.
    # It has a finite upper bound and allocates no extra memory or GPU resources if containment fails.
    child = subprocess.Popen([sys.executable, "-u", "-c", child_code, child_nonce, str(min(args.seconds + 30, 630))])
    tree = {
        "parent_pid": os.getpid(), "child_pid": child.pid, "child_nonce": child_nonce,
        "parent_create_time": windows_create_time(os.getpid()),
        "child_create_time": windows_create_time(child.pid),
        "created_at": datetime.now(timezone.utc).isoformat(), "executable": sys.executable,
        "job_id": os.environ.get("ZRUN_JOB_ID"),
    }
    (artifact_dir / "process-tree.json").write_text(json.dumps(tree, indent=2), encoding="utf-8")
    print(json.dumps({"process_tree": tree}), flush=True)
heartbeat = artifact_dir / "heartbeat.json"
started = time.monotonic()
while time.monotonic() - started < args.seconds:
    result = {"job_id": os.environ.get("ZRUN_JOB_ID"), "elapsed_seconds": time.monotonic() - started,
              "timestamp": datetime.now(timezone.utc).isoformat(), "status": "RUNNING"}
    temporary = heartbeat.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temporary.replace(heartbeat)
    print(json.dumps(result), flush=True)
    time.sleep(min(1, args.seconds))
print("HEARTBEAT_COMPLETED", flush=True)
