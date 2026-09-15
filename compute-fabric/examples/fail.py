"""Expected nonzero exit, Python traceback, and artifact preserved on failure."""
import json
import os
from pathlib import Path
import sys

artifact_dir = Path(os.environ["ZRUN_ARTIFACT_DIR"])
artifact_dir.mkdir(parents=True, exist_ok=True)
(artifact_dir / "failure-context.json").write_text(json.dumps({
    "kind": "intentional_python_exception", "job_id": os.environ.get("ZRUN_JOB_ID"),
    "expected_exception": "RuntimeError", "expected_message": "ZEYU_INTENTIONAL_FAILURE",
}, indent=2), encoding="utf-8")
print("Failure probe started; artifact written before exception", flush=True)
print("FAILURE_STDERR_MARKER", file=sys.stderr, flush=True)
raise RuntimeError("ZEYU_INTENTIONAL_FAILURE: deliberate acceptance-test exception")
