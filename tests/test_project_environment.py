import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import project_environment as pe
import chainman


class ProjectEnvironmentTests(unittest.TestCase):
    def test_explicit_host_network_keeps_bindings_on_host_loopback(self):
        values = pe.expand(
            {"BIND": "{bind}", "HOST": "{host}"},
            Path("/project"),
            {
                "CHAINMAN_MODE": "container-nix",
                "CHAINMAN_CONTAINER_NETWORK_MODE": "host",
            },
        )
        self.assertEqual(values, {"BIND": "127.0.0.1", "HOST": "127.0.0.1"})

    def test_profile_planning_does_not_execute_project_git_fsmonitor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "chainman.toml").write_text(
                'schema=2\n[profiles.default]\nflake="flake.nix#default"\n'
            )
            (root / "flake.nix").write_text("{}")
            subprocess.run(["git", "-C", str(root), "add", "flake.nix"], check=True)
            monitor = root / "monitor"
            monitor.write_text(
                '#!/bin/sh\ntouch "$(dirname "$0")/executed"\nprintf "token\\000"\n'
            )
            monitor.chmod(0o755)
            subprocess.run(
                ["git", "-C", str(root), "config", "core.fsmonitor", str(monitor)],
                check=True,
            )
            reference, _ = chainman.profile(root, "default")
            self.assertTrue(reference.startswith("git+file:"))
            self.assertFalse((root / "executed").exists())

    def test_literal_files_precedence_modes_and_dependent_bindings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "local.env").write_text(
                "USER_VALUE=file\nLITERAL=$(touch forbidden) 'quoted' {root}\nEMPTY=\n"
            )
            (root / "pins.env").write_text("PIN=immutable\n")
            spec = {
                "files": [
                    {"path": "local.env"},
                    {"path": "pins.env", "required": True, "override": True},
                ],
                "defaults": {
                    "USER_VALUE": "default",
                    "BINDING": "{bind}",
                    "DATA": "{env:BASE}/data",
                    "BASE": "{cache}/sdk",
                },
                "values": {"FIXED": "project"},
                "modes": {
                    "container-nix": {
                        "defaults": {"BINDING": "{host}"},
                        "values": {"FIXED": "container"},
                    }
                },
            }
            env = pe.apply(
                root,
                spec,
                {
                    "USER_VALUE": "caller",
                    "PIN": "caller",
                    "FIXED": "caller",
                    "CHAINMAN_MODE": "container-nix",
                    "TOOLCHAIN_DOWNLOAD_CACHE": "/downloads",
                },
            )
            self.assertEqual(env["USER_VALUE"], "caller")
            self.assertEqual(env["PIN"], "immutable")
            self.assertEqual(env["LITERAL"], "$(touch forbidden) 'quoted' {root}")
            self.assertEqual(env["EMPTY"], "")
            self.assertEqual(env["DATA"], "/downloads/sdk/data")
            self.assertEqual(env["FIXED"], "container")
            self.assertEqual(env["BINDING"], "host.docker.internal")
            self.assertFalse((root / "forbidden").exists())

    def test_invalid_files_bindings_and_ownership_overrides_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for body in (
                "A=one\nA=two\n",
                "export A=one\n",
                "CHAINMAN_MODE=host-nix\n",
                "BASH_ENV\n",
            ):
                (root / "bad.env").write_text(body)
                with self.assertRaises(ValueError):
                    pe.apply(root, {"files": [{"path": "bad.env"}]}, {})
            with self.assertRaises(ValueError):
                pe.expand({"A": "{env:B}", "B": "{env:A}"}, root, {})
            with self.assertRaises(ValueError):
                pe.expand({"A": "{env:MISSING}"}, root, {})
            with self.assertRaises(ValueError):
                pe.expand({"SCCACHE_SERVER_UDS": "/foreign"}, root, {})
            (root / "outside").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                pe.files(root, {"files": [{"path": "outside"}]})

    def test_optional_file_creation_and_byte_changes_invalidate_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = {"files": [{"path": "local.env"}]}
            missing = pe.file_fingerprint(root, spec)
            (root / "local.env").write_text("SETTING=one\n")
            first = pe.file_fingerprint(root, spec)
            (root / "local.env").write_text("SETTING=two\n")
            self.assertEqual(len({missing, first, pe.file_fingerprint(root, spec)}), 3)

    def test_host_environment_is_filtered_data_and_never_executed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b"APP_VALUE=two lines\nsecond\0UNDECLARED_SECRET=hidden\0BASH_ENV=/project/hook\0PYTHONPATH=/project\0"
            (root / "host-environment").write_bytes(data)
            before = dict(os.environ)
            selected = pe.host_inputs(root, {"pass": ["APP_*", "BASH_ENV"]})
            self.assertEqual(
                selected,
                {"APP_VALUE": "two lines\nsecond", "BASH_ENV": "/project/hook"},
            )
            self.assertEqual(dict(os.environ), before)

    def test_service_transport_ports_are_selected_without_task_port_collisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chainman.lock").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "version": "fixture",
                        "revision": "fixture",
                        "url": "https://example.invalid/runtime.tar.gz",
                        "narHash": "sha256-" + "a" * 43 + "=",
                    }
                )
            )
            (root / "chainman.toml").write_text(
                'schema=2\n[container]\nhost_access=true\n[services.frontend.transport]\nports=["127.0.0.1:8080:8080"]\n[services.api.transport]\nports=["127.0.0.1:9090:9090"]\n[tasks.verify]\ncontext_environment={APP_CONTEXT="selected"}\ncommands=[["true"]]\n'
            )
            helper = Path(__file__).resolve().parents[1] / "bootstrap/fetch.nix"

            def options(action, name):
                return subprocess.check_output(
                    [
                        "nix",
                        "--extra-experimental-features",
                        "nix-command",
                        "eval",
                        "--impure",
                        "--raw",
                        "--expr",
                        f'import {helper} {{ root = {json.dumps(str(root))}; action = "options"; }}',
                    ],
                    env=dict(
                        os.environ,
                        CHAINMAN_REQUEST_ACTION=action,
                        CHAINMAN_REQUEST_TASK=name,
                    ),
                    text=True,
                )

            frontend = options("_workflow-service", "frontend")
            self.assertIn("127.0.0.1:8080:8080", frontend)
            self.assertIn("--env-pattern\nAPP_CONTEXT\n", frontend)
            self.assertNotIn("9090", frontend)
            self.assertNotIn("--publish", options("_workflow-task", "verify"))
            self.assertEqual(options("_control-export", "ignored"), "")


if __name__ == "__main__":
    unittest.main()
