"""Real CUDA matrix multiplication and device identity; no CPU fallback."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int, default=0)
    parser.add_argument("--matrix-size", type=int, default=1024)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--expected-name", default="RTX 5070 Ti Laptop")
    args = parser.parse_args()
    if not 64 <= args.matrix_size <= 4096 or not 1 <= args.iterations <= 10000:
        parser.error("matrix-size must be 64..4096 and iterations 1..10000")
    artifact_dir = Path(os.environ["ZRUN_ARTIFACT_DIR"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "status": "FAIL", "kind": "cuda_smoke", "job_id": os.environ.get("ZRUN_JOB_ID"),
        "created_at": datetime.now(timezone.utc).isoformat(), "hostname": socket.gethostname(),
        "system": platform.system(), "platform": platform.platform(), "python": sys.version,
        "expected_name": args.expected_name, "device_index": args.device,
        "matrix_size": args.matrix_size, "iterations": args.iterations,
    }
    try:
        import torch
        result.update(torch_version=torch.__version__, cuda_runtime=torch.version.cuda,
                      cuda_available=torch.cuda.is_available(), compiled_arches=torch.cuda.get_arch_list())
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA_UNAVAILABLE: install a CUDA-enabled PyTorch build and verify NVIDIA driver")
        torch.cuda.set_device(args.device)
        props = torch.cuda.get_device_properties(args.device)
        result.update(device_name=props.name, vram_total_bytes=props.total_memory,
                      compute_capability=list(torch.cuda.get_device_capability(args.device)))
        print(json.dumps({"cuda_device": result["device_name"], "vram_bytes": props.total_memory,
                          "torch": torch.__version__, "cuda": torch.version.cuda}), flush=True)
        if args.expected_name.lower() not in props.name.lower():
            raise RuntimeError("WRONG_GPU: expected {!r}, found {!r}".format(args.expected_name, props.name))
        try:
            smi = subprocess.run(["nvidia-smi", "--query-gpu=name,uuid,driver_version,memory.total",
                                  "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("NVIDIA_SMI_UNAVAILABLE: " + type(exc).__name__ + ": " + str(exc)) from exc
        result["nvidia_smi"] = {"exit_code": smi.returncode, "stdout": smi.stdout.strip(), "stderr": smi.stderr.strip()}
        if smi.returncode != 0:
            raise RuntimeError("NVIDIA_SMI_FAILED: " + (smi.stderr.strip() or "exit " + str(smi.returncode)))
        if args.expected_name.lower() not in smi.stdout.lower():
            raise RuntimeError("NVIDIA_SMI_WRONG_GPU: expected {!r} in {!r}".format(args.expected_name, smi.stdout.strip()))
        torch.manual_seed(2026)
        # Check a small complete multiplication against CPU, then time a larger GPU workload.
        torch.backends.cuda.matmul.allow_tf32 = False
        sample_a = torch.randn(64, 64, dtype=torch.float32)
        sample_b = torch.randn(64, 64, dtype=torch.float32)
        actual = (sample_a.to("cuda") @ sample_b.to("cuda")).cpu()
        expected = sample_a @ sample_b
        torch.testing.assert_close(actual, expected, rtol=1e-4, atol=1e-4)
        result["cpu_reference_max_abs_error"] = float((actual - expected).abs().max())
        left = torch.randn(args.matrix_size, args.matrix_size, device="cuda", dtype=torch.float32)
        right = torch.randn(args.matrix_size, args.matrix_size, device="cuda", dtype=torch.float32)
        torch.cuda.reset_peak_memory_stats()
        for _ in range(3):
            output = left @ right
        torch.cuda.synchronize()
        started = time.perf_counter()
        for iteration in range(args.iterations):
            output = left @ right
            if iteration % max(1, args.iterations // 5) == 0:
                print(json.dumps({"iteration": iteration + 1, "iterations": args.iterations}), flush=True)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - started
        if output.device.type != "cuda" or not bool(torch.isfinite(output).all()):
            raise RuntimeError("CUDA_OUTPUT_INVALID: output was not finite CUDA data")
        result.update(status="PASS", execution_device=str(output.device), workload="float32_matmul",
                      elapsed_seconds=elapsed, checksum=float(output.double().sum().item()),
                      peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                      peak_reserved_bytes=torch.cuda.max_memory_reserved(),
                      reference_check="PASS", cuda_synchronized=True)
        print("CUDA_PASS device=" + props.name, flush=True)
    except BaseException as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        raise
    finally:
        (artifact_dir / "cuda-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
