#!/usr/bin/env python3
"""Run local tests and preserve an explicitly local evidence report."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Tee:
    def __init__(self, file):
        self.file = file
    def write(self, text):
        sys.stdout.write(text)
        self.file.write(text)
    def flush(self):
        sys.stdout.flush()
        self.file.flush()


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append({"test": test.id(), "status": "PASS"})
    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append({"test": test.id(), "status": "FAIL", "detail": self._exc_info_to_string(err, test)})
    def addError(self, test, err):
        super().addError(test, err)
        self.records.append({"test": test.id(), "status": "ERROR", "detail": self._exc_info_to_string(err, test)})
    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records.append({"test": test.id(), "status": "SKIPPED", "reason": reason})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "test-results" / ("local-" + time.strftime("%Y%m%d-%H%M%S")))
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ["ZEYU_TEST_EVIDENCE_DIR"] = str(output)
    started = time.monotonic()
    with (output / "unittest.log").open("w", encoding="utf-8") as log:
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(stream=Tee(log), resultclass=Result, verbosity=2).run(suite)
    passed = result.wasSuccessful() and result.testsRun > 0
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "system": platform.system(),
        "platform": platform.platform(), "python": sys.version, "duration_seconds": time.monotonic() - started,
        "tests_run": result.testsRun, "skipped": len(result.skipped), "failures": len(result.failures), "errors": len(result.errors),
        "LOCAL_IMPLEMENTATION": "PASS" if passed else "FAIL",
        "WINDOWS_INTEGRATION": "PENDING_PHYSICAL_MACHINE", "GPU_VALIDATION": "PENDING_PHYSICAL_MACHINE",
        "MAC_TO_WINDOWS_COMPUTE": "NOT_VERIFIED", "tests": result.records,
        "scope": "Real local Python/Worker/HTTP/CLI processes plus validation tests; no Windows or CUDA execution."}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Local report: " + str(output / "report.json"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
