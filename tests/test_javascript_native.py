"""Real pinned package managers against a disposable, local registry inventory."""

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import javascript_updates as js
import registry


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_PNPM") == "1",
    "explicit pinned JavaScript profile integration lane",
)
class NativeJavaScriptTests(unittest.TestCase):
    def setUp(self):
        for binary in ("npm", "pnpm"):
            self.assertIsNotNone(shutil.which(binary), binary)
        temporary = tempfile.TemporaryDirectory(prefix="native registry fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(
            os.environ,
            TOOLCHAIN_DOWNLOAD_CACHE=str(self.root / "downloads"),
            # pnpm 11 ignores cache-dir and request options in .npmrc. Native
            # fixtures must not share mutable metadata for their neutral names.
            PNPM_CONFIG_CACHE_DIR=str(self.root / "pnpm-cache"),
            PNPM_CONFIG_FETCH_RETRIES="0",
            PNPM_CONFIG_FETCH_TIMEOUT="5000",
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.metadata = {}
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = unquote(urlsplit(self.path).path.lstrip("/"))
                fixture.requests.append(name)
                body = fixture.metadata.get(name)
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body or {}).encode())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.write(
            ".npmrc",
            f"registry=http://127.0.0.1:{server.server_port}/\n"
            f"cache={self.root / 'npm-cache'}\n"
            "fetch-retries=0\nfetch-timeout=5000\naudit=false\nfund=false\n",
        )
        self.write("chainman.toml", 'schema=1\n[project]\ndefault_profile="host"\n')
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.policy = {"minimum_age_days": 30}
        self.add_release("neutral-parent", "1.0.0", {"neutral-leaf": "^1.0.0"})
        self.add_release("neutral-leaf", "1.0.0")
        self.add_release("neutral-leaf", "2.0.0")
        mocked = patch.object(registry, "data", side_effect=self.fetch)
        mocked.start()
        self.addCleanup(mocked.stop)

    def write(self, relative, value):
        (self.root / relative).write_text(value)

    def add_release(self, name, version, dependencies=None):
        body = self.metadata.setdefault(
            name, {"name": name, "versions": {}, "time": {}, "dist-tags": {}}
        )
        checksum = base64.b64encode(
            hashlib.sha512(f"{name}@{version}".encode()).digest()
        ).decode()
        body["versions"][version] = {
            "name": name,
            "version": version,
            "dependencies": dependencies or {},
            "dist": {
                "integrity": "sha512-" + checksum,
                # These commands resolve/validate locks without downloading
                # archives. An attempted archive download must fail visibly.
                "tarball": f"https://artifacts.example.invalid/{name}-{version}.tgz",
            },
        }
        body["time"][version] = "2026-01-01T00:00:00.000Z"
        body["dist-tags"]["latest"] = version

    def fetch(self, url):
        self.assertTrue(url.startswith("https://registry.npmjs.org/"), url)
        return self.metadata[unquote(urlsplit(url).path.lstrip("/"))]

    def configure(self, manager, **extra):
        self.spec = {
            "directory": ".",
            "profile": "host",
            "manager": manager,
            "copy_inputs": [".npmrc"],
        }
        self.write(
            "package.json",
            json.dumps(
                {
                    "name": "neutral-root",
                    "version": "1.0.0",
                    "private": True,
                    "dependencies": {"neutral-parent": "^1.0.0"},
                    **extra,
                }
            ),
        )
        if manager == "pnpm":
            self.write("pnpm-workspace.yaml", "packages: []\n")

    def native(self, *args):
        return subprocess.run(
            args,
            cwd=self.root,
            env=js.tc.environment(self.root),
            text=True,
            capture_output=True,
            timeout=30,
        )

    def frozen(self, manager):
        if manager == "npm":
            return self.native(
                "npm",
                "ci",
                "--dry-run",
                "--ignore-scripts",
                "--offline",
                "--no-audit",
                "--strict-peer-deps",
            )
        return self.native(
            "pnpm",
            "install",
            "--lockfile-only",
            "--ignore-scripts",
            "--strict-peer-dependencies=false",
            "--frozen-lockfile",
        )

    def test_native_cache_configuration_is_scoped_to_the_fixture(self):
        self.configure("pnpm")
        for manager, key, expected in (
            ("pnpm", "cacheDir", self.root / "pnpm-cache"),
            ("pnpm", "storeDir", self.root / "downloads/pnpm"),
            ("npm", "cache", self.root / "npm-cache"),
        ):
            with self.subTest(manager=manager, key=key):
                result = self.native(manager, "config", "get", key)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected))

    def test_real_resolvers_accept_a_registry_transitive_graph(self):
        for manager in ("npm", "pnpm"):
            with self.subTest(manager=manager):
                self.configure(manager)
                result = js.resolve(self.root, self.spec, self.policy, self.now)
                self.assertIn(
                    js.Workspace(self.root, self.spec).lock, result["changed_files"]
                )
                identities = js.snapshot(self.root, self.spec)["identities"]
                self.assertEqual(
                    {(i[1], i[2]) for i in identities},
                    {("neutral-parent", "1.0.0"), ("neutral-leaf", "1.0.0")},
                )
                self.assertIn("neutral-parent", self.requests)
                self.assertIn("neutral-leaf", self.requests)

    def test_frozen_checks_and_audit_reject_an_incompatible_transitive_version(self):
        for manager in ("npm", "pnpm"):
            with self.subTest(manager=manager):
                self.configure(manager)
                before = js.snapshot(self.root, self.spec)
                js.resolve(self.root, self.spec, self.policy, self.now)
                workspace = js.Workspace(self.root, self.spec)
                control = self.frozen(manager)
                self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
                path = self.root / workspace.lock
                lock = yaml.safe_load(path.read_text())
                if manager == "npm":
                    lock["packages"]["node_modules/neutral-leaf"].update(
                        version="2.0.0",
                        resolved=self.metadata["neutral-leaf"]["versions"]["2.0.0"][
                            "dist"
                        ]["tarball"],
                        integrity=self.metadata["neutral-leaf"]["versions"]["2.0.0"][
                            "dist"
                        ]["integrity"],
                    )
                    path.write_text(json.dumps(lock))
                else:
                    lock["packages"]["neutral-leaf@2.0.0"] = lock["packages"].pop(
                        "neutral-leaf@1.0.0"
                    )
                    lock["packages"]["neutral-leaf@2.0.0"]["resolution"] = (
                        self.metadata["neutral-leaf"]["versions"]["2.0.0"]["dist"]
                    )
                    lock["snapshots"]["neutral-leaf@2.0.0"] = lock["snapshots"].pop(
                        "neutral-leaf@1.0.0"
                    )
                    lock["snapshots"]["neutral-parent@1.0.0"]["dependencies"][
                        "neutral-leaf"
                    ] = "2.0.0"
                    path.write_text(yaml.safe_dump(lock))
                checked = self.frozen(manager)
                if checked.returncode:
                    continue
                with self.assertRaises(ValueError, msg=checked.stdout + checked.stderr):
                    js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_pnpm_audit_rejects_an_omitted_required_edge(self):
        self.configure("pnpm")
        before = js.snapshot(self.root, self.spec)
        js.resolve(self.root, self.spec, self.policy, self.now)
        path = self.root / "pnpm-lock.yaml"
        lock = yaml.safe_load(path.read_text())
        lock["snapshots"]["neutral-parent@1.0.0"].pop("dependencies")
        path.write_text(yaml.safe_dump(lock))
        with self.assertRaisesRegex(ValueError, "neutral-parent>neutral-leaf"):
            js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_real_pnpm_respects_declared_transitive_overrides(self):
        cases = (
            ("neutral-leaf", "2.0.0", {("neutral-leaf", "2.0.0")}),
            ("neutral-parent@1.0.0>neutral-leaf", "2.0.0", {("neutral-leaf", "2.0.0")}),
            ("neutral-leaf@^1.0.0", "2.0.0", {("neutral-leaf", "2.0.0")}),
            ("neutral-parent>neutral-leaf", "-", set()),
            (
                "neutral-leaf",
                "npm:neutral-replacement@1.0.0",
                {("neutral-replacement", "1.0.0")},
            ),
        )
        self.add_release("neutral-replacement", "1.0.0")
        for selector, replacement, expected in cases:
            with self.subTest(selector=selector, replacement=replacement):
                (self.root / "pnpm-lock.yaml").unlink(missing_ok=True)
                self.configure("pnpm")
                self.write(
                    "pnpm-workspace.yaml",
                    yaml.safe_dump(
                        {"packages": [], "overrides": {selector: replacement}}
                    ),
                )
                js.resolve(self.root, self.spec, self.policy, self.now)
                identities = js.snapshot(self.root, self.spec)["identities"]
                self.assertEqual(
                    {(i[1], i[2]) for i in identities},
                    {("neutral-parent", "1.0.0")} | expected,
                )

    def test_real_frozen_checks_reject_manifest_drift(self):
        for manager in ("npm", "pnpm"):
            with self.subTest(manager=manager):
                self.configure(manager)
                js.resolve(self.root, self.spec, self.policy, self.now)
                control = self.frozen(manager)
                self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
                path = self.root / "package.json"
                manifest = json.loads(path.read_text())
                manifest["dependencies"]["neutral-leaf"] = "2.0.0"
                path.write_text(json.dumps(manifest))
                checked = self.frozen(manager)
                self.assertNotEqual(
                    checked.returncode, 0, checked.stdout + checked.stderr
                )

    def test_real_pnpm_accepts_dependencies_bundled_in_the_parent_artifact(self):
        self.configure("pnpm")
        self.metadata["neutral-parent"]["versions"]["1.0.0"]["bundleDependencies"] = [
            "neutral-leaf"
        ]
        js.resolve(self.root, self.spec, self.policy, self.now)
        identities = js.snapshot(self.root, self.spec)["identities"]
        self.assertEqual(
            {(i[1], i[2]) for i in identities}, {("neutral-parent", "1.0.0")}
        )

    def test_real_pnpm_can_link_a_compatible_transitive_workspace_package(self):
        self.configure("pnpm")
        provider = self.root / "packages/provider"
        provider.mkdir(parents=True)
        (provider / "package.json").write_text(
            json.dumps({"name": "neutral-leaf", "version": "1.0.0"})
        )
        self.write(
            "pnpm-workspace.yaml",
            yaml.safe_dump(
                {
                    "packages": ["packages/*"],
                    "linkWorkspacePackages": "deep",
                    "preferWorkspacePackages": True,
                }
            ),
        )
        before = js.snapshot(self.root, self.spec)
        js.resolve(self.root, self.spec, self.policy, self.now)
        identities = js.snapshot(self.root, self.spec)["identities"]
        self.assertEqual(
            {(i[1], i[2]) for i in identities}, {("neutral-parent", "1.0.0")}
        )
        (provider / "package.json").write_text(
            json.dumps({"name": "neutral-leaf", "version": "2.0.0"})
        )
        with self.assertRaisesRegex(ValueError, "neutral-parent>neutral-leaf"):
            js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_real_pnpm_parent_override_takes_precedence_in_either_order(self):
        rules = [("neutral-parent>neutral-leaf", "2.0.0"), ("neutral-leaf", "1.0.0")]
        for entries in (rules, list(reversed(rules))):
            with self.subTest(entries=entries):
                (self.root / "pnpm-lock.yaml").unlink(missing_ok=True)
                self.configure("pnpm")
                self.spec["mode"] = "compatible"
                self.write(
                    "pnpm-workspace.yaml",
                    yaml.safe_dump(
                        {"packages": [], "overrides": dict(entries)}, sort_keys=False
                    ),
                )
                js.resolve(self.root, self.spec, self.policy, self.now)
                identities = js.snapshot(self.root, self.spec)["identities"]
                self.assertEqual(
                    {(i[1], i[2]) for i in identities},
                    {("neutral-parent", "1.0.0"), ("neutral-leaf", "2.0.0")},
                )

    def test_real_pnpm_can_replace_or_remove_an_upstream_remote_declaration(self):
        self.metadata["neutral-parent"]["versions"]["1.0.0"]["dependencies"][
            "neutral-leaf"
        ] = "https://example.invalid/legacy.tgz"
        for replacement, expected in (
            ("2.0.0", {("neutral-leaf", "2.0.0")}),
            ("-", set()),
        ):
            with self.subTest(replacement=replacement):
                (self.root / "pnpm-lock.yaml").unlink(missing_ok=True)
                self.configure("pnpm")
                self.write(
                    "pnpm-workspace.yaml",
                    yaml.safe_dump(
                        {
                            "packages": [],
                            "overrides": {"neutral-parent>neutral-leaf": replacement},
                        }
                    ),
                )
                js.resolve(self.root, self.spec, self.policy, self.now)
                identities = js.snapshot(self.root, self.spec)["identities"]
                self.assertEqual(
                    {(i[1], i[2]) for i in identities},
                    {("neutral-parent", "1.0.0")} | expected,
                )


if __name__ == "__main__":
    unittest.main()
