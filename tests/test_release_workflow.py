"""Release qualification keeps its complete matrix and scoped time allowance."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_only_intel_core_receives_extended_time(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        job = workflow["jobs"]["verify"]
        self.assertEqual(job["timeout-minutes"], "${{ matrix.timeout_minutes || 60 }}")
        lanes = job["strategy"]["matrix"]["include"]
        self.assertEqual(len(lanes), 14)
        extended = []
        for lane in lanes:
            identity = (lane["runner"], lane["mode"], lane["module"], lane["engine"])
            expected = (
                90
                if identity == ("macos-15-intel", "host-nix", "core", "docker")
                else 60
            )
            with self.subTest(lane=identity):
                self.assertEqual(lane.get("timeout_minutes", 60), expected)
            if expected == 90:
                extended.append(identity)
        self.assertEqual(len(extended), 1)

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
