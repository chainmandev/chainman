"""Optional provider dispatch and dependency repair use separate runtimes."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import attestation
import chainman
import helper_sources
import module_updates
import toolchain


class AttestationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="attestation delivery ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.policy = {
            "provider": "github-actions",
            "repository": "sample/project",
            "workflow": ".github/workflows/qualification.yml",
            "ref": "refs/heads/main",
            "required_jobs": ["Qualification", "Sign release bundles"],
            "artifact_name": "qualified-release-{runId}-{runAttempt}",
            "tag": "next",
        }
        (self.root / "manifest.json").write_text("{}\n")

    def test_policy_is_explicit_provider_aware_and_strict(self):
        for cfg, message in (
            ({}, "explicit"),
            ({"attest": {**self.policy, "provider": "other-ci"}}, "Unsupported"),
            (
                {"attest": {**self.policy, "registry": "https://example.invalid"}},
                "Unknown",
            ),
            ({"attest": {**self.policy, "required_jobs": []}}, "distinct"),
        ):
            with self.subTest(cfg=cfg), self.assertRaisesRegex(ValueError, message):
                attestation.policy(cfg)
        value = attestation.policy({"attest": self.policy})
        self.assertEqual(value["provider"], "github-actions")
        self.assertEqual(value["requiredJobs"], self.policy["required_jobs"])
        self.assertEqual(value["artifactName"], self.policy["artifact_name"])
        self.assertNotIn("artifact_name", value)
        generic = dict(self.policy)
        generic.pop("tag")
        self.assertNotIn("tag", attestation.policy({"attest": generic}))

    def test_each_public_command_enters_only_optional_runtime_and_cleans_policy(self):
        transported = []

        def execute(root, profile, argv, **kwargs):
            self.assertEqual(root, self.root)
            self.assertEqual(profile, "attest")
            self.assertEqual(argv[0], sys.executable)
            self.assertIn("CHAINMAN_ATTEST_BIN", argv[2])
            self.assertNotIn("CHAINMAN_ACTIVE_PROFILE", kwargs["env"])
            self.assertIn(str(self.root / "manifest.json"), argv)
            self.assertEqual(argv[-2], "--policy")
            path = Path(argv[-1])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["provider"], "github-actions")
            transported.append(path)
            return subprocess.CompletedProcess(argv, 7)

        with (
            patch.object(
                chainman, "configuration", return_value={"attest": self.policy}
            ),
            patch.object(chainman, "execute", side_effect=execute) as launch,
            patch.object(attestation.tc, "environment", return_value={}),
        ):
            for action, argv in (
                ("attest", ["create", "manifest.json"]),
                ("attest", ["create", "manifest.json", "--run", "123"]),
                ("attest", ["verify", "manifest.json", "--run", "123"]),
                ("npm-publish", ["manifest.json", "--run", "123"]),
            ):
                self.assertEqual(attestation.run(self.root, action, argv), 7)
            self.assertEqual(launch.call_count, 4)
        self.assertTrue(all(not path.exists() for path in transported))

    def test_consumer_profile_cannot_replace_private_helper(self):
        with (
            patch.object(
                chainman,
                "configuration",
                return_value={
                    "attest": self.policy,
                    "profiles": {"attest": {"runtime_profile": "javascript"}},
                },
            ),
            patch.object(chainman, "execute") as launch,
            self.assertRaisesRegex(ValueError, "reserved"),
        ):
            attestation.run(self.root, "attest", ["create", "manifest.json"])
        launch.assert_not_called()

    def test_npm_publication_requires_explicit_npm_tag_policy(self):
        generic = dict(self.policy)
        generic.pop("tag")
        with (
            patch.object(chainman, "configuration", return_value={"attest": generic}),
            patch.object(chainman, "execute") as launch,
            self.assertRaisesRegex(ValueError, "explicit.*tag"),
        ):
            attestation.run(self.root, "npm-publish", ["manifest.json", "--run", "123"])
        launch.assert_not_called()

    def test_bad_arguments_or_manifest_cannot_enter_runtime(self):
        invalid = (
            ("attest", ["verify", "manifest.json"]),
            ("attest", ["create", "manifest.json", "--run", "latest"]),
            ("npm-publish", ["manifest.json", "--run", "latest"]),
            ("npm-publish", ["manifest.json", "--run", "1", "--tag", "latest"]),
            ("attest", ["create", "../manifest.json"]),
        )
        with (
            patch.object(
                chainman, "configuration", return_value={"attest": self.policy}
            ),
            patch.object(chainman, "execute") as launch,
        ):
            for action, argv in invalid:
                with self.subTest(argv=argv), self.assertRaises(ValueError):
                    attestation.run(self.root, action, argv)
            launch.assert_not_called()
        (self.root / "link.json").symlink_to(self.root / "manifest.json")
        with self.assertRaisesRegex(ValueError, "symlink"):
            attestation.tc.regular_input(self.root, "link.json")

    def test_helper_source_registration_does_not_enable_ordinary_modules(self):
        selected = ["core"]
        policy = {"source_modules": ["attest", "attest"]}
        specs = {
            "core": {"name": "core", "directory": ".", "profile": "core"},
            "attest": {
                "name": "attest",
                "directory": "nix/attest",
                "profile": "javascript",
                "ecosystem": "npm",
            },
        }
        with patch.object(
            module_updates.tc, "module", side_effect=lambda name, root: specs[name]
        ) as load:
            result = module_updates.adapters(self.root, selected, policy)
        self.assertEqual(list(result), ["attest"])
        self.assertEqual(result["attest"]["adapter"], "javascript")
        self.assertEqual(result["attest"]["profile"], "javascript")
        self.assertEqual(load.call_count, 2)
        self.assertEqual(selected, ["core"])

    def test_hash_repair_uses_updater_shell_when_helper_hash_is_stale(self):
        for relative in (
            "nix/attest/package.json",
            "nix/attest/pnpm-lock.yaml",
            "nix/attest.nix",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale hash is deliberately not evaluated\n")
        with (
            patch.object(
                helper_sources.tc, "entry_command", return_value=["entry", "updates"]
            ) as entry,
            patch.object(helper_sources.tc, "environment", return_value={}),
            patch.object(helper_sources.tc, "managed_run") as run,
        ):
            helper_sources.reconcile(self.root, ["core"])
            run.assert_not_called()
            helper_sources.reconcile(self.root, ["core", "attest"])
        entry.assert_called_once_with(self.root, "updates")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["entry", "updates"])
        self.assertEqual(
            argv[-6:],
            [
                "nix-update",
                "--flake",
                "--version=skip",
                "--override-filename",
                "attest.nix",
                "attest",
            ],
        )
        self.assertNotIn("chainman-attest", argv)

    def test_optional_generated_helper_outputs_do_not_change_core_setup_identity(self):
        helper = self.root / "nix/attest"
        helper.mkdir(parents=True)
        source = helper / "package.json"
        source.write_text('{"private":true}\n')
        before = toolchain.fingerprint({"name": "core"}, self.root)
        (helper / "dist").mkdir()
        (helper / "dist/cli.js").write_text("generated\n")
        (helper / "node_modules").mkdir()
        (helper / "node_modules/indirect").symlink_to(source)
        self.assertEqual(toolchain.fingerprint({"name": "core"}, self.root), before)
        source.write_text('{"private":true,"version":"0.1.0"}\n')
        self.assertNotEqual(toolchain.fingerprint({"name": "core"}, self.root), before)


if __name__ == "__main__":
    unittest.main()
