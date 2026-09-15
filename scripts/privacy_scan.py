#!/usr/bin/env python3
"""Fail closed when a public release contains private machine data."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath


BLOCKED_SUFFIXES = {
    ".ckpt", ".db", ".key", ".log", ".msi", ".pem", ".pfx", ".pt",
    ".pth", ".safetensors", ".sqlite", ".sqlite3", ".wav", ".zip",
}
BLOCKED_NAMES = {"authorized_keys", "known_hosts", "worker.token", "credentials.json"}
CONTENT_RULES = {
    "macOS home path": re.compile(rb"/Users/[A-Za-z0-9._-]+"),
    "Windows user path": re.compile(rb"C:\\Users\\[A-Za-z0-9._-]+", re.I),
    "RFC1918 10/8 address": re.compile(rb"(?<![0-9])10(?:\.[0-9]{1,3}){3}(?![0-9])"),
    "RFC1918 172.16/12 address": re.compile(rb"(?<![0-9])172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}(?![0-9])"),
    "RFC1918 192.168/16 address": re.compile(rb"(?<![0-9])192\.168(?:\.[0-9]{1,3}){2}(?![0-9])"),
    "Tailscale CGNAT address": re.compile(rb"(?<![0-9])100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.[0-9]{1,3}){2}(?![0-9])"),
    "tailnet DNS name": re.compile(rb"[A-Za-z0-9-]+\.ts\.net", re.I),
    "private key": re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"gh[opusr]_[A-Za-z0-9]{20,}"),
    "Tailscale auth key": re.compile(rb"tskey-[A-Za-z0-9_-]{16,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
}
UUID = re.compile(rb"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
SCAN_EXEMPT = {PurePosixPath("scripts/privacy_scan.py"), PurePosixPath("tests/test_privacy.py")}
MAX_BYTES = 1_048_576


def _scan_blob(path: PurePosixPath, data: bytes) -> list[str]:
    findings: list[str] = []
    lowered = path.name.lower()
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        findings.append("blocked file type")
    if lowered in BLOCKED_NAMES or "credential" in lowered or lowered.endswith(".token"):
        findings.append("blocked sensitive filename")
    if len(data) > MAX_BYTES:
        findings.append(f"file exceeds {MAX_BYTES} bytes")
    if path not in SCAN_EXEMPT:
        for name, pattern in CONTENT_RULES.items():
            if pattern.search(data):
                findings.append(name)
        if path.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".txt"} and UUID.search(data):
            findings.append("literal run/operation UUID")
    return findings


def scan_tree(root: Path) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if path.is_symlink():
            findings.append(f"{relative}: symlink is not publishable")
        elif path.is_file():
            for reason in _scan_blob(relative, path.read_bytes()):
                findings.append(f"{relative}: {reason}")
    return findings


def scan_history(root: Path) -> list[str]:
    findings: list[str] = []
    commits = subprocess.check_output(["git", "rev-list", "--all"], cwd=root, text=True).splitlines()
    for commit in commits:
        names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", commit], cwd=root, text=True).splitlines()
        for name in names:
            path = PurePosixPath(name)
            data = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=root)
            for reason in _scan_blob(path, data):
                findings.append(f"{commit[:12]}:{path}: {reason}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    findings = scan_tree(root)
    if args.history:
        findings.extend(scan_history(root))
    if findings:
        print("PRIVACY_SCAN=FAIL")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print(f"PRIVACY_SCAN=PASS; history={'yes' if args.history else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
