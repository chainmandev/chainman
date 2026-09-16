"""Real setup probes reject stale installs before task admission."""

import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import setup_readiness
import toolchain
import test_workflows


class SetupReadinessTests(unittest.TestCase):
    write_config = test_workflows.WorkflowTests.write_config
    run_cli = test_workflows.WorkflowTests.run_cli

    def setUp(self):
        test_workflows.WorkflowTests.setUp(self)
        self.body = self.body.replace(
            "[tasks.build]",
            'readiness={command=["python3","check.py"], timeout_seconds=2}\n[tasks.build]',
        )
        (self.root / "check.py").write_text(
            "from pathlib import Path; import sys; "
            "sys.exit(0 if Path('installed').stat().st_mtime_ns >= "
            "Path('input.lock').stat().st_mtime_ns else 'patch timestamp is newer')"
        )
        self.write_config()

    def test_unchanged_patch_requires_repair_and_explicit_setup_repairs(self):
        self.assertEqual(self.run_cli("setup").returncode, 0)
        installed = (self.root / "installed").stat().st_mtime_ns
        os.utime(self.root / "input.lock", ns=(installed + 1, installed + 1))
        status = self.run_cli("setup-status")
        self.assertEqual(status.returncode, 1, status.stderr)
        self.assertEqual(
            json.loads(status.stdout)["details"]["dependencies"]["reason"],
            "readiness-failed",
        )
        with patch.dict(os.environ, CHAINMAN_SETUP="error"):
            refused = self.run_cli("run", "build")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("patch timestamp is newer", refused.stderr)
            self.assertFalse((self.root / "arguments.json").exists())
            self.assertEqual(self.run_cli("setup").returncode, 0)
            self.assertEqual(self.run_cli("run", "build").returncode, 0)
        self.assertEqual((self.root / "install-count").read_text(), "2")

    def test_failed_repair_removes_prior_stamp(self):
        self.assertEqual(self.run_cli("setup").returncode, 0)
        (self.root / "check.py").write_text("raise SystemExit('broken installation')")
        result = self.run_cli("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed validation after installation", result.stderr)
        self.assertFalse(
            (self.root / ".cache/toolchain/setup-groups/dependencies.json").exists()
        )

    def test_probe_receives_no_task_stdin_and_timeout_fails_closed(self):
        (self.root / "check.py").write_text("import sys; assert sys.stdin.read() == ''")
        self.assertEqual(self.run_cli("setup").returncode, 0)
        (self.root / "check.py").write_text("import time; time.sleep(5)")
        result = self.run_cli("setup-status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out", result.stdout)

    def test_invalid_probe_is_rejected_before_installation(self):
        for probe in (
            {},
            {"command": []},
            {"command": ["true"], "timeout_seconds": 0},
            {"command": ["true"], "timeout_seconds": True},
        ):
            with self.assertRaises(ValueError):
                setup_readiness.declaration(probe)

    def test_legacy_module_repair_requires_consent_but_explicit_setup_does_not(self):
        spec = {
            "name": "fixture",
            "directory": ".",
            "profile": "host",
            "inputs": ["input.lock", "install.py"],
            "artifacts": ["installed"],
            "commands": {"setup": [["python3", "install.py"]]},
        }
        env = dict(toolchain.environment(self.root), CHAINMAN_SETUP="error")
        with self.assertRaisesRegex(ValueError, "just chainman modules setup"):
            toolchain.setup(spec, env, self.root)
        toolchain.setup(spec, env, self.root, explicit=True)
        toolchain.setup(spec, env, self.root)

    def test_readiness_cannot_bless_inputs_modified_by_the_probe(self):
        self.assertEqual(self.run_cli("setup").returncode, 0)
        (self.root / "check.py").write_text(
            "from pathlib import Path; Path('input.lock').write_text('changed')"
        )
        result = self.run_cli("setup-status")
        self.assertEqual(result.returncode, 1)
        self.assertIn("inputs-changed-during-readiness", result.stdout)

    def test_post_install_probe_cannot_remove_a_required_artifact(self):
        (self.root / "check.py").write_text(
            "from pathlib import Path; Path('installed').unlink()"
        )
        result = self.run_cli("setup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifacts changed during validation", result.stderr)
        self.assertFalse(
            (self.root / ".cache/toolchain/setup-groups/dependencies.json").exists()
        )

    def test_legacy_setup_does_not_stamp_changed_inputs(self):
        spec = {
            "name": "fixture",
            "directory": ".",
            "profile": "host",
            "inputs": ["input.lock"],
            "artifacts": ["input.lock"],
            "commands": {
                "setup": [
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; Path('input.lock').write_text('changed')",
                    ]
                ]
            },
        }
        with self.assertRaisesRegex(ValueError, "inputs changed during installation"):
            toolchain.setup(
                spec, toolchain.environment(self.root), self.root, explicit=True
            )
        self.assertFalse((self.root / ".cache/toolchain/setup/fixture.json").exists())

    def test_prompt_answers_and_noninteractive_recovery(self):
        class Terminal(io.StringIO):
            def __init__(self, answer):
                super().__init__()
                self.answer = answer

            def readline(self):
                return self.answer

        for answer in ("\n", "y\n", "Y\n", "yes\n"):
            with patch("builtins.open", return_value=Terminal(answer)):
                setup_readiness.authorize({"javascript": "stale"}, {})
        for answer in ("n\n", ""):
            with patch("builtins.open", return_value=Terminal(answer)):
                with self.assertRaisesRegex(
                    ValueError, "just chainman setup javascript"
                ):
                    setup_readiness.authorize({"javascript": "stale"}, {})
        with patch("builtins.open", side_effect=OSError("no tty")):
            with self.assertRaisesRegex(ValueError, "No task commands"):
                setup_readiness.authorize({"javascript": "stale"}, {})
