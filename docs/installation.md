# Installation

This repository provides reusable components rather than a machine-specific one-click installer. Model projects, checkpoints, CUDA wheels, account credentials, SSH keys, and private-network enrollment stay outside the repository.

## Windows prerequisites

1. Install a supported NVIDIA driver and a Python environment whose PyTorch build reports CUDA available.
2. Enable OpenSSH Server and restrict access to the operator's private-network address. Keep both application services on `127.0.0.1`.
3. Install `compute-fabric` into a protected Python environment and use `compute-fabric/scripts/Install-Worker.ps1` from an administrator shell to create the limited worker and boot task.
4. Install BentoML dependencies from `runtime/bento/requirements.txt` into the CUDA environment.
5. Place reviewed model code and checkpoints in administrator-protected directories. Configure adapter factories through `ZEYU_UNET_ADAPTER` and `ZEYU_TIGER_ADAPTER`.
6. Configure the BentoML process with a protected token file, artifact directory, Compute Fabric package root, and shared GPU lease. Run it through `runtime/windows/supervisor.py` under the same limited worker identity.

The internal deployment-specific installer is intentionally excluded: it contained exact source locations and checkpoint identities that are neither portable nor appropriate for a public repository.

## Mac client

Create a private SSH configuration with strict host-key checking, public-key authentication, no agent forwarding, and local forwards to the two Windows loopback ports. Copy `config/client.example.json` into a private configuration directory, replace every angle-bracket placeholder, and keep the configuration, SSH identity, known-hosts file, and token readable only by your account.

Install client dependencies in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-client.txt -e compute-fabric
```

Register `plugin/` in a personal Codex marketplace using the supported Codex plugin workflow. Start a new Codex task, select **Windows GPU**, and ask for status before submitting work.

## Model adapter contract

Each adapter factory must return a reviewed implementation compatible with the interface consumed by `runtime/bento/service.py`. Validate a new adapter with a real WAV and real GPU before treating it as production-ready. Fixture tests only validate service behavior.
