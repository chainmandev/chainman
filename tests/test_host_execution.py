"""Real verified Git launches with only caller-provided Python and shell tools."""

import json
import os
import shutil
import signal
import subprocess
import unittest

import test_git_bootstrap as fixture

SOURCE = fixture.SOURCE


class HostExecutionTests(unittest.TestCase):
    git_run = fixture.GitBootstrapTests.git_run
    run_entry = fixture.GitBootstrapTests.run_entry

    def setUp(self):
        fixture.GitBootstrapTests.setUp(self)
        shutil.copytree(
            SOURCE / "scripts",
            self.origin / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        for filename in ("chainman.sh", "git-entry.sh"):
            shutil.copy2(
                SOURCE / "bootstrap" / filename, self.origin / "bootstrap" / filename
            )
        shutil.copy2(SOURCE / "VERSION", self.origin / "VERSION")
        self.git_run("add", ".")
        self.git_run("-c", "commit.gpgsign=false", "commit", "-qm", "Host runtime")
        self.revision = self.git_run("rev-parse", "HEAD").stdout.strip()
        (self.project / "chainman.lock").write_text(self.revision + "\n")
        for executable in ("python3", "dirname", "basename", "sed", "env", "bash"):
            path = shutil.which(executable)
            self.assertIsNotNone(path, executable)
            (self.binaries / executable).symlink_to(path)
        self.env.update(
            CHAINMAN_MODE="host",
            RUSTC_WRAPPER="caller-wrapper",
            CARGO_HOME="caller-cargo",
        )
        self.config = """schema = 3
[profiles.default]
flake = "absent/flake.nix#default"
compiler_cache = true
[profiles.default.environment]
FROM_PROFILE = "profile"
[environment.values]
FROM_PROJECT = "project"
[environment.modes.host.values]
FROM_MODE = "host"
[setup.prepare]
profile = "default"
inputs = ["input.txt"]
artifacts = ["prepared"]
commands = [["sh", "-c", "printf x >> prepared"]]
[tasks.check]
setup = ["prepare"]
commands = [["python3", "-c", "import os; print(':'.join(os.environ[k] for k in ('FROM_PROJECT','FROM_PROFILE','FROM_MODE')))"]]
"""
        (self.project / "chainman.toml").write_text(self.config)
        (self.project / "input.txt").write_text("input")

    def test_cold_warm_offline_and_caller_environment(self):
        code = "import json,os,shutil,sys; print(json.dumps([sys.argv[1:],sys.stdin.read(),os.environ['CARGO_HOME'],os.environ['RUSTC_WRAPPER'],[shutil.which(x) for x in ('nix','docker','podman','go')]]))"
        arguments = ["space argument", "", "$(touch should-not-exist)"]
        for overrides in ({}, {"BOOTSTRAP_TEST_OFFLINE": "1"}):
            result = self.run_entry(
                "exec",
                "--",
                "python3",
                "-c",
                code,
                *arguments,
                input=b"literal stdin\n",
                **overrides,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                [
                    arguments,
                    "literal stdin\n",
                    "caller-cargo",
                    "caller-wrapper",
                    [None] * 4,
                ],
            )
        self.assertFalse((self.project / "should-not-exist").exists())
        self.assertFalse((self.project / ".cache/toolchain/work").exists())

    def test_setup_reused_across_verified_exports_and_invalidated_by_input(self):
        for _ in range(2):
            result = self.run_entry("run", "check")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"project:profile:host\n")
        self.assertEqual((self.project / "prepared").read_text(), "x")
        (self.project / "input.txt").write_text("changed")
        self.assertEqual(self.run_entry("run", "check").returncode, 0)
        self.assertEqual((self.project / "prepared").read_text(), "xx")

    def test_entire_task_graph_rejected_before_setup(self):
        for requirement in (
            "cleanup_children = true",
            "timeout_seconds = 2",
            'timeout_env = "DEADLINE"',
            'services = ["database"]',
        ):
            with self.subTest(requirement=requirement):
                (self.project / "chainman.toml").write_text(
                    self.config
                    + '\n[tasks.blocked]\ndepends_on = ["check"]\ncommands = [["true"]]\n'
                    + requirement
                    + "\n"
                )
                result = self.run_entry("run", "blocked")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"requires Nix execution", result.stderr)
                self.assertFalse((self.project / "prepared").exists())

    def test_entire_recipe_rejected_before_earlier_setup_and_commands(self):
        for requirement in (
            "cleanup_children = true",
            "timeout_seconds = 2",
            'timeout_env = "DEADLINE"',
            'services = ["database"]',
        ):
            with self.subTest(requirement=requirement):
                (self.project / "chainman.toml").write_text(
                    self.config
                    + '\n[tasks.blocked]\ncommands=[["true"]]\n'
                    + requirement
                    + '\n[tasks.last]\ndepends_on=["blocked"]\ncommands=[["true"]]\n'
                    + '[recipes]\nverify=["check", "last"]\n'
                )
                result = self.run_entry("recipe", "verify")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"requires Nix execution", result.stderr)
                self.assertEqual(result.stdout, b"")
                self.assertFalse((self.project / "prepared").exists())

    def test_supported_recipe_keeps_order_arguments_and_failure_status(self):
        (self.project / "chainman.toml").write_text(
            self.config
            + '\n[tasks.last]\ncommands=[["sh", "-c", '
            + '"test -f prepared; printf \'%s\\\\n\' \\"$@\\"; exit 37", "last"]]\n'
            + '[recipes]\nverify=["check", "last"]\n'
        )
        result = self.run_entry("recipe", "verify", "--", "literal argument", "")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(result.stdout, b"project:profile:host\nliteral argument\n\n")
        self.assertTrue((self.project / "prepared").exists())

    def test_unsupported_operations_never_provision(self):
        for command in (
            "deps-update",
            "chainman-update",
            "deps-query",
            "format",
            "services-up",
            "_control-export",
            "cache-prune",
            "module",
        ):
            with self.subTest(command=command):
                result = self.run_entry(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"requires CHAINMAN_MODE", result.stderr)
        self.assertFalse((self.project / "prepared").exists())

    def test_configuration_version_missing_tools_and_exit_status(self):
        for arguments in (("config", "validate"), ("version",), ("doctor",)):
            result = self.run_entry(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_entry("exec", "--", "missing-host-tool")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"missing-host-tool", result.stderr)
        result = self.run_entry("exec", "--", "sh", "-c", "exit 37")
        self.assertEqual(result.returncode, 37)

    def test_signal_reaches_command(self):
        command = [
            str(self.binaries / "just"),
            "chainman",
            "exec",
            "--",
            "python3",
            "-c",
            "import signal,time; print('ready',flush=True); signal.signal(signal.SIGTERM,lambda *_:exit(42)); time.sleep(20)",
        ]
        child = subprocess.Popen(
            command,
            cwd=self.project,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline(), b"ready\n")
        os.killpg(child.pid, signal.SIGTERM)
        child.communicate(timeout=10)
        self.assertIsNotNone(child.returncode)


if __name__ == "__main__":
    unittest.main()
