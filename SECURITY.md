# Security policy

## Supported scope

Security fixes target the current `main` branch and latest release.

## Deployment boundary

- Keep BentoML and Compute Fabric bound to loopback.
- Reach Windows through a private network and strict SSH local forwarding.
- Pin SSH host keys, use a dedicated key, disable agent forwarding, and protect token files.
- Run workloads under a dedicated non-administrator account.
- Keep program code, model code, checkpoints, configuration, and credentials administrator-protected; grant the worker write access only to explicit state, log, temporary, and artifact directories.
- Never expose the command API directly to the public internet.

The worker executes commands chosen by its trusted operator. Do not deploy it as a shared service for untrusted users.

## Reporting a vulnerability

Use GitHub's private security-advisory interface for this repository. Do not include real credentials, private addresses, model data, or user audio in an issue.
