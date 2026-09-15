# Known limitations

- One Windows worker and one GPU; no multi-user or multi-GPU scheduler.
- Trusted single-user commands are allowed by Compute Fabric. It is not a hostile-code sandbox.
- One active model and controlled concurrency are optimized for reliability, not maximum throughput.
- A client timeout or broken connection may leave inference completion unknown. It does not prove the GPU operation was cancelled.
- Model implementations and checkpoints are operator-supplied and must be reviewed separately.
- TIGER support represents two-source separation and does not imply verified causal denoising quality.
- Stateful streaming, microphone processing, Wake-on-LAN, dashboard, Kubernetes, and cloud workers are outside this release.
- The public repository cannot reproduce the private benchmark without the same model projects, checkpoints, hardware, and WAV.
