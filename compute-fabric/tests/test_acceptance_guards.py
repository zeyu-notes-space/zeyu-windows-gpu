"""Negative acceptance guards, never a substitute for a physical Windows run.

Synthetic dictionaries below exercise extracted validation predicates only. They
do not execute jobs, mock CUDA, or write a physical acceptance PASS report.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest


PACKAGE = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE / "scripts" / "acceptance.py"
SOURCE = SCRIPT.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, filename=str(SCRIPT))
ACCEPTANCE = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "Acceptance")


def method_node(name):
    return next(node for node in ACCEPTANCE.body if isinstance(node, ast.FunctionDef) and node.name == name)


def predicates(method, variable):
    """Use actual guard expressions, including old substring guards if regressed."""
    found = []
    for node in ast.walk(method_node(method)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "require" and node.args):
            condition = node.args[0]
            names = {part.id for part in ast.walk(condition) if isinstance(part, ast.Name)}
            if variable in names:
                expression = ast.fix_missing_locations(ast.Expression(body=condition))
                found.append(compile(expression, str(SCRIPT), "eval", optimize=2))
    if not found:
        raise AssertionError("No guard uses {} in {}".format(variable, method))
    return found


def accepted_by(expressions, **values):
    scope = {"json": json, "str": str, "type": type, "int": int, **values}
    try:
        return all(eval(expression, {"__builtins__": {}}, scope) for expression in expressions)
    except (AttributeError, KeyError, TypeError):
        # Missing or malformed evidence must fail rather than silently pass.
        return False


class AcceptanceGuardTests(unittest.TestCase):
    def test_require_remains_active_in_real_optimized_python(self):
        program = (
            "import runpy,sys; "
            "namespace=runpy.run_path(sys.argv[1], run_name='acceptance_guard_test'); "
            "print('OPTIMIZATION_ACTIVE=' + str(not __debug__), flush=True); "
            "namespace['require'](False, 'REJECT_INVALID_ACCEPTANCE_EVIDENCE')"
        )
        for optimization in ("-O", "-OO"):
            with self.subTest(optimization=optimization):
                result = subprocess.run([sys.executable, optimization, "-c", program, str(SCRIPT)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("OPTIMIZATION_ACTIVE=True", result.stdout)
                self.assertIn("AssertionError: REJECT_INVALID_ACCEPTANCE_EVIDENCE", result.stderr)

    def test_physical_acceptance_has_no_optimization_removable_asserts(self):
        self.assertEqual([node.lineno for node in ast.walk(TREE) if isinstance(node, ast.Assert)], [])

    def test_failure_gates_reject_codes_hidden_in_unrelated_metadata(self):
        gates = {
            "failure": ("final", "PYTHON_EXCEPTION"),
            "invalid_command": ("final", "COMMAND_NOT_FOUND"),
            "oom": ("final", "GPU_OUT_OF_MEMORY"),
            "timeout": ("final", "TIMEOUT"),
            "restart": ("interrupted", "WORKER_INTERRUPTED"),
        }
        for method, (variable, expected) in gates.items():
            with self.subTest(method=method):
                guards = predicates(method, variable)
                manifest = {
                    "state": "FAILED",
                    "failure": {"code": "PROCESS_EXIT", "message": "not the expected " + expected},
                    "spec": {"timeout": 12, "command": ["examples/timeout.py"], "arguments": [expected]},
                }
                self.assertFalse(accepted_by(guards, **{variable: manifest}),
                                 "Unrelated metadata must not satisfy the failure diagnosis")
                manifest["failure"] = None
                self.assertFalse(accepted_by(guards, **{variable: manifest}))
                manifest["failure"] = {"code": expected}
                self.assertTrue(accepted_by(guards, **{variable: manifest}))
                manifest["state"] = "COMPLETED"
                self.assertFalse(accepted_by(guards, **{variable: manifest}))

    def test_windows_identity_predicate_rejects_other_systems(self):
        guards = predicates("completed_example", "data")
        for system in ("Darwin", "Linux", "", None):
            with self.subTest(system=system):
                evidence = {"status": "PASS", "system": system, "job_id": "predicate-only"}
                self.assertFalse(accepted_by(guards, data=evidence, job_id="predicate-only"))

    def test_cuda_predicates_reject_cpu_wrong_gpu_and_missing_validation(self):
        guards = predicates("cuda", "data")
        fields = {
            "device_name": "NVIDIA GeForce RTX 5070 Ti Laptop GPU",
            "execution_device": "cuda:0", "reference_check": "PASS",
            "cuda_synchronized": True, "vram_total_bytes": 12 * 1024 ** 3,
        }
        invalid = [
            {"execution_device": "cpu"}, {"device_name": "NVIDIA GeForce RTX 4060 Laptop GPU"},
            {"reference_check": "FAIL"}, {"cuda_synchronized": False},
            {"vram_total_bytes": 8 * 1024 ** 3}, {"device_name": "RTX 5070 Ti Desktop GPU"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertFalse(accepted_by(guards, data={**fields, **changes}))

    def test_non_mac_guard_runs_before_any_tunnel_action(self):
        run = method_node("run")
        body = next(node for node in run.body if isinstance(node, ast.Try)).body
        guard = body[0]
        self.assertIsInstance(guard, ast.If)
        self.assertTrue(any(isinstance(node, ast.Raise) for node in ast.walk(guard)))
        gate_only = ast.fix_missing_locations(ast.Module(body=[guard], type_ignores=[]))
        compiled = compile(gate_only, str(SCRIPT), "exec", optimize=2)
        for platform in ("linux", "win32", "freebsd"):
            with self.subTest(platform=platform), self.assertRaisesRegex(RuntimeError, "initiated from the Mac"):
                exec(compiled, {"sys": SimpleNamespace(platform=platform)})

    def config(self, directory, url="http://127.0.0.1:8765"):
        token = directory / "token"
        token.write_text("a" * 64, encoding="utf-8")
        token.chmod(0o600)
        config = directory / "client.json"
        config.write_text(json.dumps({"url": url, "token_file": str(token), "ssh_host": "not-contacted"}), encoding="utf-8")
        return config

    def test_main_rejects_non_loopback_config_without_starting_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = self.config(directory, "http://192.0.2.10:8765")
            report = directory / "physical-result"
            result = subprocess.run([sys.executable, "-O", str(SCRIPT), "--config", str(config),
                                     "--commit", "a" * 40, "--output", str(report)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("127.0.0.1", result.stderr)
            self.assertFalse(report.exists(), "Invalid configuration must not produce any physical result")

    @unittest.skipUnless(sys.platform.startswith("linux"), "A real Linux runner is unavailable; no OS identity is forged")
    def test_real_linux_main_cannot_publish_windows_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = self.config(directory)
            report = directory / "physical-result"
            result = subprocess.run([sys.executable, "-O", str(SCRIPT), "--config", str(config),
                                     "--commit", "a" * 40, "--output", str(report)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            evidence = json.loads((report / "report.json").read_text())
            self.assertEqual(evidence["runner_system"], sys.platform)
            self.assertEqual(evidence["MAC_TO_WINDOWS_COMPUTE"], "NOT_PASS")
            self.assertNotEqual(evidence["GPU_VALIDATION"], "PASS")
            self.assertNotEqual(evidence["WINDOWS_INTEGRATION"], "PASS")
            self.assertEqual(evidence["checks"], [])
            self.assertEqual(evidence["jobs"], [])
            self.assertIn("initiated from the Mac", evidence["blocking_error"])


if __name__ == "__main__":
    unittest.main()
