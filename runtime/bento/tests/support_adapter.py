from __future__ import annotations

import hashlib


class FakeAdapter:
    close_count = 0
    infer_count = 0

    def __init__(self, device: str):
        self.device = device

    def metadata(self) -> dict:
        return {
            "real_audio_model": True,
            "model": "contract-test-model",
            "project": "contract-test-project",
            "git_commit": "a" * 40,
            "git_dirty": False,
            "checkpoint": "contract-test-checkpoint.pt",
            "checkpoint_sha256": "b" * 64,
            "adapter_model_load_ms": 1.5,
        }

    def infer_wav(self, data: bytes):
        type(self).infer_count += 1
        output = b"contract-output:" + data
        return output, {
            "adapter_compute_wall_ms": 2.5,
            "adapter_cuda_event_ms": 2.0,
        }

    def close(self):
        type(self).close_count += 1


def create_adapter(device: str):
    return FakeAdapter(device)


def create_factory_failure(device: str):
    raise RuntimeError("factory failed after touching the model runtime")


def reset_counts():
    FakeAdapter.close_count = 0
    FakeAdapter.infer_count = 0
