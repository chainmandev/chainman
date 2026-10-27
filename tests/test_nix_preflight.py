"""A shared Nix lock node has one authority across all selected adapters."""

import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_api as api
import source_updates as sources
import test_source_updates as fixture
import update_staging
import updates


class NixPreflightTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.NixTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.lock["nodes"]["root"]["inputs"]["alias"] = "pkgs"
        self.fixture.json("flake.lock", self.fixture.lock)
        self.settings = {
            "adapters": {
                name: {
                    "adapter": "nix",
                    "inputs": [{**self.fixture.spec["inputs"][0], "input": input}],
                    "policy": {"nix_exceptions": [self.fixture.approval(input=input)]},
                }
                for name, input in (("first", "pkgs"), ("second", "alias"))
            },
            "steps": [
                {"commands": [["before"]]},
                {"resolve": "first"},
                {"resolve": "second"},
            ],
        }

    def rejected(self, pattern):
        before = (self.root / "flake.lock").read_bytes()
        with (
            patch.object(sources, "resolve") as resolve,
            patch.object(api.chainman, "run_hook") as hook,
            self.assertRaisesRegex(ValueError, pattern),
        ):
            api.run_steps(self.root, self.settings, fixture.NOW, [])
        resolve.assert_not_called()
        hook.assert_not_called()
        self.assertEqual((self.root / "flake.lock").read_bytes(), before)

    def test_conflicting_approvals_fail_before_any_resolver_or_hook(self):
        self.settings["adapters"]["second"]["policy"]["nix_exceptions"][0][
            "revision"
        ] = fixture.C
        self.rejected("first:flake.lock:pkgs.*second:flake.lock:alias.*approvals")

    def test_approved_and_unapproved_aliases_conflict(self):
        self.settings["adapters"]["second"]["policy"]["nix_exceptions"] = []
        self.rejected("aliases.*approvals")

    def test_resumed_audit_checks_original_alias_approvals_before_auditing(self):
        self.settings["adapters"]["second"]["policy"]["nix_exceptions"][0][
            "revision"
        ] = fixture.C
        self.fixture.write(
            "chainman.toml", tomlkit.dumps({"schema": 3, "updates": self.settings})
        )
        updates.git(self.root, "init")
        updates.git(self.root, "config", "user.name", "Test")
        updates.git(self.root, "config", "user.email", "test@example.invalid")
        updates.git(self.root, "config", "commit.gpgsign", "false")
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-m", "Original conflicting source policy")
        with (
            patch.object(sources, "audit") as audit,
            self.assertRaisesRegex(ValueError, "aliases.*approvals"),
        ):
            update_staging.reaudit(
                self.root, fixture.NOW.isoformat(), ["--skip-chainman"]
            )
        audit.assert_not_called()

    def test_wrong_repository_fails_before_mutation(self):
        second = self.settings["adapters"]["second"]
        second["inputs"][0]["repository"] = "wrong/repository"
        second["policy"]["nix_exceptions"][0]["repository"] = "wrong/repository"
        self.rejected("declared source repository")

    def test_matching_approvals_and_explicit_selection_are_accepted(self):
        _, _, adapters = api.plan_steps(self.root, self.settings, [])
        api.preflight_nix(self.root, adapters)
        self.settings["adapters"]["second"]["policy"]["nix_exceptions"] = []
        _, _, adapters = api.plan_steps(
            self.root, self.settings, ["--targets", "first"]
        )
        self.assertEqual(set(adapters), {"first"})
        api.preflight_nix(self.root, adapters)

    def test_static_plan_does_not_require_source_inventory(self):
        (self.root / "flake.nix").unlink()
        (self.root / "flake.lock").unlink()
        with patch.object(sources, "nix_snapshot") as snapshot:
            _, _, adapters = api.plan_steps(self.root, self.settings, [])
        self.assertEqual(set(adapters), {"first", "second"})
        snapshot.assert_not_called()
        with self.assertRaises(FileNotFoundError):
            api.preflight_nix(self.root, adapters)

    def test_canonical_paths_cannot_hide_conflicting_approvals(self):
        second = self.settings["adapters"]["second"]
        second["inputs"][0]["directory"] = "./"
        second["policy"]["nix_exceptions"] = []
        self.rejected("aliases.*approvals")

    def test_distinct_lockfiles_can_have_distinct_approvals(self):
        self.fixture.json("nested/flake.lock", self.fixture.lock)
        self.fixture.write("nested/flake.nix", "{}\n")
        second = self.settings["adapters"]["second"]
        for entry in (second["inputs"][0], second["policy"]["nix_exceptions"][0]):
            entry["directory"] = "nested"
        second["policy"]["nix_exceptions"][0]["revision"] = fixture.C
        _, _, adapters = api.plan_steps(self.root, self.settings, [])
        api.preflight_nix(self.root, adapters)

    def test_different_branch_candidates_fail_before_hooks(self):
        for spec in self.settings["adapters"].values():
            spec["policy"]["nix_exceptions"] = []
        self.settings["adapters"]["second"]["inputs"][0]["branch"] = "other"

        def candidate(item, policy, now):
            return {
                "revision": fixture.B if item["branch"] == "stable" else fixture.C,
                "published": fixture.MATURE.isoformat(),
            }

        with patch.object(sources, "approved_nix_candidate", side_effect=candidate):
            self.rejected("first:flake.lock:pkgs.*second:flake.lock:alias.*revisions")

    def test_matching_selected_revisions_reach_resolution_and_final_audit(self):
        self.settings["steps"] = [{"resolve": name} for name in ("first", "second")]

        # Keep source validation real; only substitute native lock rewriting.
        def rewrite(*args, **kwargs):
            lock = copy.deepcopy(self.fixture.lock)
            lock["nodes"]["pkgs"]["locked"].update(
                rev=fixture.B, lastModified=int(fixture.MATURE.timestamp())
            )
            self.fixture.json("flake.lock", lock)

        with (
            patch.object(sources, "descendant", return_value=True),
            patch.object(sources.chainman, "execute", side_effect=rewrite),
            patch.object(sources.tc, "environment", return_value={}),
        ):
            api.run_steps(self.root, self.settings, fixture.NOW, [])


if __name__ == "__main__":
    unittest.main()
