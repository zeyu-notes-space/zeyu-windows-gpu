# Contributing

Open an issue before a large behavioral change. Keep the project focused on a single-user Mac-to-Windows GPU backend.

Before a pull request:

```bash
python3 scripts/privacy_scan.py
python3 -m pytest tests compute-fabric/tests -q
```

Do not submit logs, audio, checkpoints, credentials, private addresses, personal paths, or generated environments. Tests must distinguish fixture behavior from real GPU evidence.
