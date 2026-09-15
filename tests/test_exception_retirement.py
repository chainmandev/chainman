"""Temporary policy is removed only with complete, verified dependency evidence."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_api as api
import exception_retirement as subject
import registry
from dependency_identity import Identity
import updates

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


class RetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="exception retirement ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="default"
[updates]
policy_file="policy.toml"
verify_task="verify"
outputs=["Cargo.lock"]
[updates.adapters.rust]
adapter="rust"
directory="."
[[updates.steps]]
resolve="rust"
[tasks.verify]
commands=[["true"]]
""")
        (self.root / "Cargo.toml").write_text("""[package]
name="fixture"
version="0.1.0"
edition="2021"
[dependencies]
demo="1.0"
""")
        (self.root / "src").mkdir()
        (self.root / "src/lib.rs").write_text("")
        self.policy_path = self.root / "policy.toml"
        self.policy_path.write_text("""# Project policy stays readable.
minimum_age_days = 30

[[exceptions]]
package = "crates:demo"
version = "1.2.3"
minimum_safe = "1.2.3"
reason = "Permit the exact fix while it matures."
advisory = "https://example.invalid/advisory"
expires = "2026-09-16T00:00:00Z"

[constraints."crates:demo"]
range = ">=1.0.0 <2.0.0"
reason = "Keep the supported API." # This comment must survive.
""")
        self.policy_path.chmod(0o640)
        self.write_lock("1.2.3")
        self.releases = [self.release("1.2.2", 90), self.release("1.2.3", 30)]
        fetched = patch.object(
            registry, "releases", side_effect=lambda *a, **k: self.releases
        )
        fetched.start()
        self.addCleanup(fetched.stop)
        self.settings = api.policy(self.root)
        self.originals = subject.documents(self.root)
        _, _, self.audited = api.plan_steps(self.root, self.settings, [])
        self.baselines = {
            name: dict(api.implementation(spec).snapshot(self.root, spec))
            for name, (spec, _) in self.audited.items()
        }

    @staticmethod
    def release(version, days):
        at = NOW - timedelta(days=days)
        artifact = registry.Artifact(
            "https://example.invalid/" + version, "sha256:" + "a" * 64, at
        )
        return registry.Release(version, at, artifacts=(artifact,))

    def write_lock(self, *versions):
        (self.root / "Cargo.lock").write_text(
            "version = 4\n"
            + "".join(
                '''
[[package]]
name = "demo"
version = "'''
                + version
                + '''"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "'''
                + "a" * 64
                + """"
"""
                for version in versions
            )
        )

    def retire(self, now=NOW):
        for name, (spec, policy) in self.audited.items():
            api.implementation(spec).audit(
                self.root, spec, self.baselines[name], policy, now
            )
        return subject.retire(
            self.root, self.originals, self.settings, self.audited, self.baselines, now
        )

    def test_exact_boundary_removes_whole_entry_preserving_policy_and_mode(self):
        lock = (self.root / "Cargo.lock").read_bytes()
        self.assertEqual(self.retire(), ["crates:demo@1.2.3"])
        current = self.policy_path.read_text()
        self.assertNotIn("minimum_safe", current)
        self.assertNotIn("example.invalid/advisory", current)
        self.assertIn("# This comment must survive.", current)
        self.assertIn("# Project policy stays readable.", current)
        self.assertEqual(tomllib.loads(current)["exceptions"], [])
        self.assertEqual(stat.S_IMODE(self.policy_path.stat().st_mode), 0o640)
        self.assertEqual((self.root / "Cargo.lock").read_bytes(), lock)
        # The original settings continue to protect this whole transaction.
        self.assertEqual(
            registry.minimum_safe("crates", self.settings, "demo"),
            registry.version("crates", "1.2.3"),
        )
        self.assertIsNone(
            registry.minimum_safe("crates", api.policy(self.root), "demo")
        )

    def test_young_unchanged_lock_is_not_retirement_evidence(self):
        self.releases[-1] = self.release("1.2.3", 29)
        old = self.policy_path.read_bytes()
        self.assertEqual(self.retire(), [])
        self.assertEqual(self.policy_path.read_bytes(), old)

    def test_cleanup_preserves_candidate_comments_and_other_formatting(self):
        self.policy_path.write_text(
            self.policy_path.read_text() + "\n# Reconciled project comment.\n"
        )
        self.retire()
        self.assertIn("# Reconciled project comment.", self.policy_path.read_text())
        self.retire()  # A resumed cleanup must preserve it too.
        self.assertIn("# Reconciled project comment.", self.policy_path.read_text())

    def test_no_cleanup_does_not_rewrite_comments(self):
        self.releases[-1] = self.release("1.2.3", 1)
        self.policy_path.write_text(
            self.policy_path.read_text() + "\n# Candidate comment.\n"
        )
        old = self.policy_path.read_bytes()
        self.retire()
        self.assertEqual(self.policy_path.read_bytes(), old)

    def test_mature_upstream_alternative_does_not_excuse_young_actual_version(self):
        self.releases.append(self.release("1.2.4", 1))
        self.write_lock("1.2.4")
        self.baselines["rust"] = dict(
            api.implementation(self.audited["rust"][0]).snapshot(
                self.root, self.audited["rust"][0]
            )
        )
        self.assertEqual(self.retire(), [])

    def test_every_locked_version_and_artifact_must_be_mature(self):
        self.releases.append(self.release("1.2.4", 1))
        self.write_lock("1.2.3", "1.2.4")
        self.baselines["rust"] = dict(
            api.implementation(self.audited["rust"][0]).snapshot(
                self.root, self.audited["rust"][0]
            )
        )
        self.assertEqual(self.retire(), [])
        # A newer artifact timestamp cannot hide behind an older release date.
        self.write_lock("1.2.3")
        self.releases[-2] = registry.Release(
            "1.2.3",
            NOW - timedelta(days=50),
            artifacts=self.release("1.2.3", 1).artifacts,
        )
        self.assertEqual(self.retire(), [])

    def test_expired_needed_exception_fails_without_deleting_it(self):
        self.releases[-1] = self.release("1.2.3", 1)
        original = self.policy_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.retire(NOW + timedelta(days=2))
        self.assertEqual(self.policy_path.read_bytes(), original)

    def test_dependency_removal_retires_expired_exception(self):
        manifest = self.root / "Cargo.toml"
        manifest.write_text(manifest.read_text().replace('demo="1.0"', ""))
        self.write_lock()
        self.assertEqual(self.retire(NOW + timedelta(days=2)), ["crates:demo@1.2.3"])

    def test_source_sdk_uses_bound_release_age_even_for_retained_tools(self):
        import source_toolchain

        spec = {"adapter": "toolchain"}
        evidence = {
            "provider": "crates",
            "name": "demo",
            "release": "1.2.3",
            "published": (NOW - timedelta(days=29)).isoformat(),
        }
        with patch.object(
            source_toolchain, "snapshot", return_value={"tools": [evidence]}
        ):
            self.assertFalse(
                subject.mature_scope(
                    self.root, spec, {}, self.settings, "crates", "demo", NOW
                )
            )
            evidence["published"] = (NOW - timedelta(days=30)).isoformat()
            self.assertTrue(
                subject.mature_scope(
                    self.root, spec, {}, self.settings, "crates", "demo", NOW
                )
            )
            evidence["release"] = "1.2.2"
            with self.assertRaisesRegex(ValueError, "safe floor"):
                subject.mature_scope(
                    self.root, spec, {}, self.settings, "crates", "demo", NOW
                )

    def test_actions_retained_without_bound_release_evidence_stay_in_policy(self):
        import source_updates

        item = {"repository": "owner/action", "selected": {"revision": "a" * 40}}
        with patch.object(source_updates, "action_plan", return_value=[item]):
            self.assertFalse(
                subject.mature_scope(
                    self.root,
                    {"adapter": "actions"},
                    {},
                    {},
                    "github",
                    "owner/action",
                    NOW,
                )
            )
            item["selected"].update(
                version="1.0.0", published=(NOW - timedelta(days=30)).isoformat()
            )
            self.assertTrue(
                subject.mature_scope(
                    self.root,
                    {"adapter": "actions"},
                    {},
                    {},
                    "github",
                    "owner/action",
                    NOW,
                )
            )

    def test_image_retirement_requires_the_actual_tag_digest_and_age(self):
        import source_updates

        image = {
            "repository": "owner/image",
            "tag": "1.0.0",
            "digest": "sha256:" + "b" * 64,
            "versionSource": "dockerHub",
        }
        released = registry.Release("1.0.0", NOW - timedelta(days=30), image["digest"])
        with (
            patch.object(
                source_updates, "oci_inventory", return_value={"image": image}
            ),
            patch.object(source_updates, "oci_candidates", return_value=[released]),
        ):
            self.assertTrue(
                subject.mature_scope(
                    self.root, {"adapter": "oci"}, {}, {}, "docker", "owner/image", NOW
                )
            )
            image["digest"] = "sha256:" + "c" * 64
            with self.assertRaisesRegex(ValueError, "image evidence"):
                subject.mature_scope(
                    self.root, {"adapter": "oci"}, {}, {}, "docker", "owner/image", NOW
                )

    def test_partial_provider_scope_retains_global_exception(self):
        self.settings["adapters"]["other"] = {"adapter": "rust", "directory": "other"}
        self.assertEqual(self.retire(), [])
        self.settings["adapters"].pop("other")
        self.settings["adapters"]["web"] = {"adapter": "javascript"}
        self.assertEqual(self.retire(), ["crates:demo@1.2.3"])

    def test_unselected_sdk_using_same_package_blocks_global_retirement(self):
        self.settings["adapters"]["sdk"] = {
            "adapter": "toolchain",
            "tools": [{"provider": "crates", "name": "demo"}],
        }
        self.assertEqual(self.retire(), [])

    def test_missing_or_corrupt_metadata_is_not_retirement_evidence(self):
        self.releases[-1] = registry.Release("1.2.3", NOW - timedelta(days=90))
        original = self.policy_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "identity|evidence"):
            self.retire()
        self.assertEqual(self.policy_path.read_bytes(), original)

    def test_retained_npm_deprecated_and_prerelease_artifacts_keep_evidence(self):
        policy = {"minimum_age_days": 30}
        for version, deprecated in (("1.2.3", True), ("1.2.4-beta.1", False)):
            with self.subTest(version=version):
                released = self.release(version, 40)
                retained = registry.Release(
                    version,
                    released.published,
                    artifacts=released.artifacts,
                    deprecated=deprecated,
                )
                artifact = retained.artifacts[0]
                identities = {
                    Identity("npm", "demo", version, artifact.url, artifact.digest)
                }

                def available(provider, package, **options):
                    return [self.release("1.3.0", 40)] + (
                        [retained]
                        if options.get("include_deprecated")
                        and options.get("include_prerelease")
                        else []
                    )

                with patch.object(registry, "releases", side_effect=available):
                    updates.audit_identities(
                        self.root, identities, identities, policy, NOW
                    )
                    self.assertTrue(
                        subject.mature_artifacts(self.root, identities, policy, NOW)
                    )
                    self.assertFalse(
                        subject.mature_artifacts(
                            self.root, identities, policy, NOW - timedelta(days=11)
                        )
                    )

    def test_unsafe_candidate_and_policy_edits_are_rejected(self):
        self.write_lock("1.2.2")
        with self.assertRaisesRegex(ValueError, "safe floor"):
            self.retire()
        self.write_lock("1.2.3")
        self.policy_path.write_text(
            self.policy_path.read_text().replace(
                "minimum_age_days = 30", "minimum_age_days = 0"
            )
        )
        edited = self.policy_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "policy changed"):
            self.retire()
        self.assertEqual(self.policy_path.read_bytes(), edited)

    def test_resume_rechecks_original_floor_after_cleanup(self):
        self.retire()
        pruned = self.policy_path.read_bytes()
        self.assertEqual(self.retire(), ["crates:demo@1.2.3"])
        self.assertEqual(self.policy_path.read_bytes(), pruned)
        self.write_lock("1.2.2")
        with self.assertRaisesRegex(ValueError, "safe floor"):
            self.retire()

    def test_resume_rejects_removed_exception_if_actual_artifact_is_still_young(self):
        self.retire()
        self.releases[-1] = self.release("1.2.3", 1)
        with self.assertRaisesRegex(ValueError, "policy changed"):
            self.retire()

    def test_scoped_exception_only_needs_its_own_adapter(self):
        body = self.policy_path.read_text().replace(
            "[[exceptions]]", "[[adapters.rust.policy.exceptions]]"
        )
        self.policy_path.write_text(body)
        self.settings = api.policy(self.root)
        self.settings["adapters"]["other"] = {"adapter": "rust"}
        _, _, self.audited = api.plan_steps(
            self.root, self.settings, ["--targets", "rust"]
        )
        self.originals = subject.documents(self.root)
        self.assertEqual(self.retire(), ["crates:demo@1.2.3"])
        self.assertEqual(self.retire(), ["crates:demo@1.2.3"])

    def test_inline_exceptions_and_inherited_empty_list(self):
        config = self.root / "chainman.toml"
        entry = self.settings["exceptions"][0]
        import tomlkit

        document = tomlkit.parse(config.read_text())
        document["updates"]["exceptions"] = [entry]
        config.write_text(tomlkit.dumps(document))
        self.originals = subject.documents(self.root)
        self.retire()
        self.retire()
        self.assertEqual(tomllib.loads(config.read_text())["updates"]["exceptions"], [])
        self.assertEqual(api.policy(self.root)["exceptions"], [])

    def test_resolution_failure_does_not_cleanup_policy(self):
        original = self.policy_path.read_bytes()
        engine = api.implementation(self.audited["rust"][0])
        with patch.object(
            api,
            "implementation",
            return_value=api.Adapter(
                engine.snapshot,
                lambda *a, **k: None,
                lambda *a: (_ for _ in ()).throw(
                    ValueError("failed verification audit")
                ),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "failed verification audit"):
                api.run_steps(self.root, self.settings, NOW, [])
        self.assertEqual(self.policy_path.read_bytes(), original)

    def test_hooks_cannot_authorize_their_own_cleanup(self):
        original = self.policy_path.read_bytes()
        self.settings["steps"].append({"commands": [["tamper"]]})
        engine = api.implementation(self.audited["rust"][0])

        def tamper(*a, **k):
            self.policy_path.write_text("minimum_age_days=0\nexceptions=[]\n")

        with (
            patch.object(
                api,
                "implementation",
                return_value=api.Adapter(
                    engine.snapshot, lambda *a, **k: None, engine.audit
                ),
            ),
            patch.object(api.chainman, "run_hook", side_effect=tamper),
            patch.object(api.tc, "environment", return_value={}),
        ):
            with self.assertRaisesRegex(ValueError, "policy changed"):
                api.run_steps(self.root, self.settings, NOW, [])
        self.assertNotEqual(
            self.policy_path.read_bytes(), original
        )  # Preserve the candidate for diagnosis.


if __name__ == "__main__":
    unittest.main()
