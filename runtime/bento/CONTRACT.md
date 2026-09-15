# ZeYu Native Bento handoff

This directory is an isolated handoff for the final integration. It does not
replace or edit `outputs/ZeYuRuntimeFrameworkPOC`. `service.py` is a thin
BentoML 1.4.39 service that imports the existing audited adapters and the
existing `ZeYuComputeFabric.zeyu_fabric.gpu_lease.GpuLease` implementation.
It contains no shell, reboot, security, batch-job, or custom serving-engine
endpoint.

## Runtime environment

The Windows installer should set these values for the protected Bento process.
Paths are intentionally explicit so a missing model/lease path fails closed.

| Variable | Required | Meaning |
| --- | --- | --- |
| `ZEYU_BENTO_TOKEN_FILE` | yes | Protected file containing the bearer token, read at Bento service/ASGI construction. |
| `ZEYU_BENTO_TOKEN` | local fallback | Inline token fallback for isolated tests only. |
| `ZEYU_GPU_LEASE_PATH` | yes | Shared lease file also used by Compute Fabric. Its parent directory must already exist. |
| `ZEYU_FABRIC_ROOT` | yes | Directory containing `zeyu_fabric/gpu_lease.py` (for example `C:\ProgramData\ZeYuComputeFabric`). |
| `ZEYU_RUNTIME_COMMON` | yes | Directory containing the audited adapter modules. |
| `ZEYU_UNET_ADAPTER` | no | Defaults to `speech_denoise_adapter:create_adapter`. |
| `ZEYU_TIGER_ADAPTER` | no | Defaults to `tiger_adapter:create_adapter`. |
| `ZEYU_UNET_PROJECT_ROOT` | recommended | Alias-specific value applied to `ZEYU_REAL_PROJECT_ROOT` while the UNet factory loads. |
| `ZEYU_TIGER_PROJECT_ROOT` | recommended | Alias-specific value applied to `ZEYU_REAL_PROJECT_ROOT` while the TIGER factory loads. |
| `ZEYU_UNET_CONFIG`, `ZEYU_UNET_CHECKPOINT`, `ZEYU_UNET_MODE`, `ZEYU_UNET_GIT_COMMIT`, `ZEYU_UNET_GIT_DIRTY` | adapter-dependent | Alias-specific overrides for the audited UNet adapter. |
| `ZEYU_TIGER_CONFIG`, `ZEYU_TIGER_CHECKPOINT`, `ZEYU_TIGER_MODE`, `ZEYU_TIGER_GIT_COMMIT`, `ZEYU_TIGER_GIT_DIRTY` | adapter-dependent | Alias-specific overrides for any TIGER adapter implementation that uses them. |
| `ZEYU_BENTO_ARTIFACT_ROOT` | yes | Writable directory for returned WAV artifacts. |
| `ZEYU_CUDA_DEVICE` | no | Defaults to `cuda:0`. |
| `ZEYU_BENTO_TIMEOUT_SECONDS` | no | Honest response timeout; defaults to `300`. A timeout response never cancels adapter work. |

The alias-specific variables are applied only during factory construction and
restored immediately afterward. This lets UNet and TIGER use different audited
project roots without concurrent process-environment races; model loading is
serialized by the in-process gate and the shared cross-process lease.

## HTTP contract

All custom routes require `Authorization: Bearer <token>`. Bento's own
`/healthz`, `/livez`, `/readyz`, and `/metrics` infrastructure routes remain
available to the supervisor.

`POST /gpu_health` takes `{}` and returns CPU/process state. It does not import
torch, probe CUDA, acquire the lease, or load a model. `cuda_probe` is
`deferred` until a model is resident.

`POST /infer_audio` is multipart form data:

```text
audio: uploaded WAV file (required)
model: unet | tiger (required)
run_id: UUID (required by the service contract)
```

The successful response is the WAV `Path` body with an
`X-ZeYu-Operation` JSON header. The header includes `run_id`, model/checkpoint
identity, git commit, hardware/runtime metadata, input parameters, artifact
SHA-256, and measured model-load/adapter/total timings. The artifact path is
inside `ZEYU_BENTO_ARTIFACT_ROOT` and is written atomically.

Only one alias can be resident. A second request for the same alias reuses the
resident model. A different alias returns `MODEL_BUSY` until
`POST /release_model` completes. The service holds `ZEYU_GPU_LEASE_PATH` for
the entire residency period, including timed-out work. `release_model` closes
the adapter, drops references and CUDA cache, then releases the shared lease;
if unload fails, the lease stays protected and `MODEL_UNLOAD_FAILED` is
returned.

If a factory raises before returning an adapter object, the service cannot prove
that CUDA references were released. In that case `release_model` deliberately
returns `MODEL_UNLOAD_FAILED` and keeps the lease held for supervisor-verified
process recovery.

Errors are JSON objects with stable `error_code`, `run_id`, model/checkpoint/git
fields, hardware/runtime/parameters, artifact hash, timing, and a `details`
object. Common codes are `AUTH_REQUIRED`, `INVALID_RUN_ID`, `INVALID_MODEL`,
`GPU_BUSY`, `GPU_LEASE_STALE`, `MODEL_BUSY`, `MODEL_LOAD_FAILED`,
`INFERENCE_FAILED`, `MODEL_UNLOAD_FAILED`, and `SERVICE_TIMEOUT`.

`SERVICE_TIMEOUT` is a truthful 504 response. The adapter task continues in
the Bento worker, late response bytes are discarded, and the lease/gate stay
held until the work exits. Process-tree termination remains a supervisor
responsibility after it verifies the job tree.

## Isolated validation

Use the existing local BentoML 1.4.39 environment. The tests inject a tiny
contract adapter and a temporary lease file; they do not claim CUDA, a Windows
GPU, or real-model compatibility:

```bash
work/real-gpu-bakeoff/mac-client-venv/bin/python \
  -m unittest discover \
  -s work/final-integration/bento-handoff/tests -v
```

The schema/runtime wiring check uses the same real Bento package:

```bash
ZEYU_BENTO_TOKEN=contract-test \
ZEYU_BENTO_ARTIFACT_ROOT=/tmp/zeyu-bento-schema-check \
work/real-gpu-bakeoff/mac-client-venv/bin/python -c \
  'import sys; sys.path.insert(0, "work/final-integration/bento-handoff"); import service; service.svc.inject_config(); print(service.svc.schema()); service.svc.to_asgi()'
```

## Handoff copy commands

From the repository root, stage only this service plus the audited runtime
inputs for the Windows installer:

```bash
handoff="$PWD/work/final-integration/bento-handoff"
stage="$PWD/work/final-integration/bento-stage"
mkdir -p "$stage/bento" "$stage/common" "$stage/zeyu_fabric"
cp "$handoff/service.py" "$stage/bento/service.py"
cp "$PWD/outputs/ZeYuRuntimeFrameworkPOC/common/speech_denoise_adapter.py" "$stage/common/"
cp "$PWD/outputs/ZeYuRuntimeFrameworkPOC/common/tiger_adapter.py" "$stage/common/"
cp "$PWD/outputs/ZeYuRuntimeFrameworkPOC/common/REAL_ADAPTER_CONTRACT.md" "$stage/common/"
cp "$PWD/outputs/ZeYuComputeFabric/zeyu_fabric/gpu_lease.py" "$stage/zeyu_fabric/"
```

The Windows installer should copy `$stage/bento/service.py` to
`C:\ProgramData\ZeYuWindowsGPU\bento\service.py`, `$stage/common` to
`C:\ProgramData\ZeYuWindowsGPU\common`, and set `ZEYU_RUNTIME_COMMON` to that
`common` directory. Set `ZEYU_FABRIC_ROOT` to the existing Fabric root (or
copy the staged `zeyu_fabric` package under that root). Do not copy the
contract test adapter into the protected runtime.
