"""The reusable pnpm declaration never downloads a replacement package manager."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pnpm_setup
import workflows


class DeclarationTests(unittest.TestCase):
    def test_checked_commands_follow_only_the_tasks_managed_setup_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = {
                "project": {"default_profile": "setup-profile"},
                "setup": {
                    "managed": {"pnpm": True},
                    "bridge": {"depends_on": ["managed"]},
                    "unrelated": {},
                },
            }
            argv = ["pnpm", "run", "check"]
            wrapped = workflows.task_command(root, cfg, {"setup": ["bridge"]}, {}, argv)
            self.assertEqual(wrapped[0], sys.executable)
            self.assertEqual(json.loads(wrapped[3]), [str(root)])
            self.assertEqual(wrapped[4:], argv)
            self.assertIs(
                workflows.task_command(root, cfg, {"setup": ["unrelated"]}, {}, argv),
                argv,
            )
            # A sibling/dependency task's setup does not grant another profile's
            # payload permission to suppress its package-manager identity guard.
            self.assertIs(
                workflows.task_command(
                    root, cfg, {"depends_on": ["managed-task"]}, {}, argv
                ),
                argv,
            )

    def test_checked_environment_rejects_a_different_task_profile_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"packageManager":"pnpm@12.1.0"}')
            with patch.object(
                pnpm_setup.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "11.0.0\n"),
            ) as probe:
                with self.assertRaisesRegex(ValueError, "pnpm mismatch"):
                    pnpm_setup.execution_environment([root])
            self.assertEqual(probe.call_args.kwargs["cwd"], "/")

    def test_checked_environment_keeps_no_install_guards_and_normalizes_aliases(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"packageManager":"pnpm@12.1.0"}')
            with (
                patch.dict(
                    os.environ,
                    pnpm_config_verify_deps_before_run="install",
                    PNPM_CONFIG_PM_ON_FAIL="error",
                ),
                patch.object(
                    pnpm_setup.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, "12.1.0\n"),
                ),
            ):
                env = pnpm_setup.execution_environment([root])
                self.assertEqual(os.environ["PNPM_CONFIG_PM_ON_FAIL"], "error")
            for prefix in (
                "npm_config_",
                "pnpm_config_",
                "NPM_CONFIG_",
                "PNPM_CONFIG_",
            ):
                lower = prefix.islower()
                self.assertEqual(
                    env[prefix + ("pm_on_fail" if lower else "PM_ON_FAIL")], "ignore"
                )
                self.assertEqual(
                    env[
                        prefix
                        + (
                            "verify_deps_before_run"
                            if lower
                            else "VERIFY_DEPS_BEFORE_RUN"
                        )
                    ],
                    "error",
                )
                self.assertEqual(
                    env[
                        prefix
                        + (
                            "manage_package_manager_versions"
                            if lower
                            else "MANAGE_PACKAGE_MANAGER_VERSIONS"
                        )
                    ],
                    "false",
                )

    def test_expansion_and_conflicting_custom_installer(self):
        for field in ("commands", "readiness"):
            with self.assertRaisesRegex(ValueError, "supplies"):
                pnpm_setup.expand({field: []})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[setup.js]
pnpm=true
inputs=["package.json","pnpm-lock.yaml"]
artifacts=["node_modules/.modules.yaml"]
""")
            spec = workflows.group_spec(workflows.configuration(root), "js")
            self.assertEqual(spec["commands"][0][-1], "install")
            self.assertEqual(spec["readiness"]["command"][-1], "check")

    def test_old_pnpm_keeps_its_explicit_no_download_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "package.json").write_text('{"packageManager":"pnpm@10.0.0"}')
            with patch.object(
                pnpm_setup.subprocess,
                "run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, "10.0.0\n"),
                    subprocess.CompletedProcess([], 0),
                ],
            ) as run:
                self.assertEqual(pnpm_setup.run("install", root), 0)
            self.assertEqual(run.call_args_list[0].kwargs["cwd"], "/")
            self.assertEqual(
                run.call_args.args[0][:3],
                [
                    "pnpm",
                    "--config.manage-package-manager-versions=false",
                    "--config.package-manager-strict-version=true",
                ],
            )


@unittest.skipUnless(shutil.which("pnpm"), "requires the pinned JavaScript profile")
class ActualPnpmTests(unittest.TestCase):
    def test_checked_task_and_nested_pnpm_preserve_the_frozen_lock(self):
        version = subprocess.check_output(
            ["pnpm", "--version"], cwd="/", text=True
        ).strip()
        with tempfile.TemporaryDirectory(prefix="pnpm checked task ") as temporary:
            root = Path(temporary)
            dependency = root / "dependency"
            dependency.mkdir()
            (dependency / "package.json").write_text(
                '{"name":"local-fixture","version":"1.0.0"}'
            )
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "checked-task-fixture",
                        "packageManager": "pnpm@" + version,
                        "dependencies": {"local-fixture": "file:./dependency"},
                        "scripts": {
                            "outer": "pnpm run inner",
                            "inner": "node -e \"require('fs').writeFileSync('ran','yes')\"",
                        },
                    }
                )
            )
            (root / "pnpm-workspace.yaml").write_text("packages: []\npmOnFail: error\n")
            subprocess.run(
                [
                    "pnpm",
                    *pnpm_setup.options(version, version),
                    "install",
                    "--offline",
                    "--lockfile-only",
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
            lock = (root / "pnpm-lock.yaml").read_bytes()
            with patch.dict(
                os.environ,
                PNPM_CONFIG_PM_ON_FAIL="error",
                pnpm_config_pm_on_fail="error",
                npm_config_pm_on_fail="error",
                npm_config_registry="http://127.0.0.1:1",
                PNPM_CONFIG_REGISTRY="http://127.0.0.1:1",
                PNPM_HOME=str(root / "manager-home"),
                XDG_DATA_HOME=str(root / "data"),
            ):
                self.assertEqual(pnpm_setup.run("install", root), 0)
                result = subprocess.run(
                    pnpm_setup.execution_command([root], ["pnpm", "run", "outer"]),
                    cwd=root,
                    text=True,
                    capture_output=True,
                )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "ran").read_text(), "yes")
            self.assertEqual((root / "pnpm-lock.yaml").read_bytes(), lock)
            self.assertNotIn(b"packageManagerDependencies:", lock)
            self.assertFalse((root / "manager-home/.tools").exists())
            self.assertFalse((root / "data/pnpm/.tools").exists())
            # Simulate a task profile with a different binary from the setup profile.
            wrong = root / "wrong-profile"
            wrong.mkdir()
            fake = wrong / "pnpm"
            fake.write_text("#!/bin/sh\nprintf '0.0.0\\n'\n")
            fake.chmod(0o755)
            (root / "ran").unlink()
            result = subprocess.run(
                pnpm_setup.execution_command([root], ["pnpm", "run", "outer"]),
                cwd=root,
                env=dict(
                    os.environ,
                    PATH=str(wrong) + os.pathsep + os.environ.get("PATH", ""),
                ),
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pnpm mismatch", result.stderr)
            self.assertFalse((root / "ran").exists())
            self.assertEqual((root / "pnpm-lock.yaml").read_bytes(), lock)

    def test_mismatch_does_not_run_prepare_or_download_a_manager(self):
        with tempfile.TemporaryDirectory(prefix="pnpm identity ") as temporary:
            root = Path(temporary)
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "packageManager": "pnpm@0.0.0",
                        "scripts": {"prepare": "touch should-not-run"},
                    }
                )
            )
            with patch.dict(
                os.environ,
                npm_config_registry="http://127.0.0.1:1",
                PNPM_CONFIG_REGISTRY="http://127.0.0.1:1",
                PNPM_HOME=str(root / "manager-home"),
                XDG_DATA_HOME=str(root / "data"),
            ):
                with self.assertRaisesRegex(ValueError, "pnpm mismatch"):
                    pnpm_setup.run("install", root)
            self.assertFalse((root / "should-not-run").exists())
            self.assertFalse((root / "node_modules").exists())
            self.assertFalse((root / "manager-home").exists())
            self.assertFalse((root / "data").exists())

    def test_frozen_local_install_and_readiness(self):
        version = subprocess.check_output(
            ["pnpm", "--version"], text=True, cwd="/"
        ).strip()
        with tempfile.TemporaryDirectory(prefix="pnpm local readiness ") as temporary:
            root = Path(temporary)
            dependency = root / "dependency"
            dependency.mkdir()
            (dependency / "package.json").write_text(
                '{"name":"local-fixture","version":"1.0.0"}'
            )
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "packageManager": "pnpm@" + version,
                        "dependencies": {"local-fixture": "file:./dependency"},
                    }
                )
            )
            (root / "pnpm-workspace.yaml").write_text(
                "packages: []\npmOnFail: download\n"
            )
            options = (
                ["--pm-on-fail=ignore"]
                if int(version.split(".")[0]) >= 11
                else [
                    "--config.manage-package-manager-versions=false",
                    "--config.package-manager-strict-version=true",
                ]
            )
            subprocess.run(
                ["pnpm", *options, "install", "--offline", "--lockfile-only"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            lock = (root / "pnpm-lock.yaml").read_bytes()
            self.assertNotIn(b"packageManagerDependencies:", lock)
            # Local dependencies require no registry. A manager lookup/download
            # would fail, and the frozen lock cannot acquire manager metadata.
            with patch.dict(
                os.environ,
                npm_config_registry="http://127.0.0.1:1",
                PNPM_CONFIG_REGISTRY="http://127.0.0.1:1",
                PNPM_CONFIG_PM_ON_FAIL="error",
                PNPM_HOME=str(root / "manager-home"),
                XDG_DATA_HOME=str(root / "data"),
            ):
                self.assertEqual(pnpm_setup.run("install", root), 0)
                self.assertEqual(pnpm_setup.run("check", root), 0)
            self.assertEqual((root / "pnpm-lock.yaml").read_bytes(), lock)
            self.assertFalse((root / "manager-home/.tools").exists())
            self.assertFalse((root / "data/pnpm/.tools").exists())


if __name__ == "__main__":
    unittest.main()
