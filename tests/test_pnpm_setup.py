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
            (root / "pnpm-workspace.yaml").write_text("pmOnFail: download\n")
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
