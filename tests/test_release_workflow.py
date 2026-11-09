"""Release qualification keeps its complete matrix and scoped time allowance."""

from pathlib import Path
import tomllib
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

    def test_extended_time_is_limited_to_macos_and_container_core(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        job = workflow["jobs"]["verify"]
        self.assertEqual(job["timeout-minutes"], "${{ matrix.timeout_minutes || 60 }}")
        lanes = job["strategy"]["matrix"]["include"]
        self.assertEqual(len(lanes), 14)
        allowances = {
            ("macos-15-intel", "host-nix", "core", "docker"): 360,
            ("macos-15", "host-nix", "core", "docker"): 90,
            ("ubuntu-24.04", "container-nix", "core", "docker"): 90,
            ("ubuntu-24.04", "container-nix", "core", "podman"): 90,
        }
        identities = []
        extended = []
        for lane in lanes:
            identity = (lane["runner"], lane["mode"], lane["module"], lane["engine"])
            identities.append(identity)
            expected = allowances.get(identity, 60)
            with self.subTest(lane=identity):
                self.assertEqual(lane.get("timeout_minutes", 60), expected)
            if expected != 60:
                extended.append(identity)
        self.assertEqual(len(set(identities)), 14)
        self.assertCountEqual(extended, allowances)

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

    def test_optional_attestation_helper_is_required_in_the_javascript_lane(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        job = workflow["jobs"]["verify"]
        steps = [
            step
            for step in job["steps"]
            if step.get("name") == "Qualify optional attestation helper"
        ]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(set(step), {"name", "if", "run"})
        self.assertEqual(
            step["if"],
            "matrix.mode == 'host-nix' && matrix.module == 'javascript' && matrix.runner == 'ubuntu-24.04'",
        )
        self.assertEqual(
            step["run"],
            "nix --extra-experimental-features 'nix-command flakes' run path:./nix#just -- module attest verify",
        )
        self.assertEqual(job["strategy"]["max-parallel"], 1)
        module = tomllib.loads((ROOT / "modules/attest.toml").read_text())
        self.assertEqual(
            module["commands"]["verify"],
            [
                ["pnpm", "run", "check"],
                ["pnpm", "run", "build"],
                ["pnpm", "run", "test"],
                ["nix", "build", "path:..#attest", "--no-link"],
            ],
        )

    def test_javascript_lane_runs_native_pnpm_setup_regressions(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/verify.yml").read_text())
        step = next(
            step
            for step in workflow["jobs"]["verify"]["steps"]
            if step.get("name") == "Qualify native JavaScript update validation"
        )
        self.assertEqual(
            step["if"], "matrix.mode == 'host-nix' && matrix.module == 'javascript'"
        )
        self.assertEqual(
            step["run"],
            "nix --extra-experimental-features 'nix-command flakes' run path:./nix#just -- javascript-test",
        )
        recipe = (ROOT / "justfile").read_text().split("javascript-test:\n", 1)[1]
        recipe = recipe.split("\n\n", 1)[0]
        self.assertIn(
            "    @./scripts/enter.sh javascript python3 -B -m unittest discover -s tests -p test_pnpm_setup.py -v\n",
            recipe + "\n",
        )

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
