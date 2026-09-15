"""Small deterministic CPU job, with stdout, stderr, and a durable JSON artifact."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import socket
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--message", default="Mac to Windows compute smoke test")
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args()
    if not 1 <= args.steps <= 10000 or not 0 <= args.delay <= 60:
        parser.error("steps must be 1..10000 and delay 0..60")
    artifact_dir = Path(os.environ["ZRUN_ARTIFACT_DIR"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    print(args.message, flush=True)
    print("SMOKE_STDERR_MARKER: stderr capture is working", file=sys.stderr, flush=True)
    for step in range(args.steps):
        print(json.dumps({"step": step + 1, "steps": args.steps}), flush=True)
        time.sleep(args.delay)
    result = {
        "status": "PASS", "kind": "python_smoke", "job_id": os.environ.get("ZRUN_JOB_ID"),
        "hostname": socket.gethostname(), "platform": platform.platform(),
        "system": platform.system(), "python": sys.version, "executable": sys.executable,
        "created_at": datetime.now(timezone.utc).isoformat(), "message": args.message,
        "steps": args.steps, "checksum": sum(n * n for n in range(10000)),
    }
    target = artifact_dir / "smoke-result.json"
    target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("SMOKE_PASS artifact=smoke-result.json", flush=True)


if __name__ == "__main__":
    main()
