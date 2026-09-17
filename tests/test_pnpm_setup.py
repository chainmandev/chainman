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
            ):
                with self.assertRaisesRegex(ValueError, "pnpm mismatch"):
                    pnpm_setup.run("install", root)
            self.assertFalse((root / "should-not-run").exists())
            self.assertFalse((root / "node_modules").exists())

    def test_frozen_local_install_and_readiness(self):
        version = subprocess.check_output(
            ["pnpm", "--version"], text=True, cwd="/tmp"
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
            subprocess.run(
                ["pnpm", "install", "--offline", "--lockfile-only"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            lock = (root / "pnpm-lock.yaml").read_bytes()
            self.assertEqual(pnpm_setup.run("install", root), 0)
            self.assertEqual(pnpm_setup.run("check", root), 0)
            self.assertEqual((root / "pnpm-lock.yaml").read_bytes(), lock)


if __name__ == "__main__":
    unittest.main()
