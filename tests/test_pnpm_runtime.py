"""Actual pnpm must not reinstall dependencies while a task uses its setup."""

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
import toolchain


@unittest.skipUnless(shutil.which("pnpm"), "requires the JavaScript profile's pnpm")
class PnpmRuntimeTests(unittest.TestCase):
    def test_ci_transition_preserves_setup_and_stale_dependencies_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="chainman pnpm policy ") as directory:
            root = Path(directory).resolve()
            (root / "toolchain.toml").write_text('schema=1\nmodules=["core"]\n')
            dependency = root / "dependency"
            dependency.mkdir()
            (dependency / "package.json").write_text(
                '{"name":"local-dependency","version":"1.0.0"}'
            )
            package = {
                "name": "runtime-policy-fixture",
                "private": True,
                "version": "1.0.0",
                "dependencies": {"local-dependency": "file:dependency"},
                "scripts": {
                    "preinstall": "node install.cjs",
                    "check": "node check.cjs",
                },
            }
            manifest = root / "package.json"
            manifest.write_text(json.dumps(package))
            (root / "install.cjs").write_text(
                "const fs=require('fs'); fs.appendFileSync('installed', 'install\\n');\n"
            )
            (root / "check.cjs").write_text(
                "const fs=require('fs'); fs.appendFileSync('ran', 'run\\n');\n"
            )
            with patch.dict(
                os.environ, {"TOOLCHAIN_DOWNLOAD_CACHE": str(root / "downloads")}
            ):
                env = toolchain.environment(root)
            # pnpm also recognizes vendor markers such as GITHUB_ACTIONS.
            # Explicit false models local execution even when this fixture runs
            # on a CI host; the later true value exercises the actual transition.
            env["CI"] = "false"

            def run(*args, selected=env):
                return subprocess.run(
                    ["pnpm", *args],
                    cwd=root,
                    env=selected,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    capture_output=True,
                    timeout=60,
                )

            prior_layout = dict(env)
            toolchain.pnpm_environment(
                prior_layout, {"PNPM_CONFIG_ENABLE_GLOBAL_VIRTUAL_STORE": "true"}
            )
            installed = run("install", "--offline", selected=prior_layout)
            self.assertEqual(
                installed.returncode, 0, installed.stdout + installed.stderr
            )
            modules = root / "node_modules/.modules.yaml"
            self.assertTrue(
                modules.is_file(), "Migration requires installed dependencies"
            )
            prior_modules = modules.read_bytes()
            # Migrate an existing global layout through explicit installation,
            # with captured stdio and no terminal or blanket CI environment.
            refused = run("install", "--offline")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn(
                "ABORTED_REMOVE_MODULES_DIR_NO_TTY", refused.stdout + refused.stderr
            )
            installed = run(
                "install",
                "--offline",
                "--frozen-lockfile",
                "--config.confirmModulesPurge=false",
            )
            self.assertEqual(
                installed.returncode, 0, installed.stdout + installed.stderr
            )
            self.assertNotEqual(modules.read_bytes(), prior_modules)
            receipt = (root / "installed").read_bytes()
            ci = dict(env, CI="true")
            result = run("run", "check", selected=ci)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "installed").read_bytes(), receipt)
            self.assertEqual((root / "ran").read_text(), "run\n")

            dependency = root / "another-dependency"
            dependency.mkdir()
            (dependency / "package.json").write_text(
                '{"name":"another-dependency","version":"1.0.0"}'
            )
            package["dependencies"]["another-dependency"] = "file:another-dependency"
            manifest.write_text(json.dumps(package))
            stale = run("run", "check", selected=ci)
            self.assertNotEqual(stale.returncode, 0, stale.stdout + stale.stderr)
            self.assertEqual((root / "installed").read_bytes(), receipt)
            self.assertEqual((root / "ran").read_text(), "run\n")


if __name__ == "__main__":
    unittest.main()
