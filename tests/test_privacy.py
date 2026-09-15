import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("privacy_scan", ROOT / "scripts" / "privacy_scan.py")
privacy_scan = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(privacy_scan)


class PrivacyScanTests(unittest.TestCase):
    def scan(self, name: str, content: bytes = b"safe"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            return privacy_scan.scan_tree(root)

    def test_rejects_private_paths_and_addresses(self):
        fixtures = [
            ("note.md", b"/Users/example-person/project"),
            ("note.md", b"C:\\Users\\example-person\\project"),
            ("note.md", b"host 10.23.45.67"),
            ("note.md", b"host 172.20.30.40"),
            ("note.md", b"host 192.168.20.30"),
            ("note.md", b"host 100.90.20.30"),
            ("note.md", b"device.example-tailnet.ts.net"),
        ]
        for name, content in fixtures:
            with self.subTest(content=content):
                self.assertTrue(self.scan(name, content))

    def test_rejects_secrets_and_private_artifacts(self):
        self.assertTrue(self.scan("id.key", b"secret"))
        self.assertTrue(self.scan("audio.wav", b"RIFF"))
        self.assertTrue(self.scan("worker.token", b"secret"))
        self.assertTrue(self.scan("note.md", b"-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertTrue(self.scan("note.md", b"gho_abcdefghijklmnopqrstuvwxyz"))

    def test_accepts_parameterized_public_configuration(self):
        content = b'{"ssh_host":"<windows-host>","token_file":"<private-token-file>"}'
        self.assertEqual([], self.scan("config/client.example.json", content))


if __name__ == "__main__":
    unittest.main()
