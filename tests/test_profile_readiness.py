"""Imported toolchain files and nested entry must participate in task admission."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_workflows
import chainman
import toolchain as tc
import workflows


class ProfileReadinessTests(unittest.TestCase):
    setUp = test_workflows.WorkflowTests.setUp
    write_config = test_workflows.WorkflowTests.write_config
    run_cli = test_workflows.WorkflowTests.run_cli

    def test_profile_inputs_invalidate_import_changes_additions_and_removals(self):
        self.body += '[profiles.tools]\nflake="nix#default"\ninputs=["nix/*.nix", "nix/*.toml"]\n'
        self.write_config()
        folder = self.root / "nix"
        folder.mkdir()
        (folder / "flake.nix").write_text("import ./tools.nix")
        (folder / "tools.nix").write_text("one")
        ref, _ = chainman.profile(self.root, "tools")
        first = chainman.profile_fingerprint(self.root, "tools", ref)
        (self.root / "application.ts").write_text("unrelated")
        self.assertEqual(first, chainman.profile_fingerprint(self.root, "tools", ref))
        (folder / "tools.nix").write_text("two")
        second = chainman.profile_fingerprint(self.root, "tools", ref)
        self.assertNotEqual(first, second)
        (folder / "pins.toml").write_text("pin=1")
        self.assertNotEqual(
            second, chainman.profile_fingerprint(self.root, "tools", ref)
        )
        (folder / "pins.toml").unlink()
        self.assertEqual(second, chainman.profile_fingerprint(self.root, "tools", ref))
        (folder / "escape.nix").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError, "symlink"):
            chainman.profile_fingerprint(self.root, "tools", ref)

    def test_profile_input_validation_and_bare_host_do_not_require_nix_inputs(self):
        self.body += (
            '[profiles.tools]\nflake="absent#default"\ninputs=["absent/*.nix"]\n'
        )
        self.write_config()
        with patch.dict(os.environ, CHAINMAN_MODE="host"):
            ref, _ = chainman.profile(self.root, "tools")
            self.assertIsNone(ref)
            chainman.profile_fingerprint(self.root, "tools", ref)
            self.body = self.body.replace('"absent/*.nix"', '"../escape"')
            self.write_config()
            with self.assertRaisesRegex(ValueError, "unsafe"):
                chainman.profile(self.root, "tools")

    def test_status_names_changed_added_missing_inputs_without_writing_stamp(self):
        self.body = self.body.replace(
            '"input.lock","install.py"', '"*.lock","install.py"'
        )
        self.write_config()
        (self.root / "old.lock").write_text("old")
        self.assertEqual(self.run_cli("setup").returncode, 0)
        stamp = workflows.stamp_path(self.root, "dependencies")
        before = stamp.read_bytes()
        (self.root / "input.lock").write_text("two")
        (self.root / "old.lock").unlink()
        (self.root / "new.lock").write_text("new")
        status = self.run_cli("setup-status")
        detail = json.loads(status.stdout)["details"]["dependencies"]
        self.assertEqual(
            detail["changed_inputs"],
            {
                "changed": ["setup:input.lock"],
                "added": ["setup:new.lock"],
                "missing": ["setup:old.lock"],
            },
        )
        self.assertEqual(detail["recovery"], ["setup", "dependencies"])
        self.assertEqual(stamp.read_bytes(), before)

    def test_imported_profile_input_changes_invalidate_setup_and_environment_reuse(
        self,
    ):
        self.body = self.body.replace(
            'default_profile="host"', 'default_profile="tools"'
        )
        self.body += '[profiles.tools]\nflake="nix#default"\ninputs=["nix/*.nix"]\n'
        self.write_config()
        folder = self.root / "nix"
        folder.mkdir()
        (folder / "flake.nix").write_text("import ./tools.nix")
        (folder / "tools.nix").write_text("one")
        ref, _ = chainman.profile(self.root, "tools")
        before = chainman.profile_fingerprint(self.root, "tools", ref)
        env = dict(
            os.environ,
            CHAINMAN_ACTIVE_PROFILE="tools",
            CHAINMAN_ACTIVE_FINGERPRINT=before,
        )
        cfg = workflows.configuration(self.root)
        spec = workflows.group_specs(self.root, cfg, ["dependencies"], env)[
            "dependencies"
        ]
        with tc.operation(self.root, exclusive=False):
            with workflows.setup_use(
                self.root, cfg, ["dependencies"], env, explicit=True
            ):
                pass
        (folder / "tools.nix").write_text("two")
        detail = workflows.setup_detail(self.root, "dependencies", spec, env)
        self.assertIn("profile:nix/tools.nix", detail["changed_inputs"]["changed"])
        # The old active token can no longer suppress Nix environment entry.
        with patch.object(tc, "managed_run") as run:
            chainman.execute(self.root, "tools", ["true"], env=env)
            self.assertIn("develop", run.call_args.args[0])

    def test_nested_task_checks_setup_preserves_input_and_rejects_changed_pin(self):
        pin = "a" * 40
        (self.root / "chainman.lock").write_text(pin + "\n")
        runtime = Path(chainman.__file__).resolve().parents[1]
        entry = runtime / "bootstrap/reenter.sh"
        command = ["exec", "--", str(entry), str(self.root), "build"]
        with patch.dict(os.environ, CHAINMAN_SETUP="error"):
            refused = self.run_cli(*command)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("dependencies", refused.stderr)
        self.assertFalse((self.root / "arguments.json").exists())
        self.assertEqual(self.run_cli("setup").returncode, 0)
        arguments = ["space argument", "", "$(not-a-command)"]
        succeeded = self.run_cli(*command, *arguments)
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
        self.assertEqual(
            json.loads((self.root / "arguments.json").read_text()), arguments
        )
        (self.root / "task.py").write_text(
            "import sys; print(sys.stdin.read(),end=''); sys.exit(37)"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(runtime / "scripts/chainman.py"),
                "--root",
                str(self.root),
                *command,
            ],
            input="literal stdin\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(result.stdout, "literal stdin\n")
        with patch.dict(
            os.environ,
            CHAINMAN_ROOT=str(self.root),
            CHAINMAN_ACTIVE_PROFILE="host",
            CHAINMAN_ACTIVE_PIN="b" * 40,
            CHAINMAN_RUNTIME_PYTHON=sys.executable,
        ):
            changed = subprocess.run(
                [str(entry), str(self.root), "build"], capture_output=True, text=True
            )
        self.assertEqual(changed.returncode, 2)
        self.assertIn("leave this shell", changed.stderr)
        with patch.dict(
            os.environ,
            CHAINMAN_ROOT=str(self.root),
            CHAINMAN_ACTIVE_PROFILE="host",
            CHAINMAN_ACTIVE_PIN=pin,
            CHAINMAN_RUNTIME_PYTHON=sys.executable,
        ):
            (self.root / "chainman.lock").write_text(pin + "\n\n")
            malformed = subprocess.run(
                [str(entry), str(self.root), "build"], capture_output=True, text=True
            )
            self.assertEqual(malformed.returncode, 2)
            self.assertIn("malformed", malformed.stderr)
