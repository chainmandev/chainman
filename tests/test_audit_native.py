"""Real pinned audit-tool startup and native policy discovery in disposable fixtures."""

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_audit
import toolchain as tc


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_AUDIT") == "1", "run just audit-test"
)
class NativeAuditTests(unittest.TestCase):
    def test_python_audit_tool_starts_without_project_or_user_python_packages(self):
        source = Path(__file__).resolve().parents[1]
        with tc.nix_temporary_directory("chainman-native-audit-") as temporary:
            tools = dependency_audit.tools_path(
                source, "python", gc_root=Path(temporary) / "tool"
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "NIX_PYTHONPATH",
                    "PIPAPI_PYTHON_LOCATION",
                }
            }
            env["PYTHONNOUSERSITE"] = "1"
            result = tc.managed_run(
                [str(tools / "pip-audit"), "--version"],
                cwd=source,
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("pip-audit", result.stdout)

    def test_rust_absolute_manifest_discovers_parent_policy_and_fails_closed(self):
        source = Path(__file__).resolve().parents[1]
        with tc.nix_temporary_directory("chainman-native-audit-") as temporary:
            base = Path(temporary)
            tools = dependency_audit.tools_path(source, "rust", gc_root=base / "tool")
            project = base / "project with spaces"
            workspace = project / "nested/server"
            workspace.mkdir(parents=True)
            manifest = workspace / "Cargo.toml"
            manifest.write_text("[workspace]\nmembers=[]\n")
            (project / "deny.toml").write_text("[deliberately malformed policy\n")
            result = tc.managed_run(
                [
                    str(tools / "cargo-deny"),
                    "--manifest-path",
                    str(manifest),
                    "check",
                    "advisories",
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("deny.toml", output)
            self.assertNotIn("unable to find a config path", output)


if __name__ == "__main__":
    unittest.main()
