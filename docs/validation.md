# Validation evidence

The private deployment used a Native Windows PyTorch CUDA runtime on an NVIDIA GeForce RTX 5070 Ti Laptop GPU. The same real WAV and real model checkpoints were used to validate interactive inference and framework comparisons.

Measured final-integration results retained as aggregate evidence:

| Check | Result |
|---|---|
| Interactive UNet and TIGER requests | Completed on the real GPU; WAV returned to the Mac and hashes verified |
| Compute Fabric CUDA job | Completed with lifecycle, logs, metrics, and verified artifact bundle |
| Bad WAV / Python exception / timeout / cancellation | Returned diagnostic terminal outcomes without taking down the backend |
| BentoML process termination | Supervisor recovered; a subsequent real inference completed |
| Active Compute Fabric worker termination | Running job became `WORKER_INTERRUPTED`; worker recovered; a subsequent CUDA job completed |
| Transport interruption | Unknown outcome reported without automatic duplicate execution; later inference succeeded |
| Soak | 1,201.342 seconds, 588 requests, 588 successes, zero request failures |

The soak used a previously warmed UNet and a 1.741-second WAV. Its Mac end-to-end mean was 1.195 seconds, p95 1.944 seconds, and p99 2.937 seconds. Service-side total mean was 29.054 ms; CUDA event mean was 10.642 ms. These figures describe that machine and workload, not a general benchmark.

The retained test was shorter than the desired 2–4-hour soak. Windows reboot recovery, different ordinary networks, and physical offline/recovery were not completed in the final retained acceptance report. They are not reported as passing here.

Raw logs, WAVs, checkpoint hashes, local paths, addresses, usernames, machine identifiers, and run IDs are deliberately omitted.
