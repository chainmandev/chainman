"""Release qualification retains every required lane and isolates diagnostics."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_required_matrix_uses_supported_runners_and_uniform_timeout(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        job = workflow["jobs"]["verify"]
        self.assertEqual(job["timeout-minutes"], 60)
        lanes = job["strategy"]["matrix"]["include"]
        expected = {
            ("ubuntu-24.04", "host-nix", "core", "docker"),
            ("ubuntu-24.04-arm", "host-nix", "core", "docker"),
            ("macos-15", "host-nix", "core", "docker"),
            ("ubuntu-24.04", "container-nix", "core", "docker"),
            ("ubuntu-24.04", "container-nix", "core", "podman"),
            ("macos-15", "host-nix", "swift", "docker"),
        } | {
            ("ubuntu-24.04", "host-nix", module, "docker")
            for module in (
                "javascript",
                "rust",
                "python",
                "go",
                "flutter",
                "swift",
                "compose",
            )
        }
        self.assertEqual(len(lanes), len(expected))
        self.assertEqual(
            {
                (lane["runner"], lane["mode"], lane["module"], lane["engine"])
                for lane in lanes
            },
            expected,
        )
        self.assertTrue(all("timeout_minutes" not in lane for lane in lanes))

    def test_cancellation_diagnostic_keeps_forensics_opt_in_on_apple_silicon(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertFalse(inputs["diagnose_stopped_cancellation"]["default"])
        jobs = workflow["jobs"]
        diagnostic = jobs["diagnose-stopped-cancellation"]
        self.assertEqual(diagnostic["if"], "inputs.diagnose_stopped_cancellation")
        self.assertEqual(diagnostic["runs-on"], "macos-15")
        self.assertEqual(diagnostic["timeout-minutes"], 30)
        self.assertNotIn("continue-on-error", diagnostic)
        for flag in (
            "CHAINMAN_TEST_STOPPED_TASK_STACKS",
            "CHAINMAN_TEST_TERMINAL_STACKS",
            "CHAINMAN_TEST_TASK_SIGNAL_TRACE",
        ):
            with self.subTest(flag=flag):
                self.assertEqual(diagnostic["env"][flag], "1")
                self.assertNotIn(flag, jobs["verify"]["env"])

    def test_publication_and_readback_still_require_qualification(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        jobs = workflow["jobs"]
        self.assertEqual(jobs["qualify"]["uses"], "./.github/workflows/verify.yml")
        self.assertEqual(jobs["publish"]["needs"], "qualify")
        self.assertEqual(jobs["readback"]["needs"], "publish")
        for name in ("publish", "readback"):
            self.assertNotIn("if", jobs[name])
            self.assertNotIn("continue-on-error", jobs[name])


if __name__ == "__main__":
    unittest.main()
