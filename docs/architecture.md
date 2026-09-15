# Architecture

The Mac is the control plane and remains the only interactive development environment. Windows is a private compute worker with an NVIDIA GPU.

The Codex plugin is a thin local MCP server. It validates explicit selection before loading connection configuration. Status probes check the private SSH path, the BentoML health endpoint, and Compute Fabric within a bounded deadline.

Interactive WAV inference goes to BentoML. The service loads one approved adapter, keeps it resident, serializes inference, reports structured failures, and writes a metadata-bearing result. The Mac client publishes the returned WAV only after run identity, model identity, byte length, and SHA-256 checks agree.

Long-running work goes to Compute Fabric. A submitted specification names a registered project, exact Git commit, argv command, arguments, registered environment, timeout, artifact paths, and optional resources. The worker exports the commit into an isolated workspace, supervises the process tree, persists lifecycle transitions, records metrics, and returns a hash-verified archive.

BentoML and Compute Fabric coordinate through one file-backed GPU lease. A batch request may ask an idle model service to release its model before acquiring the lease. Unknown transport outcomes are not automatically repeated; batch recovery must reuse the original idempotency key.
