"""Best-effort host telemetry; missing GPUs and tools are explicit, not zeroes."""

import csv
import io
import os
import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone

import psutil


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _nvidia_smi():
    candidate = shutil.which("nvidia-smi")
    if candidate:
        return candidate
    if os.name == "nt":
        for relative in ("System32/nvidia-smi.exe",):
            candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), relative)
            if os.path.isfile(candidate):
                return candidate
        candidate = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def gpu_metrics():
    executable = _nvidia_smi()
    if not executable:
        return {"available": False, "devices": [], "error": "nvidia-smi is not installed or is not discoverable"}
    fields = ("index", "uuid", "name", "driver_version", "memory.total", "memory.used", "utilization.gpu", "temperature.gpu")
    try:
        result = subprocess.run(
            [executable, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode:
            return {"available": False, "devices": [], "error": result.stderr.strip()[:2000] or "nvidia-smi exit " + str(result.returncode)}
        devices = []
        for values in csv.reader(io.StringIO(result.stdout)):
            if len(values) != len(fields):
                continue
            values = [v.strip() for v in values]
            item = dict(zip(("index", "uuid", "name", "driver_version", "vram_total_mb", "vram_used_mb", "utilization_percent", "temperature_c"), values))
            for field in ("index", "vram_total_mb", "vram_used_mb", "utilization_percent", "temperature_c"):
                try:
                    item[field] = float(item[field]) if "." in item[field] else int(item[field])
                except ValueError:
                    item[field] = None
            devices.append(item)
        return {"available": bool(devices), "devices": devices, "error": None if devices else "nvidia-smi returned no GPU devices"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "devices": [], "error": str(exc)}


def host_identity():
    uname = platform.uname()
    cpu_name = platform.processor() or uname.processor or "unknown"
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                cpu_name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    return {
        "hostname": platform.node(),
        "os": {"system": uname.system, "release": uname.release, "version": uname.version, "machine": uname.machine},
        "windows_version": platform.version() if os.name == "nt" else None,
        "cpu": {"name": cpu_name, "logical_cores": psutil.cpu_count(), "physical_cores": psutil.cpu_count(logical=False)},
        "ram_total_bytes": psutil.virtual_memory().total,
        "gpu": gpu_metrics(),
        "worker_python": {"version": platform.python_version(), "implementation": platform.python_implementation()},
    }


def sample_metrics(pid=None):
    memory = psutil.virtual_memory()
    sample = {
        "timestamp": utc_now(),
        "monotonic_seconds": time.monotonic(),
        "cpu_utilization_percent": psutil.cpu_percent(interval=None),
        "memory": {"total_bytes": memory.total, "used_bytes": memory.used, "available_bytes": memory.available, "utilization_percent": memory.percent},
        "gpu": gpu_metrics(),
    }
    if pid is not None:
        try:
            process = psutil.Process(pid)
            members = [process] + process.children(recursive=True)
            rss = 0
            cpu_seconds = 0.0
            live = 0
            for member in members:
                try:
                    rss += member.memory_info().rss
                    cpu_time = member.cpu_times()
                    cpu_seconds += cpu_time.user + cpu_time.system
                    live += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            sample["process_tree"] = {"pid": pid, "process_count": live, "rss_bytes": rss, "cpu_seconds": cpu_seconds}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            sample["process_tree"] = {"pid": pid, "unavailable": True}
    return sample
