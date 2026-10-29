"""Release qualification keeps its complete matrix and scoped time allowance."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_intel_preparation_is_required_and_transferred_by_exact_id_and_digest(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())[
            "jobs"
        ]
        prepare = jobs["prepare-intel-runtime"]
        verify = jobs["verify"]
        self.assertEqual(verify["needs"], "prepare-intel-runtime")
        self.assertEqual(prepare["runs-on"], "macos-15-intel")
        self.assertEqual(prepare["timeout-minutes"], 360)
        self.assertEqual(prepare["if"], verify["if"])
        self.assertEqual(
            prepare["outputs"],
            {
                "artifact_id": "${{ steps.upload.outputs.artifact-id }}",
                "sha256": "${{ steps.export.outputs.sha256 }}",
                "zip_sha256": "${{ steps.upload.outputs.artifact-digest }}",
            },
        )
        for job in (prepare, verify):
            self.assertNotIn("continue-on-error", job)
            for step in job["steps"]:
                self.assertNotIn("continue-on-error", step)
        upload = next(s for s in prepare["steps"] if s.get("id") == "upload")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertNotIn("overwrite", upload["with"])
        download = next(
            s
            for s in verify["steps"]
            if s.get("name") == "Download and verify this run's prepared Intel runtime"
        )
        self.assertEqual(download["if"], "matrix.runner == 'macos-15-intel'")
        self.assertEqual(
            download["env"]["ARTIFACT_ID"],
            "${{ needs.prepare-intel-runtime.outputs.artifact_id }}",
        )
        self.assertEqual(
            download["env"]["ZIP_SHA256"],
            "${{ needs.prepare-intel-runtime.outputs.zip_sha256 }}",
        )
        self.assertEqual(
            download["env"]["ARCHIVE_SHA256"],
            "${{ needs.prepare-intel-runtime.outputs.sha256 }}",
        )
        self.assertEqual(download["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn("ci-runtime-download", download["run"])
        self.assertEqual(verify["permissions"], {"contents": "read", "actions": "read"})
        self.assertFalse(
            any(
                s.get("uses", "").startswith("actions/download-artifact@")
                for s in verify["steps"]
            )
        )
        imported = next(
            s
            for s in verify["steps"]
            if s.get("name") == "Verify and import the exact Intel runtime closure"
        )
        self.assertEqual(
            imported["env"]["ARCHIVE_SHA256"],
            "${{ needs.prepare-intel-runtime.outputs.sha256 }}",
        )
        names = [s.get("name") for s in verify["steps"]]
        self.assertLess(
            names.index(download["name"]),
            names.index(imported["name"]),
        )
        self.assertLess(
            names.index(imported["name"]),
            names.index("Qualify effective runtime trust stores"),
        )
        self.assertIn("Qualify initialization and standalone starter", names)
        self.assertIn("Qualify native service ownership and backend", names)

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
                360
                if identity == ("macos-15-intel", "host-nix", "core", "docker")
                else 60
            )
            with self.subTest(lane=identity):
                self.assertEqual(lane.get("timeout_minutes", 60), expected)
            if expected == 360:
                extended.append(identity)
        self.assertEqual(len(extended), 1)

    def test_publication_and_readback_still_require_qualification(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        jobs = workflow["jobs"]
        self.assertEqual(jobs["qualify"]["uses"], "./.github/workflows/verify.yml")
        self.assertEqual(
            jobs["qualify"]["permissions"], {"contents": "read", "actions": "read"}
        )
        self.assertEqual(jobs["publish"]["needs"], "qualify")
        self.assertEqual(jobs["readback"]["needs"], "publish")
        for name in ("publish", "readback"):
            self.assertNotIn("if", jobs[name])
            self.assertNotIn("continue-on-error", jobs[name])

    def test_native_runtime_transfer_is_required_in_linux_host_core_lane(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        job = workflow["jobs"]["verify"]
        step = next(
            s
            for s in job["steps"]
            if s.get("name") == "Qualify runtime closure transfer"
        )
        self.assertEqual(
            step["if"],
            "matrix.mode == 'host-nix' && matrix.module == 'core' && matrix.runner == 'ubuntu-24.04'",
        )
        self.assertEqual(
            step["run"],
            "nix --extra-experimental-features 'nix-command flakes' run path:./nix#just -- ci-runtime-test",
        )
        self.assertNotIn("continue-on-error", job)
        self.assertNotIn("continue-on-error", step)


if __name__ == "__main__":
    unittest.main()
