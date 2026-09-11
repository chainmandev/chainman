"""Setup readiness and shared-use contracts exercised with real fixture commands."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workflows
import toolchain as tc


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman workflow ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        env["TOOLCHAIN_DOWNLOAD_CACHE"] = str(self.root / "downloads")
        context = patch.dict(os.environ, env, clear=True)
        context.start()
        self.addCleanup(context.stop)
        (self.root / "input.lock").write_text("one")
        (self.root / "install.py").write_text("""from pathlib import Path
p=Path('install-count')
p.write_text(str(int(p.read_text())+1 if p.exists() else 1))
Path('installed').write_text(Path('input.lock').read_text())
""")
        (self.root / "task.py").write_text("""import json,sys
from pathlib import Path
assert Path('installed').read_text()==Path('input.lock').read_text()
Path('arguments.json').write_text(json.dumps(sys.argv[1:]))
""")
        self.body = """schema=2
[project]
default_profile="host"
[setup.dependencies]
inputs=["input.lock","install.py"]
artifacts=["installed"]
commands=[["python3","install.py"]]
[tasks.build]
setup=["dependencies"]
commands=[["python3","task.py"]]
"""
        self.write_config()

    def write_config(self):
        (self.root / "chainman.toml").write_text(self.body)

    def run_cli(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(Path(workflows.__file__).with_name("chainman.py")),
                "--root",
                str(self.root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_changed_inputs_and_missing_outputs_reinstall_before_task(self):
        for iteration, change in enumerate((None, "input", "output"), 1):
            if change == "input":
                (self.root / "input.lock").write_text("two")
            if change == "output":
                (self.root / "installed").unlink()
            workflows.run(self.root, "build", ["two words", "", "$(literal)"])
            self.assertEqual((self.root / "install-count").read_text(), str(iteration))
            workflows.run(self.root, "build", [])
            self.assertEqual((self.root / "install-count").read_text(), str(iteration))

    def test_exclusive_service_access_requires_services_and_a_boolean(self):
        for value in ("true", '"yes"'):
            with self.subTest(value=value):
                self.body += f"\nexclusive_services={value}\n"
                self.write_config()
                with self.assertRaisesRegex(ValueError, "exclusive_services"):
                    workflows.configuration(self.root)
                self.body = self.body.rsplit("\nexclusive_services=", 1)[0]

    def test_public_run_preserves_literal_arguments(self):
        result = self.run_cli("run", "build", "--", "two words", "", "$(literal)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((self.root / "arguments.json").read_text()),
            ["two words", "", "$(literal)"],
        )

    def test_changed_digest_artifact_rebuilds_even_when_all_inputs_are_unchanged(self):
        self.body = self.body.replace(
            'artifacts=["installed"]', 'artifacts=[{path="installed",digest=true}]'
        )
        self.write_config()
        self.assertEqual(self.run_cli("run", "build").returncode, 0)
        (self.root / "installed").write_text("another workflow's build variant")
        result = self.run_cli("run", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "installed").read_text(), "one")
        self.assertEqual((self.root / "install-count").read_text(), "2")
        self.assertEqual(self.run_cli("run", "build").returncode, 0)
        self.assertEqual((self.root / "install-count").read_text(), "2")

    def test_setup_group_can_be_requested_explicitly(self):
        result = self.run_cli("setup", "dependencies")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "installed").exists())
        self.assertFalse((self.root / "arguments.json").exists())

    def test_setup_status_never_installs_or_blesses_stale_outputs(self):
        result = self.run_cli("setup-status", "dependencies")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["groups"], {"dependencies": False})
        self.assertFalse((self.root / "install-count").exists())
        self.assertEqual(self.run_cli("setup").returncode, 0)
        self.assertEqual(self.run_cli("setup-status").returncode, 0)
        (self.root / "input.lock").write_text("changed")
        result = self.run_cli("setup-status")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.root / "install-count").read_text(), "1")
        self.assertFalse(json.loads(result.stdout)["current"])

    def test_setup_excludes_declared_generated_dependency_trees(self):
        self.body = self.body.replace(
            'inputs=["input.lock","install.py"]',
            'inputs=["**/*.lock","install.py"]\nexclude_inputs=["node_modules/**"]',
        )
        self.write_config()
        with (self.root / "install.py").open("a") as script:
            script.write(
                "Path('node_modules').mkdir(exist_ok=True)\nPath('node_modules/download.lock').write_text('generated')\n"
            )
        self.assertEqual(self.run_cli("setup").returncode, 0)
        (self.root / "node_modules/download.lock").write_text("another download")
        self.assertEqual(self.run_cli("setup-status").returncode, 0)
        (self.root / "input.lock").write_text("source changed")
        self.assertEqual(self.run_cli("setup-status").returncode, 1)

    def test_automatic_prune_runs_only_without_active_work(self):
        obsolete = self.root / ".cache/toolchain/work/old-context"
        obsolete.mkdir(parents=True)
        (obsolete / "output").write_text("disposable")
        self.body += "\n[cache]\nbuild_limit_gib=0\nstale_hours=0\n"
        self.write_config()
        with tc.operation(self.root, exclusive=False):
            result = self.run_cli("run", "build")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(obsolete.exists())
        result = self.run_cli("run", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(obsolete.exists())

    def test_reserved_setup_task_is_rejected_instead_of_silently_skipped(self):
        self.body += '\n[tasks.setup]\ncommands=[["false"]]\n'
        self.write_config()
        for args in [("run", "setup"), ("setup",)]:
            result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Task name 'setup' is reserved", result.stderr)
            self.assertFalse((self.root / "installed").exists())

    def test_aggregate_task_runs_dependencies_once_without_a_noop_command(self):
        self.body += '\n[tasks.all]\ndepends_on=["build"]\n'
        self.write_config()
        result = self.run_cli("run", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "install-count").read_text(), "1")
        self.assertTrue((self.root / "arguments.json").exists())

    def test_exclusive_maintenance_refuses_another_active_task(self):
        self.body += '\n[tasks.clean]\nexclusive=true\ncommands=[["python3","-c","from pathlib import Path; Path(\\"cleaned\\").touch()"]]\n'
        self.write_config()
        with tc.operation(self.root, exclusive=False, new_execution=True):
            result = self.run_cli("run", "clean")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "cleaned").exists())
        result = self.run_cli("run", "clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "cleaned").exists())

    def test_task_without_setup_remains_available_when_outputs_are_missing(self):
        self.body += '\n[tasks.inspect]\ncommands=[["python3","-c","print(42)"]]\n'
        self.write_config()
        result = self.run_cli("run", "inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "42")
        self.assertFalse((self.root / "installed").exists())

    def test_invalid_deadline_override_refuses_to_start_the_task(self):
        self.body += '\n[tasks.bounded]\ntimeout_seconds=600\ntimeout_env="APP_TEST_TIMEOUT"\ncommands=[["python3","-c","from pathlib import Path; Path(\\"started\\").touch()"]]\n'
        self.write_config()
        for value in ("0", "-1", "1.5", "86401", "unlimited"):
            with patch.dict(os.environ, APP_TEST_TIMEOUT=value):
                result = self.run_cli("run", "bounded")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("APP_TEST_TIMEOUT must be an integer", result.stderr)
            self.assertFalse((self.root / "started").exists())

    def test_reinstall_is_refused_while_another_task_uses_outputs(self):
        workflows.run(self.root, "build", [])
        cfg = workflows.configuration(self.root)
        with workflows.setup_use(
            self.root, cfg, ["dependencies"], tc.environment(self.root)
        ):
            (self.root / "input.lock").write_text("two")
            with self.assertRaisesRegex(
                ValueError, "another task uses installed artifacts"
            ):
                workflows.run(self.root, "build", [])
        self.assertEqual((self.root / "install-count").read_text(), "1")
        workflows.run(self.root, "build", [])
        self.assertEqual((self.root / "installed").read_text(), "two")

    def test_missing_artifact_and_changed_input_do_not_record_readiness(self):
        for script, error in [
            ("pass\n", "did not create"),
            (
                'from pathlib import Path\nPath("installed").touch()\nPath("input.lock").write_text("changed")\n',
                "inputs changed",
            ),
        ]:
            with self.subTest(error=error):
                (self.root / "install.py").write_text(script)
                with self.assertRaisesRegex(ValueError, error):
                    workflows.run(self.root, "build", [])
                self.assertFalse(
                    workflows.stamp_path(self.root, "dependencies").exists()
                )

    def test_unknown_reference_and_cycle_fail_before_installation(self):
        for extra in ['depends_on=["missing"]\n', 'depends_on=["build"]\n']:
            self.write_config()
            with (self.root / "chainman.toml").open("a") as out:
                out.write(extra)
            result = self.run_cli("run", "build")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "install-count").exists())

    def test_dependency_task_runs_once_before_requested_task(self):
        self.body = self.body.replace(
            "[tasks.build]\n", '[tasks.build]\ndepends_on=["prepare"]\n'
        )
        self.body += '\n[tasks.prepare]\ncommands=[["python3","prepare.py"]]\n'
        (self.root / "prepare.py").write_text(
            'from pathlib import Path\nPath("prepared").touch()\n'
        )
        self.write_config()
        workflows.run(self.root, "build", [])
        self.assertTrue((self.root / "prepared").exists())

    def test_task_child_retains_setup_lease_after_parent_is_killed(self):
        self.body += (
            '\n[tasks.hold]\nsetup=["dependencies"]\ncommands=[["python3","hold.py"]]\n'
        )
        self.write_config()
        (self.root / "hold.py").write_text(
            'import time\nfrom pathlib import Path\nPath("ready").touch()\ntime.sleep(30)\n'
        )
        parent = subprocess.Popen(
            [
                sys.executable,
                str(Path(workflows.__file__).with_name("chainman.py")),
                "--root",
                str(self.root),
                "run",
                "hold",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not (self.root / "ready").exists()
                and parent.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue((self.root / "ready").exists())
            parent.kill()
            parent.wait(timeout=5)
            (self.root / "input.lock").write_text("changed")
            result = self.run_cli("run", "build")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another task uses installed artifacts", result.stderr)
            self.assertEqual((self.root / "install-count").read_text(), "1")
        finally:
            try:
                os.killpg(parent.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            parent.wait(timeout=5)
            parent.stderr.close()

    def test_resource_budget_reaches_the_project_command(self):
        self.body += '\n[resources]\njob_variables=["CARGO_BUILD_JOBS"]\nmax_jobs=1\n'
        self.write_config()
        with (self.root / "task.py").open("a") as handle:
            handle.write(
                '\nimport os\nPath("jobs").write_text(os.environ["CARGO_BUILD_JOBS"])\n'
            )
        with patch.dict(os.environ):
            os.environ.pop("CARGO_BUILD_JOBS", None)
            result = self.run_cli("run", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "jobs").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
