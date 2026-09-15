#!/usr/bin/env python3
"""Import the private token copied securely from Windows and configure zrun."""
import argparse
import json
import os
from pathlib import Path
import re


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure Mac access after Windows setup")
    parser.add_argument("--ssh-host", required=True, help="Existing verified SSH alias, for example zeyu-win")
    parser.add_argument("--token-file", required=True, type=Path, help="Token transferred privately from Windows")
    parser.add_argument("--directory", type=Path, default=Path.home() / ".config/zeyu-fabric")
    parser.add_argument("--local-port", type=int, default=8765)
    parser.add_argument("--remote-port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", args.ssh_host):
        parser.error("SSH host must be a configured alias; set User in your SSH config")
    if not all(1 <= port <= 65535 for port in (args.local_port, args.remote_port)):
        parser.error("ports must be 1..65535")
    token = args.token_file.expanduser().read_text(encoding="utf-8-sig").strip()
    if len(token) < 32 or any(c.isspace() for c in token):
        parser.error("token file is invalid")
    directory = args.directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    config_file = directory / "client.json"
    token_file = directory / "worker.token"
    if config_file.exists() or (token_file.exists() and token_file.resolve() != args.token_file.expanduser().resolve()):
        parser.error("configuration already exists; choose another --directory or edit the existing configuration")
    if token_file.is_symlink():
        parser.error("token file cannot be a symlink")
    if not token_file.exists():
        descriptor = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(token + "\n")
    token_file.chmod(0o600)
    config = {"url": "http://127.0.0.1:" + str(args.local_port), "token_file": str(token_file),
              "ssh_host": args.ssh_host, "local_port": args.local_port, "remote_port": args.remote_port}
    descriptor = os.open(str(config_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"configured": str(config_file), "next": "./zrun connect"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
