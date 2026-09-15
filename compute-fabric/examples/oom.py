"""Make one real over-VRAM CUDA allocation. No repeated exhaustion or fake OOM."""
import json
import os
from pathlib import Path
import sys

artifact_dir = Path(os.environ["ZRUN_ARTIFACT_DIR"])
artifact_dir.mkdir(parents=True, exist_ok=True)
result = {"kind": "real_cuda_oom_probe", "status": "NOT_OBSERVED", "job_id": os.environ.get("ZRUN_JOB_ID")}
try:
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA_UNAVAILABLE: cannot test GPU OOM without CUDA")
    props = torch.cuda.get_device_properties(0)
    requested_bytes = props.total_memory + 1024 * 1024 * 1024
    result.update(device_name=props.name, vram_total_bytes=props.total_memory,
                  requested_bytes=requested_bytes, torch_version=torch.__version__,
                  cuda_runtime=torch.version.cuda)
    print(json.dumps({"probe": "single_overcapacity_allocation", "requested_bytes": requested_bytes,
                      "physical_vram_bytes": props.total_memory}), flush=True)
    try:
        allocation = torch.empty((requested_bytes,), dtype=torch.uint8, device="cuda:0")
        torch.cuda.synchronize()
    except torch.cuda.OutOfMemoryError as exc:
        result.update(status="CUDA_OOM_OBSERVED", error_type=type(exc).__name__, error=str(exc))
        print("CUDA_OOM_OBSERVED: actual PyTorch allocator rejected the request", file=sys.stderr, flush=True)
        raise
    else:
        del allocation
        torch.cuda.empty_cache()
        raise RuntimeError("OOM_NOT_OBSERVED: driver permitted allocation beyond physical VRAM; no exhaustion loop attempted")
except BaseException as exc:
    result.setdefault("error_type", type(exc).__name__)
    result.setdefault("error", str(exc))
    raise
finally:
    (artifact_dir / "oom-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
