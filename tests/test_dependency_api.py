"""Independent acceptance for shared update ordering and the public query boundary."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_api as api
import registry


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dependency queries ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "chainman.toml").write_text(
            "schema=1\n[updates]\nminimum_age_days=30\n"
        )

    def test_selects_new_major_only_after_maturity_window(self):
        inventory = [
            registry.Release("1.0.0", NOW - timedelta(days=90)),
            registry.Release("2.0.0", NOW - timedelta(days=31)),
            registry.Release("3.0.0", NOW - timedelta(days=29)),
        ]
        with patch.object(registry, "releases", return_value=inventory):
            result = api.query(
                self.root,
                {
                    "schema": 1,
                    "operation": "select",
                    "provider": "npm",
                    "package": "sample",
                    "current": "1.0.0",
                },
                now=NOW,
            )
        self.assertEqual(result["version"], "2.0.0")
        self.assertEqual(result["disposition"], "selected")

    def test_retention_is_explicit_and_does_not_claim_baseline_eligibility(self):
        with patch.object(
            registry,
            "releases",
            return_value=[registry.Release("1.0.0", NOW - timedelta(days=90))],
        ):
            result = api.query(
                self.root,
                {
                    "schema": 1,
                    "operation": "select",
                    "provider": "npm",
                    "package": "sample",
                    "current": "2.0.0",
                },
                now=NOW,
            )
        self.assertEqual(result["disposition"], "retained")
        self.assertEqual(result["version"], "2.0.0")
        self.assertEqual(result["eligible_candidate"]["version"], "1.0.0")
        self.assertNotIn("published", result)

    def test_missing_registry_evidence_is_a_failure(self):
        with patch.object(
            registry,
            "releases",
            side_effect=ValueError("Missing registry publication age"),
        ):
            with self.assertRaisesRegex(ValueError, "publication age"):
                api.query(
                    self.root,
                    {
                        "schema": 1,
                        "operation": "select",
                        "provider": "npm",
                        "package": "sample",
                    },
                    now=NOW,
                )

    def test_public_audit_does_not_accept_a_claimed_baseline_exemption(self):
        identity = [
            "npm",
            "sample",
            "1.0.0",
            "https://registry.npmjs.org/sample/-/sample-1.0.0.tgz",
            "sha256:" + "a" * 64,
        ]
        candidate = registry.Release(
            "1.0.0",
            NOW - timedelta(days=1),
            artifacts=(
                registry.Artifact(identity[3], identity[4], NOW - timedelta(days=1)),
            ),
        )
        with (
            patch.object(registry, "releases", return_value=[candidate]),
            self.assertRaisesRegex(ValueError, "not mature"),
        ):
            api.query(
                self.root,
                {
                    "schema": 1,
                    "operation": "audit",
                    "artifacts": [identity],
                    "before": [identity],
                },
                now=NOW,
            )

    def test_schema_and_path_validation_precede_work(self):
        for request in (
            {},
            {"schema": True},
            {"schema": 2},
            {"schema": 1, "operation": "unknown"},
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                api.query(self.root, request)
        with self.assertRaises(ValueError):
            api.configured(self.root, "../escape")

    def test_custom_resolve_requires_active_transaction(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValueError, "inside deps-update"),
        ):
            api.resolve_command(self.root, ["packages"])

    def test_metadata_is_exact_and_does_not_claim_maturity(self):
        with patch.object(
            registry,
            "releases",
            return_value=[registry.Release("2.0.0", NOW - timedelta(days=1))],
        ):
            request = {
                "schema": 1,
                "operation": "metadata",
                "provider": "npm",
                "package": "sample",
                "version": "2.0.0",
            }
            result = api.query(self.root, request, now=NOW)
            self.assertEqual(result["version"], "2.0.0")
            self.assertNotEqual(result.get("disposition"), "selected")
            with self.assertRaisesRegex(ValueError, "Exact release"):
                api.query(self.root, {**request, "version": "1.0.0"}, now=NOW)

    def test_query_constraint_is_never_a_fallback_hint(self):
        request = {
            "schema": 1,
            "operation": "select",
            "provider": "npm",
            "package": "sample",
            "mode": "compatible",
        }
        with patch.object(
            registry,
            "releases",
            return_value=[registry.Release("3.0.0", NOW - timedelta(days=90))],
        ):
            with self.assertRaisesRegex(ValueError, "actual constraint"):
                api.query(self.root, request, now=NOW)
            with self.assertRaisesRegex(ValueError, "No eligible"):
                api.query(
                    self.root,
                    {
                        **request,
                        "constraint": {"range": "^2", "reason": "Required API version"},
                    },
                    now=NOW,
                )

    def test_old_github_release_with_new_tag_contents_is_ineligible(self):
        import source_updates

        with (
            patch.object(
                registry,
                "releases",
                return_value=[registry.Release("1.0.0", NOW - timedelta(days=90))],
            ),
            patch.object(registry, "github_commit", return_value="a" * 40),
            patch.object(
                source_updates, "commit_time", return_value=NOW - timedelta(days=1)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "eligible|mature"):
                api.query(
                    self.root,
                    {
                        "schema": 1,
                        "operation": "select",
                        "provider": "github",
                        "package": "example/package",
                    },
                    now=NOW,
                )


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ordered dependencies ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "chainman.toml").write_text("schema=1\n")
        self.events = []
        self.settings = {
            "minimum_age_days": 30,
            "adapters": {"tools": {"adapter": "nix"}, "packages": {"adapter": "rust"}},
            "steps": [
                {"resolve": "tools"},
                {"resolve": "packages"},
                {"commands": [["generate"]], "targets": ["packages"]},
            ],
        }
        parent = self

        class Engine:
            @staticmethod
            def snapshot(root, spec):
                parent.events.append(("snapshot", spec["adapter"]))
                return {"old": spec["adapter"]}

            @staticmethod
            def resolve(root, spec, policy, now):
                parent.events.append(("resolve", spec["adapter"]))
                parent.assertEqual(api.instant(), NOW)

            @staticmethod
            def audit(root, spec, before, policy, now):
                parent.events.append(("audit", spec["adapter"]))
                parent.assertEqual(before, {"old": spec["adapter"]})

        self.engine = Engine

    def run_pipeline(self, extra=()):
        with (
            patch.object(api, "implementation", return_value=self.engine),
            patch.object(
                api.chainman,
                "run_hook",
                side_effect=lambda *a, **k: self.events.append(
                    ("generate", k["env"]["TOOLCHAIN_FRESH"])
                ),
            ),
            patch.object(api.tc, "environment", return_value={}),
        ):
            api.run_steps(self.root, self.settings, NOW, list(extra))

    def test_all_baselines_precede_mutation_and_all_audits_follow_generators(self):
        self.run_pipeline()
        self.assertEqual(
            self.events,
            [
                ("snapshot", "nix"),
                ("snapshot", "rust"),
                ("resolve", "nix"),
                ("resolve", "rust"),
                ("generate", "1"),
                ("audit", "nix"),
                ("audit", "rust"),
            ],
        )

    def test_target_selection_skips_unrelated_generators(self):
        self.run_pipeline(["--targets", "tools"])
        self.assertEqual(
            self.events, [("snapshot", "nix"), ("resolve", "nix"), ("audit", "nix")]
        )

    def test_named_project_hook_does_not_require_a_package_adapter(self):
        self.settings["targets"] = ["assets"]
        self.settings["steps"].append({"commands": [["assets"]], "targets": ["assets"]})
        self.run_pipeline(
            ["--targets", "assets", "--target-policy", "assets=compatible"]
        )
        self.assertEqual(self.events, [("generate", "1")])

    def test_declared_hook_requires_a_selected_step(self):
        self.settings["targets"] = ["assets"]
        with self.assertRaisesRegex(ValueError, "missing"):
            self.run_pipeline(["--targets", "assets"])
        self.assertEqual(self.events, [])

    def test_bad_order_duplicate_and_missing_steps_fail_before_mutation(self):
        for steps in (
            [{"resolve": "packages"}, {"resolve": "tools"}],
            [{"resolve": "tools"}, {"resolve": "tools"}, {"resolve": "packages"}],
            [{"resolve": "tools"}],
        ):
            self.settings["steps"] = steps
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                self.run_pipeline()
            self.assertEqual(self.events, [])

    def test_audit_failure_is_not_hidden(self):
        with (
            patch.object(
                self.engine,
                "audit",
                side_effect=ValueError("Generator replaced selected artifact"),
            ),
            self.assertRaisesRegex(ValueError, "replaced"),
        ):
            self.run_pipeline()

    def test_environment_restored_after_failure(self):
        with (
            patch.dict(os.environ, {"CHAINMAN_UPDATE_AT": "previous"}),
            self.assertRaises(RuntimeError),
        ):
            with api.transaction_environment(self.root, NOW):
                raise RuntimeError("interruption")
        self.assertNotEqual(os.environ.get("CHAINMAN_UPDATE_AT"), NOW.isoformat())

    def test_unknown_target_or_policy_fails(self):
        for extra in (
            ["--targets", "missing"],
            ["--targets", "tools", "--target-policy", "packages=compatible"],
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.run_pipeline(extra)


if __name__ == "__main__":
    unittest.main()
