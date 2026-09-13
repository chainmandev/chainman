"""Qualify uv's real lock/check behavior against a disposable package index."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import tomllib
import unittest
from urllib.parse import urlsplit
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import toolchain as tc
import updates


@unittest.skipUnless(os.environ.get("CHAINMAN_TEST_UV") == "1", "run just python-test")
class NativePythonTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("uv"), "python-test requires pinned uv")
        temporary = tempfile.TemporaryDirectory(prefix="chainman uv fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.files, self.packages = {}, {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.respond(head=True)

            def do_GET(self):
                self.respond(head=False)

            def respond(self, *, head):
                name = urlsplit(self.path).path
                if name.startswith("/simple/"):
                    package = name.removeprefix("/simple/").strip("/")
                    entries = fixture.packages.get(package)
                    body = json.dumps(
                        {
                            "meta": {"api-version": "1.4"},
                            "name": package,
                            "files": entries or [],
                        }
                    ).encode()
                    content = "application/vnd.pypi.simple.v1+json"
                    status = 200 if entries else 404
                else:
                    body = fixture.files.get(name, b"")
                    content, status = "application/octet-stream", 200 if body else 404
                self.send_response(status)
                self.send_header("Content-Type", content)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.index = f"http://127.0.0.1:{server.server_port}/simple/"
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("UV_", "PIP_", "PYTHON"))
        }
        self.env.update(
            UV_DEFAULT_INDEX=self.index,
            UV_CACHE_DIR=str(self.root / "cache"),
            UV_PYTHON=sys.executable,
            UV_PYTHON_DOWNLOADS="never",
            XDG_CONFIG_HOME=str(self.root / "config"),
            UV_HTTP_TIMEOUT="5",
            UV_HTTP_RETRIES="0",
        )
        self.manifest = self.root / "pyproject.toml"
        self.lock = self.root / "uv.lock"
        self.manifest.write_text(
            '# preserved project comment\n[project]\nname="neutral-app"\nversion="1.0.0"\n'
            'requires-python=">=3.11"\ndependencies=["neutral-parent>=1,<3"]\n'
        )
        # Runtime transaction anchors have subsecond precision. Integer-second
        # fixtures alone would miss timestamp truncation in uv/no-op retention.
        self.now = datetime(2025, 3, 1, 12, 34, 56, 123456, tzinfo=timezone.utc)
        self.spec = {"directory": "."}
        self.release(
            "neutral-parent", "1.0.0", "2025-01-01T00:00:00Z", ["neutral-leaf>=1,<2"]
        )
        self.release(
            "neutral-parent", "2.0.0", "2025-02-28T00:00:00Z", ["neutral-leaf>=1,<2"]
        )
        self.release("neutral-leaf", "1.0.0", "2025-01-01T00:00:00Z")
        self.release("neutral-leaf", "2.0.0", "2025-01-01T00:00:00Z")

    def release(self, name, version, published, requirements=()):
        normalized = name.replace("-", "_")
        filename = f"{normalized}-{version}-py3-none-any.whl"
        dist = f"{normalized}-{version}.dist-info"
        entries = {
            f"{dist}/METADATA": (
                f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\nRequires-Python: >=3.11\n"
                + "".join(
                    f"Requires-Dist: {requirement}\n" for requirement in requirements
                )
                + "\n"
            ).encode(),
            f"{dist}/WHEEL": b"Wheel-Version: 1.0\nGenerator: neutral-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{normalized}/__init__.py": b"",
        }
        entries[f"{dist}/RECORD"] = (
            "".join(
                f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip('=')},{len(body)}\n"
                for path, body in entries.items()
            )
            + f"{dist}/RECORD,,\n"
        ).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path, body in entries.items():
                archive.writestr(path, body)
        body = buffer.getvalue()
        path = "/files/" + filename
        self.files[path] = body
        self.packages.setdefault(name, []).append(
            {
                "filename": filename,
                "url": self.index.removesuffix("simple/") + path.lstrip("/"),
                "hashes": {"sha256": hashlib.sha256(body).hexdigest()},
                "size": len(body),
                "upload-time": published,
                "requires-python": ">=3.11",
            }
        )

    def uv(self, *arguments, succeeds=True):
        result = tc.managed_run(
            ["uv", "lock", *arguments],
            cwd=self.root,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def configure(self, at, *, package=False):
        options = updates.uv_resolution_options({}, at)
        if package:
            options += ["--exclude-newer-package", f"neutral-parent={at.isoformat()}"]
        return updates.configure_uv(self.root, self.spec, options)

    def versions(self):
        return {
            item["name"]: item["version"]
            for item in tomllib.loads(self.lock.read_text())["package"]
        }

    def test_real_cutoffs_and_transitive_constraints(self):
        self.configure(self.now)
        self.uv("--upgrade")
        self.assertEqual(
            self.versions(),
            {
                "neutral-app": "1.0.0",
                "neutral-parent": "1.0.0",
                "neutral-leaf": "1.0.0",
            },
        )
        self.uv("--check", "--offline")
        self.configure(self.now, package=True)
        self.uv("--upgrade")
        self.assertEqual(self.versions()["neutral-parent"], "2.0.0")
        self.assertEqual(self.versions()["neutral-leaf"], "1.0.0")
        self.uv("--check", "--offline")

    def test_native_cutoff_is_conservative_at_millisecond_boundary(self):
        cutoff = self.now - timedelta(days=30)
        for version, milliseconds in (("1.1.0", -1), ("1.2.0", 0), ("1.3.0", 1)):
            self.release(
                "neutral-parent",
                version,
                (cutoff + timedelta(milliseconds=milliseconds)).isoformat(),
                ["neutral-leaf<2"],
            )
        self.configure(self.now)
        self.uv("--upgrade")
        self.assertEqual(self.versions()["neutral-parent"], "1.1.0")
        self.uv("--check", "--offline")
        # uv's strict millisecond comparison can defer an eligible upload by
        # a millisecond. Do not relax the cutoff and admit younger artifacts.
        self.configure(self.now + timedelta(milliseconds=1))
        self.uv("--upgrade")
        self.assertEqual(self.versions()["neutral-parent"], "1.2.0")
        self.uv("--check", "--offline")

    def test_noop_restores_exact_previously_checkable_manifest_and_lock(self):
        for package in (False, True):
            with self.subTest(package_cutoff=package):
                self.configure(self.now, package=package)
                self.uv("--upgrade")
                self.uv("--check", "--offline")
                old_manifest, old_lock = (
                    self.manifest.read_text(),
                    self.lock.read_text(),
                )
                configured = self.configure(
                    self.now + timedelta(days=1), package=package
                )
                self.uv("--upgrade")
                self.assertNotEqual(self.lock.read_text(), old_lock)
                updates.retain_uv_noop(
                    self.root, self.spec, old_manifest, old_lock, configured
                )
                self.assertEqual(self.manifest.read_text(), old_manifest)
                self.assertEqual(self.lock.read_text(), old_lock)
                self.uv("--check", "--offline")

    def test_retiring_package_cutoff_keeps_new_policy_even_if_versions_do_not_change(
        self,
    ):
        self.configure(self.now, package=True)
        self.uv("--upgrade")
        before = self.versions()
        old_manifest, old_lock = self.manifest.read_text(), self.lock.read_text()
        configured = self.configure(self.now + timedelta(days=60))
        self.uv("--upgrade")
        self.assertEqual(self.versions(), before)
        updates.retain_uv_noop(self.root, self.spec, old_manifest, old_lock, configured)
        self.assertEqual(self.manifest.read_text(), configured)
        self.assertNotEqual(self.lock.read_text(), old_lock)
        self.assertNotIn(
            "exclude-newer-package",
            tomllib.loads(self.manifest.read_text())["tool"]["uv"],
        )
        self.uv("--check", "--offline")

    def test_frozen_check_rejects_manifest_drift_without_rewriting_lock(self):
        self.configure(self.now)
        self.uv("--upgrade")
        self.uv("--check", "--offline")
        old_lock = self.lock.read_bytes()
        self.manifest.write_text(
            self.manifest.read_text().replace(
                "neutral-parent>=1,<3", "neutral-parent>=2,<3"
            )
        )
        self.uv("--check", "--offline", succeeds=False)
        self.assertEqual(self.lock.read_bytes(), old_lock)


if __name__ == "__main__":
    unittest.main()
