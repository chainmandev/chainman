"""Audit the project graph without resolving another package-manager graph."""

import base64
from contextlib import redirect_stdout
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_audit as audit
import pnpm_setup


class PnpmAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pnpm audit ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifest = self.root / "package.json"
        self.manifest.write_text("{}")
        self.spec = {"adapter": "javascript", "profile": "selected-js"}

    def command(self, actual, directory=None):
        with (
            patch.object(audit.tc, "environment", return_value={}),
            patch.object(
                audit.chainman,
                "execute",
                return_value=subprocess.CompletedProcess([], 0, actual + "\n", ""),
            ) as probe,
        ):
            command = audit.javascript_audit_command(
                self.root, self.spec, self.root if directory is None else directory
            )
        self.assertEqual(
            probe.call_args.args[:3],
            (
                self.root,
                "selected-js",
                ["pnpm", "--version"],
            ),
        )
        self.assertEqual(probe.call_args.kwargs["cwd"], Path("/"))
        self.assertTrue(probe.call_args.kwargs["check"])
        return command

    def test_matching_exact_pin_preserves_cli_identity_check_and_disables_manager_sync(
        self,
    ):
        self.manifest.write_text('{"packageManager":"pnpm@12.14.0+sha512.YWJjZA=="}')
        self.assertEqual(
            self.command("12.14.0"),
            [
                "pnpm",
                "--pm-on-fail=ignore",
                "audit",
                "--json",
            ],
        )

    def test_absent_optional_pin_still_observes_the_selected_binary(self):
        self.assertEqual(
            self.command("12.14.0"),
            [
                "pnpm",
                "--pm-on-fail=ignore",
                "audit",
                "--json",
            ],
        )

    def test_missing_manifest_still_checks_the_selected_sdk(self):
        self.manifest.unlink()
        self.assertEqual(
            self.command("12.14.0"),
            ["pnpm", "--pm-on-fail=ignore", "audit", "--json"],
        )
        with self.assertRaisesRegex(ValueError, "exact pnpm"):
            self.command("garbage")

    def test_lock_only_directory_does_not_inherit_an_ancestor_manifest_pin(self):
        self.manifest.write_text('{"packageManager":"pnpm@0.0.0"}')
        directory = self.root / "lock-only"
        directory.mkdir()
        self.assertEqual(
            self.command("12.14.0", directory),
            ["pnpm", "--pm-on-fail=ignore", "audit", "--json"],
        )

    def test_existing_malformed_nonregular_and_symlink_manifests_are_rejected(self):
        for body in ["{", "[]"]:
            self.manifest.write_text(body)
            with patch.object(audit.chainman, "execute") as probe:
                with self.assertRaises(ValueError):
                    audit.javascript_audit_command(self.root, self.spec, self.root)
                probe.assert_not_called()
        self.manifest.unlink()
        self.manifest.mkdir()
        with patch.object(audit.chainman, "execute") as probe:
            with self.assertRaisesRegex(ValueError, "regular file"):
                audit.javascript_audit_command(self.root, self.spec, self.root)
            probe.assert_not_called()
        self.manifest.rmdir()
        (self.root / "other.json").write_text("{}")
        for target in ["other.json", "missing.json"]:
            self.manifest.symlink_to(target)
            with patch.object(audit.chainman, "execute") as probe:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    audit.javascript_audit_command(self.root, self.spec, self.root)
                probe.assert_not_called()
            self.manifest.unlink()

    def test_older_supported_pnpm_retains_its_existing_guard_options(self):
        self.assertEqual(
            self.command("10.32.0"),
            [
                "pnpm",
                "--config.manage-package-manager-versions=false",
                "--config.package-manager-strict-version=true",
                "audit",
                "--json",
            ],
        )

    def test_mismatched_or_invalid_actual_version_is_rejected_before_audit(self):
        self.manifest.write_text('{"packageManager":"pnpm@12.14.0"}')
        for actual, message in [
            ("11.25.0", "pnpm mismatch"),
            ("garbage", "exact pnpm"),
        ]:
            with (
                self.subTest(actual=actual),
                self.assertRaisesRegex(ValueError, message),
            ):
                self.command(actual)

    def test_invalid_declared_pin_fails_before_any_package_manager_invocation(self):
        for declared in ["pnpm@^12.14.0", "npm@12.2.0"]:
            self.manifest.write_text(json.dumps({"packageManager": declared}))
            with patch.object(audit.chainman, "execute") as probe:
                with self.assertRaisesRegex(ValueError, "exact pnpm"):
                    audit.javascript_audit_command(self.root, self.spec, self.root)
                probe.assert_not_called()

    def test_npm_behavior_does_not_probe_pnpm_or_read_its_package_manager_pin(self):
        self.manifest.unlink()
        with patch.object(audit.chainman, "execute") as probe:
            self.assertEqual(
                audit.javascript_audit_command(
                    self.root,
                    {**self.spec, "manager": "npm"},
                    self.root,
                ),
                ["npm", "audit", "--json"],
            )
            probe.assert_not_called()


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_PNPM_AUDIT") == "1",
    "run just javascript-test or just audit-test with the pinned JavaScript profile",
)
class NativePnpmAuditTests(unittest.TestCase):
    def test_real_audit_preserves_lock_and_sends_only_project_dependencies(self):
        version = subprocess.check_output(
            ["pnpm", "--version"],
            cwd="/",
            text=True,
            timeout=15,
        ).strip()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(("GET", self.path))
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                requests.append((self.path, json.loads(body)))
                self.send_response(
                    200 if self.path.endswith("/advisories/bulk") else 404
                )
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory(prefix="native pnpm audit ") as temporary:
            root = Path(temporary).resolve()
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "neutral-audit-fixture",
                        "version": "1.0.0",
                        "packageManager": "pnpm@" + version,
                        "dependencies": {"neutral-audit-dependency": "1.0.0"},
                    }
                )
            )
            (root / "pnpm-workspace.yaml").write_text("packages: []\npmOnFail: error\n")
            checksum = base64.b64encode(bytes(64)).decode()
            lock = root / "pnpm-lock.yaml"
            lock.write_text(
                "lockfileVersion: '9.0'\n"
                "settings:\n  autoInstallPeers: false\n  excludeLinksFromLockfile: false\n"
                "importers:\n  .:\n    dependencies:\n      neutral-audit-dependency:\n"
                "        specifier: 1.0.0\n        version: 1.0.0\n"
                "packages:\n  neutral-audit-dependency@1.0.0:\n"
                f"    resolution: {{integrity: sha512-{checksum}}}\n"
                "snapshots:\n  neutral-audit-dependency@1.0.0: {}\n"
            )
            before = lock.read_bytes()
            env = {
                "PATH": os.environ["PATH"],
                "HOME": str(root / "home"),
                "TMPDIR": str(root),
                "PNPM_HOME": str(root / "manager-home"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_DATA_HOME": str(root / "data"),
                "CI": "true",
            }
            for prefix in (
                "PNPM_CONFIG_",
                "pnpm_config_",
                "NPM_CONFIG_",
                "npm_config_",
            ):
                for key, value in {
                    "pm_on_fail": "error",
                    "manage_package_manager_versions": "false",
                    "verify_deps_before_run": "error",
                    "fetch_retries": "0",
                    "registry": f"http://127.0.0.1:{server.server_port}/",
                }.items():
                    env[prefix + (key.upper() if prefix.isupper() else key)] = value
            cfg = {
                "updates": {
                    "adapters": {
                        "js": {"adapter": "javascript", "profile": "selected-js"}
                    },
                    "steps": [{"resolve": "js"}],
                }
            }
            calls = []

            def execute(project, profile, argv, **kwargs):
                self.assertEqual((project, profile), (root, "selected-js"))
                calls.append((argv, kwargs["cwd"]))
                return subprocess.run(argv, timeout=20, **kwargs)

            output = io.StringIO()
            with (
                patch.object(audit.tc, "config", return_value=cfg),
                patch.object(audit.tc, "environment", return_value=env),
                patch.object(audit.chainman, "execute", side_effect=execute),
                redirect_stdout(output),
            ):
                status = audit.run_retained(root, ["targets=js"], root / "retained")
            self.assertEqual(status, 0, output.getvalue())
            self.assertTrue(json.loads(output.getvalue())["passed"])
            self.assertEqual(calls[0], (["pnpm", "--version"], Path("/")))
            self.assertEqual(calls[1][1], root)
            self.assertEqual(
                requests,
                [
                    (
                        "/-/npm/v1/security/advisories/bulk",
                        {"neutral-audit-dependency": ["1.0.0"]},
                    )
                ],
            )
            self.assertEqual(lock.read_bytes(), before)
            self.assertNotIn(b"packageManagerDependencies:", lock.read_bytes())
            self.assertFalse((root / "node_modules").exists())
            self.assertFalse((root / "manager-home/.tools").exists())
            self.assertFalse((root / "data/pnpm/.tools").exists())

            # pnpm 12 returns a complete lock-only report; some older CLIs exit
            # successfully without JSON. Preserve valid reports and fail closed
            # on incomplete evidence, without resolving another manager.
            manifest_bytes = (root / "package.json").read_bytes()
            (root / "package.json").unlink()
            requests.clear()
            calls.clear()
            direct = subprocess.run(
                ["pnpm", *pnpm_setup.options(version, None), "audit", "--json"],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertIn(direct.returncode, (0, 1), direct.stdout + direct.stderr)
            try:
                direct_report = json.loads(direct.stdout)
            except ValueError:
                expected_status = 1
            else:
                expected_status = (
                    direct.returncode
                    if isinstance(direct_report, dict)
                    and any(
                        key in direct_report
                        for key in ("advisories", "vulnerabilities")
                    )
                    else 1
                )
            expected_requests = list(requests)
            self.assertFalse(any(method == "GET" for method, _ in requests))
            self.assertEqual(lock.read_bytes(), before)
            requests.clear()
            output = io.StringIO()
            with (
                patch.object(audit.tc, "config", return_value=cfg),
                patch.object(audit.tc, "environment", return_value=env),
                patch.object(audit.chainman, "execute", side_effect=execute),
                redirect_stdout(output),
            ):
                status = audit.run_retained(root, ["targets=js"], root / "retained")
            self.assertEqual(status, expected_status, output.getvalue())
            self.assertEqual(calls[0], (["pnpm", "--version"], Path("/")))
            self.assertEqual(calls[1][1], root)
            self.assertEqual(requests, expected_requests)
            self.assertEqual(lock.read_bytes(), before)
            self.assertFalse((root / "package.json").exists())
            (root / "package.json").write_bytes(manifest_bytes)

            # Suppressing pnpm's duplicate manager handling must not authorize
            # an audit with a different SDK than the project's exact pin.
            manifest = json.loads((root / "package.json").read_text())
            manifest["packageManager"] = "pnpm@0.0.0"
            (root / "package.json").write_text(json.dumps(manifest))
            requests.clear()
            calls.clear()
            output = io.StringIO()
            with (
                patch.object(audit.tc, "config", return_value=cfg),
                patch.object(audit.tc, "environment", return_value=env),
                patch.object(audit.chainman, "execute", side_effect=execute),
                redirect_stdout(output),
            ):
                status = audit.run_retained(root, ["targets=js"], root / "retained")
            self.assertEqual(status, 1)
            self.assertIn("pnpm mismatch", output.getvalue())
            self.assertEqual(calls, [(["pnpm", "--version"], Path("/"))])
            self.assertEqual(requests, [])
            self.assertEqual(lock.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
