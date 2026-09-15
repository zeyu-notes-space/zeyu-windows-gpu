#!/usr/bin/env python3
"""Real Mac -> SSH -> Windows -> RTX 5070 Ti acceptance. No simulation mode."""
import argparse
import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zeyu_fabric.cli import Client, ClientError, load_config, ssh_command, TERMINAL


def require(condition, message):
    if not condition:
        raise AssertionError(str(message))


def now():
    return datetime.now(timezone.utc).isoformat()


class Acceptance:
    def __init__(self, args):
        self.args = args
        self.config = load_config(args.config)
        self.client = Client(self.config, timeout=5, attempts=1)
        self.output = args.output.expanduser().resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.tunnel = None
        self.tunnel_log = (self.output / "ssh.log").open("ab")
        self.report = {"schema_version": 1, "started_at": now(), "runner_system": sys.platform,
                       "WINDOWS_INTEGRATION": "PENDING", "GPU_VALIDATION": "PENDING",
                       "MAC_TO_WINDOWS_COMPUTE": "NOT_PASS", "checks": [], "jobs": []}
        self.persist()

    def persist(self):
        temporary = self.output / "report.json.tmp"
        temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.output / "report.json")

    def check(self, name, operation):
        print("Checking " + name, flush=True)
        item = {"name": name, "started_at": now()}
        try:
            item["evidence"] = operation()
            item["status"] = "PASS"
        except Exception as exc:
            item.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc))
        item["ended_at"] = now()
        self.report["checks"].append(item)
        self.persist()
        print(name + " = " + item["status"], flush=True)
        return item["status"] == "PASS"

    def start_tunnel(self):
        port = urlsplit(self.config["url"]).port
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError("Local port already occupied. Stop zrun connect first; acceptance manages its own tunnel.") from exc
        command = ssh_command(self.config)
        command[1:1] = ["-o", "BatchMode=yes"]
        self.tunnel = subprocess.Popen(command, stdout=self.tunnel_log, stderr=self.tunnel_log)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.tunnel.poll() is not None:
                raise RuntimeError("SSH tunnel failed; see ssh.log (verify key authentication and host fingerprint first)")
            try:
                return self.client.json("/v1/health")
            except ClientError:
                time.sleep(0.5)
        raise RuntimeError("SSH opened but authenticated worker health did not become available")

    def stop_tunnel(self):
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait(timeout=5)
        self.tunnel = None

    def spec(self, example, arguments=(), timeout=90, gpu=False):
        return {"project": self.args.project, "git_commit": self.args.commit,
                "environment": self.args.environment, "command": ["{python}", "-u", "examples/" + example + ".py"],
                "arguments": list(arguments), "timeout": timeout, "artifact_paths": [],
                "resources": {"gpu": gpu}}

    def submit(self, spec):
        key = str(uuid.uuid4())
        # Persist before network transmission so an ambiguous submit remains recoverable.
        record = {"idempotency_key": key, "spec": spec, "submitted_at": now()}
        self.report["jobs"].append(record)
        self.persist()
        job = self.client.submit(spec, key)
        record["job_id"] = job["job_id"]
        self.persist()
        return job["job_id"]

    def wait(self, job_id, states=TERMINAL, seconds=240):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            job = self.client.status(job_id)
            if job["state"] in states:
                return job
            if job["state"] in TERMINAL:
                raise AssertionError("Job ended before expected state: " + json.dumps(job))
            time.sleep(0.5)
        raise TimeoutError("Timed out waiting for " + job_id)

    def collect(self, job_id):
        self.client.artifacts(job_id, self.output / "runs")
        return self.output / "runs" / job_id

    def wait_workload_started(self, job_id):
        self.wait(job_id, {"RUNNING"})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            page = self.client.json("/v1/jobs/" + job_id + "/logs")
            if page["text"]:
                return
            time.sleep(0.2)
        raise RuntimeError("Heartbeat workload did not emit its first sample")

    def remote_powershell(self, script):
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                   "-o", "ForwardAgent=no", self.config["ssh_host"], "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        require(result.returncode == 0, result.stderr)
        return result.stdout

    def verify_process_tree_stopped(self, local):
        tree = json.loads((local / "artifacts/process-tree.json").read_text(encoding="utf-8"))
        ids = [tree.get("parent_pid"), tree.get("child_pid")]
        require(all(type(pid) is int and pid > 0 for pid in ids), "Missing real process-tree identities")
        expression = " or ".join("ProcessId = " + str(pid) for pid in ids)
        script = "$ErrorActionPreference='Stop'; $rows=@(Get-CimInstance Win32_Process -Filter '" + expression + "' | ForEach-Object { @{pid=[int]$_.ProcessId; created=([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds()/1000.0; command_line=$_.CommandLine} }); ConvertTo-Json -InputObject $rows -Compress"
        observed = json.loads(self.remote_powershell(script).strip() or "[]")
        for entry in observed:
            role = "parent" if entry["pid"] == tree["parent_pid"] else "child"
            created = tree.get(role + "_create_time")
            require(isinstance(created, (int, float)), "Missing Windows process creation time")
            same_process = abs(entry["created"] - created) < 0.01
            same_child = tree["child_nonce"] in (entry.get("command_line") or "")
            require(not same_process and not same_child, "Interrupted process is still alive: " + str(entry["pid"]))
        return {"parent_pid": tree["parent_pid"], "child_pid": tree["child_pid"], "stopped": True}

    def completed_example(self, example, artifact, gpu=False):
        job_id = self.submit(self.spec(example, gpu=gpu))
        final = self.wait(job_id)
        local = self.collect(job_id)
        require(final['state'] == 'COMPLETED', final)
        data = json.loads((local / "artifacts" / artifact).read_text(encoding="utf-8"))
        require(data['status'] == 'PASS', data)
        require(data['system'] == 'Windows', 'This is not a Windows worker; cannot pass physical acceptance')
        require(data['job_id'] == job_id, 'Acceptance check failed')
        return job_id, final, local, data

    def smoke(self):
        job_id, final, local, data = self.completed_example("smoke", "smoke-result.json")
        stdout, stderr = io.StringIO(), io.StringIO()
        self.client.logs(job_id, output=stdout)
        self.client.logs(job_id, "stderr", output=stderr)
        require('SMOKE_PASS' in stdout.getvalue(), 'Acceptance check failed')
        require('SMOKE_STDERR_MARKER' in stderr.getvalue(), 'Acceptance check failed')
        require((local / 'metrics/samples.jsonl').is_file(), 'Acceptance check failed')
        self.report["workers"] = self.client.json("/v1/workers")
        return {"job_id": job_id, "artifact": str(local.relative_to(self.output)), "system": data["system"]}

    def cuda(self):
        job_id, final, local, data = self.completed_example("cuda_smoke", "cuda-result.json", gpu=True)
        require('RTX 5070 Ti Laptop'.lower() in data['device_name'].lower(), data)
        require(data['execution_device'].startswith('cuda:'), data)
        require(data['reference_check'] == 'PASS' and data['cuda_synchronized'] is True, data)
        require(data['vram_total_bytes'] >= 10 * 1024 ** 3, data)
        require(data.get('nvidia_smi', {}).get('exit_code') == 0, data)
        require('rtx 5070 ti laptop' in data['nvidia_smi']['stdout'].lower(), data)
        return {"job_id": job_id, "device_name": data["device_name"], "vram_total_bytes": data["vram_total_bytes"],
                "artifact": str((local / "artifacts/cuda-result.json").relative_to(self.output))}

    def failure(self):
        job_id = self.submit(self.spec("fail"))
        final = self.wait(job_id)
        local = self.collect(job_id)
        require(final['state'] == 'FAILED', final)
        require(final.get("failure", {}).get("code") == "PYTHON_EXCEPTION", final)
        require('ZEYU_INTENTIONAL_FAILURE' in (local / 'logs/stderr.log').read_text(encoding='utf-8'), final)
        require((local / 'artifacts/failure-context.json').exists(), 'Acceptance check failed')
        return {"job_id": job_id, "diagnostic": "Python traceback and failure artifact retained"}

    def invalid_command(self):
        spec = self.spec("smoke")
        spec["command"] = ["zeyu-command-deliberately-does-not-exist-971531"]
        job_id = self.submit(spec)
        final = self.wait(job_id)
        self.collect(job_id)
        require(final['state'] == 'FAILED', final)
        require(final.get('failure', {}).get('code') == 'COMMAND_NOT_FOUND', final)
        return {"job_id": job_id}

    def oom(self):
        job_id = self.submit(self.spec("oom", timeout=90, gpu=True))
        final = self.wait(job_id)
        local = self.collect(job_id)
        data = json.loads((local / "artifacts/oom-result.json").read_text())
        require(final['state'] == 'FAILED', final)
        require(data['status'] == 'CUDA_OOM_OBSERVED', data)
        require(final.get("failure", {}).get("code") == "GPU_OUT_OF_MEMORY", final)
        require('OutOfMemoryError' in (local / 'logs/stderr.log').read_text(encoding='utf-8'), 'Acceptance check failed')
        return {"job_id": job_id, "observed": data["status"]}

    def timeout(self):
        job_id = self.submit(self.spec("timeout", ["--seconds", "60", "--spawn-child"], timeout=12))
        final = self.wait(job_id)
        local = self.collect(job_id)
        require(final['state'] == 'FAILED' and final.get('failure', {}).get('code') == 'TIMEOUT', final)
        require((local / 'artifacts/heartbeat.json').exists(), 'Workload never reached RUNNING before timeout')
        return {"job_id": job_id, "process_tree": self.verify_process_tree_stopped(local)}

    def cancellation(self):
        job_id = self.submit(self.spec("timeout", ["--seconds", "60", "--spawn-child"]))
        self.wait_workload_started(job_id)
        self.client.json("/v1/jobs/" + job_id + "/cancel", {})
        final = self.wait(job_id)
        local = self.collect(job_id)
        require(final['state'] == 'CANCELLED', final)
        return {"job_id": job_id, "process_tree": self.verify_process_tree_stopped(local)}

    def network(self):
        job_id = self.submit(self.spec("timeout", ["--seconds", "15"]))
        self.wait(job_id, {"RUNNING"})
        first = self.client.json("/v1/jobs/" + job_id + "/logs")
        self.stop_tunnel()
        try:
            self.client.json("/v1/health")
        except ClientError:
            pass
        else:
            raise AssertionError("Stopping our tunnel did not actually break the connection")
        time.sleep(3)
        self.start_tunnel()
        final = self.wait(job_id)
        local = self.collect(job_id)
        require(final['state'] == 'COMPLETED', final)
        resumed = io.StringIO()
        self.client.logs(job_id, offset=first["next_offset"], output=resumed)
        require('HEARTBEAT_COMPLETED' in resumed.getvalue(), 'Acceptance check failed')
        return {"job_id": job_id, "disconnect_seconds": 3, "resumed_offset": first["next_offset"]}

    def restart(self):
        if not self.args.allow_worker_restart:
            raise RuntimeError("Worker restart was not enabled; rerun with --allow-worker-restart while the node is idle")
        active = self.submit(self.spec("timeout", ["--seconds", "180", "--spawn-child"], timeout=240))
        self.wait_workload_started(active)
        queued = self.submit(self.spec("smoke"))
        require(self.client.status(queued)['state'] == 'QUEUED', 'Acceptance check failed')
        before = self.client.json("/v1/health")
        pid = before["pid"]
        require(type(pid) is int and pid > 0, 'Acceptance check failed')
        # Terminate only the PID reported by this authenticated worker, after checking
        # its command line. The same standard account owns the worker and SSH session.
        script = "$ErrorActionPreference='Stop'; $p=Get-CimInstance Win32_Process -Filter 'ProcessId = " + str(pid) + "'; if (-not $p -or $p.CommandLine -notmatch 'zeyu_fabric[.]server') { throw 'PID is not a ZeYu worker' }; Stop-Process -Id " + str(pid) + " -Force"
        self.remote_powershell(script)
        deadline = time.monotonic() + 150
        after = None
        while time.monotonic() < deadline:
            try:
                candidate = self.client.json("/v1/health")
                if candidate["instance_id"] != before["instance_id"]:
                    after = candidate
                    break
            except ClientError:
                pass
            time.sleep(2)
        require(after, 'Task Scheduler did not restart the worker within 150 seconds')
        interrupted = self.wait(active)
        resumed = self.wait(queued)
        interrupted_local = self.collect(active)
        self.collect(queued)
        require(interrupted['state'] == 'FAILED' and interrupted.get('failure', {}).get('code') == 'WORKER_INTERRUPTED', interrupted)
        require(resumed['state'] == 'COMPLETED', resumed)
        return {"interrupted_job": active, "queued_job": queued, "before": before["instance_id"], "after": after["instance_id"],
                "process_tree": self.verify_process_tree_stopped(interrupted_local)}

    def run(self):
        try:
            if sys.platform != "darwin":
                raise RuntimeError("Full physical acceptance must be initiated from the Mac")
            self.start_tunnel()
            busy = [job for job in self.client.status() if job["state"] not in TERMINAL]
            if busy:
                raise RuntimeError("Worker has existing active/queued jobs; run destructive acceptance only on an idle node")
            if not self.check("windows_python_smoke_logs_artifacts", self.smoke):
                return 1
            self.check("rtx_5070_ti_cuda", self.cuda)
            for name, function in (("python_exception", self.failure), ("invalid_command", self.invalid_command),
                                   ("gpu_oom", self.oom), ("timeout", self.timeout), ("cancel", self.cancellation),
                                   ("ssh_interruption", self.network), ("worker_restart_recovery", self.restart)):
                self.check(name, function)
            passed = {check["name"] for check in self.report["checks"] if check["status"] == "PASS"}
            self.report["GPU_VALIDATION"] = "PASS" if "rtx_5070_ti_cuda" in passed else "FAIL"
            required = {"windows_python_smoke_logs_artifacts", "rtx_5070_ti_cuda", "python_exception", "invalid_command",
                        "gpu_oom", "timeout", "cancel", "ssh_interruption", "worker_restart_recovery"}
            all_passed = required <= passed
            self.report["WINDOWS_INTEGRATION"] = "PASS" if all_passed else "INCOMPLETE"
            self.report["MAC_TO_WINDOWS_COMPUTE"] = "PASS" if all_passed else "NOT_PASS"
            return 0 if all_passed else 1
        except Exception as exc:
            self.report["blocking_error"] = type(exc).__name__ + ": " + str(exc)
            return 2
        finally:
            self.report["ended_at"] = now()
            self.persist()
            self.stop_tunnel()
            self.tunnel_log.close()
            print(json.dumps({key: self.report[key] for key in ("WINDOWS_INTEGRATION", "GPU_VALIDATION", "MAC_TO_WINDOWS_COMPUTE")}, indent=2))
            print("Report: " + str(self.output / "report.json"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="~/.config/zeyu-fabric/client.json")
    parser.add_argument("--info", type=Path, help="acceptance-info.json generated on Windows")
    parser.add_argument("--commit")
    parser.add_argument("--project", default="fabric-acceptance")
    parser.add_argument("--environment", default="acceptance")
    parser.add_argument("--output", type=Path, default=Path("test-results") / ("windows-" + time.strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--allow-worker-restart", action="store_true", help="Kill this worker's PID and verify automatic restart on an idle node")
    args = parser.parse_args(argv)
    if args.info:
        info = json.loads(args.info.read_text(encoding="utf-8-sig"))
        args.commit = args.commit or info["git_commit"]
        args.project = info.get("project", args.project)
        args.environment = info.get("environment", args.environment)
    if not args.commit:
        parser.error("provide --info or --commit from the Windows acceptance repository")
    try:
        return Acceptance(args).run()
    except (ClientError, OSError, ValueError) as exc:
        print("Acceptance could not start: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
