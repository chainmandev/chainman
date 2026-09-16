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
    def test_patch_timestamp_repair_through_setup(self):
        import io
        import tarfile
        import workflows

        with tempfile.TemporaryDirectory(
            prefix="chainman patch readiness "
        ) as directory:
            root = Path(directory)
            with tarfile.open(root / "fixture.tgz", "w:gz") as archive:
                for name, data in {
                    "package/package.json": b'{"name":"fixture-dependency","version":"1.0.0"}',
                    "package/index.js": b"module.exports = 1;\n",
                }.items():
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    archive.addfile(entry, io.BytesIO(data))
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "fixture",
                        "private": True,
                        "dependencies": {"fixture-dependency": "file:./fixture.tgz"},
                    }
                )
            )
            (root / "pnpm-workspace.yaml").write_text(
                "packages: []\npatchedDependencies:\n  fixture-dependency@1.0.0: fixture.patch\n"
            )
            patchfile = root / "fixture.patch"
            patchfile.write_text(
                "diff --git a/index.js b/index.js\n--- a/index.js\n+++ b/index.js\n@@ -1 +1 @@\n-module.exports = 1;\n+module.exports = 2;\n"
            )
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[setup.javascript]
inputs=["package.json","pnpm-lock.yaml","pnpm-workspace.yaml","fixture.patch"]
artifacts=["node_modules/.modules.yaml"]
commands=[["pnpm","install","--offline","--frozen-lockfile","--config.confirmModulesPurge=false"]]
readiness={command=["pnpm","exec","node","-e",""],timeout_seconds=30}
[tasks.check]
setup=["javascript"]
commands=[["node","-e","require('fs').writeFileSync('ran','yes')"]]
""")
            with patch.dict(
                os.environ,
                {
                    "TOOLCHAIN_DOWNLOAD_CACHE": str(root / "downloads"),
                    "CHAINMAN_SETUP": "error",
                },
            ):
                env = toolchain.environment(root)
                subprocess.run(
                    ["pnpm", "install", "--offline"],
                    cwd=root,
                    env=env,
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(workflows.run(root, "setup", []), 0)
                # Git checkout can replace identical patch bytes with a new mtime.
                import time

                time.sleep(0.02)
                patchfile.write_bytes(patchfile.read_bytes())
                # pnpm 10 first checks whether manifests have newer timestamps.
                manifest = root / "package.json"
                manifest.write_bytes(manifest.read_bytes())
                with self.assertRaisesRegex(ValueError, "VERIFY_DEPS_BEFORE_RUN"):
                    workflows.run(root, "check", [])
                self.assertFalse((root / "ran").exists())
                self.assertEqual(workflows.run(root, "setup", []), 0)
                self.assertEqual(workflows.run(root, "check", []), 0)
                self.assertEqual(workflows.setup_status(root, []), 0)
                (root / "node_modules/.pnpm-workspace-state-v1.json").unlink()
                # pnpm may safely reconstruct metadata; either validation succeeds
                # or setup repairs it, without running the task during inspection.
                status = workflows.setup_status(root, [])
                if status:
                    self.assertEqual(workflows.run(root, "setup", []), 0)
                self.assertEqual(workflows.run(root, "check", []), 0)
                patchfile.write_text(
                    patchfile.read_text().replace("exports = 2", "exports = 3")
                )
                with self.assertRaisesRegex(ValueError, "inputs-changed"):
                    workflows.run(root, "check", [])
                with self.assertRaises(subprocess.CalledProcessError):
                    workflows.run(root, "setup", [])
                self.assertFalse(
                    (root / ".cache/toolchain/setup-groups/javascript.json").exists()
                )

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
