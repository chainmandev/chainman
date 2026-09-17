"""Independent fixtures for public entry, complete preflight and nested leases."""

import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import test_workflows
import admission
import chainman
import reentry
import toolchain as tc
import workflows


class EntryAdmissionTests(unittest.TestCase):
    setUp = test_workflows.WorkflowTests.setUp
    write_config = test_workflows.WorkflowTests.write_config
    run_cli = test_workflows.WorkflowTests.run_cli

    def configure(self):
        self.body = self.body.replace("schema=2", "schema=3")
        self.body += '\n[profiles.host]\nentry_setup=["dependencies"]\n'
        self.write_config()

    def test_exec_shell_admit_setup_and_installers_do_not_recurse(self):
        self.configure()
        for action in ("exec", "shell"):
            with patch.dict(os.environ, CHAINMAN_SETUP="error"):
                failed = self.run_cli(action, "--", "python3", "task.py")
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("dependencies", failed.stderr)
            self.assertFalse((self.root / "installed").exists())
        result = self.run_cli("setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(
            "exec", "--", "python3", "task.py", "space argument", "", "$(literal)"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((self.root / "arguments.json").read_text()),
            ["space argument", "", "$(literal)"],
        )
        self.assertEqual((self.root / "install-count").read_text(), "1")

    def test_live_shell_blocks_installation(self):
        self.configure()
        self.assertEqual(self.run_cli("setup").returncode, 0)
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(chainman.__file__)),
                "--root",
                str(self.root),
                "shell",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; import time; Path('alive').touch(); time.sleep(10)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if (self.root / "alive").exists():
                    break
                time.sleep(0.02)
            self.assertTrue((self.root / "alive").exists())
            (self.root / "input.lock").write_text("changed")
            failed = self.run_cli("setup")
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("another task", failed.stderr)
            self.assertEqual((self.root / "installed").read_text(), "one")
        finally:
            os.killpg(child.pid, signal.SIGTERM)
            child.communicate(timeout=5)

    def test_preflight_checks_later_tasks_services_watch_and_setup_profiles(self):
        self.configure()
        self.body += (
            '\n[tasks.later]\nallowed_modes=["container-nix"]\ncommands=[["false"]]\n'
        )
        self.write_config()
        with patch.dict(os.environ, CHAINMAN_MODE="host"):
            failed = self.run_cli("preflight", "build", "later")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("later", failed.stderr)
        self.assertFalse((self.root / "installed").exists())
        cfg = workflows.configuration(self.root)
        cfg["profiles"] = {"host": {"allowed_platforms": ["Darwin"]}}
        with patch.dict(os.environ, CHAINMAN_HOST_PLATFORM="Linux"):
            with self.assertRaisesRegex(ValueError, "Profile host"):
                admission.graph(self.root, cfg, [], groups=["dependencies"])
        cfg["profiles"] = {}
        cfg["tasks"]["build"]["services"] = ["worker"]
        cfg["services"] = {"worker": {"command": ["true"], "watch": {"task": "later"}}}
        with patch.dict(os.environ, CHAINMAN_MODE="host-nix"):
            with self.assertRaisesRegex(ValueError, "later"):
                admission.graph(self.root, cfg, ["build"])

    def test_unknown_requirements_fail_configuration(self):
        self.configure()
        for declaration in (
            'allowed_modes=["automatic"]',
            'entry_setup=["missing"]',
            "allowed_platforms=[]",
        ):
            original = self.body
            self.body += declaration + "\n"
            self.write_config()
            with self.assertRaises(ValueError):
                workflows.configuration(self.root)
            self.body = original

    def test_service_receipt_reuse_requires_inherited_operation_and_unchanged_inputs(
        self,
    ):
        self.configure()
        self.body += '\n[services.worker]\ncommand=["true"]\n[tasks.borrow]\nservices=["worker"]\ncommands=[["true"]]\n'
        self.write_config()
        cfg = workflows.configuration(self.root)
        with (
            tc.operation(self.root, exclusive=False),
            reentry.service_context(
                self.root, cfg, "borrow", tc.environment(self.root), True
            ) as (env, descriptors),
        ):
            options = {"env": env, "pass_fds": descriptors}
            tc.managed_options(options)
            with (
                patch.dict(os.environ, options["env"]),
                tc.operation_state(None, None, "", None, ()),
            ):
                self.assertTrue(reentry.borrow(self.root, "borrow"))
                self.body += '\n[tasks.changed]\ncommands=[["false"]]\n'
                self.write_config()
                with self.assertRaisesRegex(ValueError, "stale"):
                    reentry.borrow(self.root, "borrow")

    def test_nested_entry_rejects_other_root_pin_and_marker_only_services(self):
        self.configure()
        pin = "a" * 40
        (self.root / "chainman.lock").write_text(pin + "\n")
        runtime = Path(chainman.__file__).resolve().parents[1]
        self.assertEqual(self.run_cli("setup").returncode, 0)
        script = self.root / "literal script.sh"
        script.write_text('printf "%s\\n" "$1"; cat; exit 37\n\n')
        result = subprocess.run(
            [
                sys.executable,
                str(runtime / "scripts/chainman.py"),
                "--root",
                str(self.root),
                "exec",
                "--",
                str(runtime / "bootstrap/reenter.sh"),
                str(self.root),
                "--entry",
                "script",
                str(script),
                "literal argument",
            ],
            input="literal stdin\n",
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(result.stdout, "literal argument\nliteral stdin\n")
        with patch.dict(
            os.environ,
            CHAINMAN_ROOT=str(self.root / "other"),
            CHAINMAN_ACTIVE_PROFILE="host",
            CHAINMAN_ACTIVE_PIN=pin,
        ):
            with self.assertRaisesRegex(ValueError, "different project"):
                reentry.validate(self.root)
        with patch.dict(
            os.environ, CHAINMAN_ACTIVE_MODE="host-nix", CHAINMAN_MODE="host"
        ):
            with self.assertRaisesRegex(ValueError, "different mode"):
                reentry.validate(self.root)
        with patch.dict(
            os.environ,
            CHAINMAN_ROOT=str(self.root),
            CHAINMAN_ACTIVE_PROFILE="host",
            CHAINMAN_ACTIVE_PIN="b" * 40,
        ):
            with self.assertRaisesRegex(ValueError, "pin changed"):
                reentry.validate(self.root)
        self.body += '\n[services.worker]\ncommand=["true"]\n[tasks.borrow]\nservices=["worker"]\ncommands=[["true"]]\n'
        self.write_config()
        with patch.dict(os.environ, CHAINMAN_SERVICE_CONTEXT_FD="99"):
            with self.assertRaisesRegex(ValueError, "No live service"):
                reentry.borrow(self.root, "borrow")


if __name__ == "__main__":
    unittest.main()
