"""Node and its containing Nix revision mature independently in real transactions."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_api as api
import registry
import source_toolchain
import source_updates
import update_staging
import updates
import test_update_staging as staging_fixture

A, B, C, D = (letter * 40 for letter in "abcd")
HASH = "sha256-" + "A" * 43 + "="


class NodeNixRetirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = staging_fixture.StagingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.candidate = self.fixture.candidate
        shared = {
            "reason": "Reviewed exact fix while it matures",
            "advisory": "https://example.invalid/security/review",
            "expires": "2100-01-01T00:00:00Z",
        }
        policy = {
            "minimum_age_days": 30,
            "exceptions": [
                {
                    **shared,
                    "package": "github:nodejs/node",
                    "version": "24.2.3",
                    "minimum_safe": "24.2.3",
                    "applies_to": ">=24.0.0 <25.0.0",
                }
            ],
            "nix_exceptions": [
                {
                    **shared,
                    "directory": ".",
                    "input": "pkgs",
                    "repository": "example/packages",
                    "revision": B,
                }
            ],
        }
        (self.root / "policy.toml").write_text(tomlkit.dumps(policy))
        (self.root / "chainman.toml").write_text(
            tomlkit.dumps(
                {
                    "schema": 3,
                    "project": {"default_profile": "host"},
                    "updates": {
                        "policy_file": "policy.toml",
                        "verify_task": "verify",
                        "outputs": ["flake.lock", ".node-version"],
                        "adapters": {
                            "nix": {
                                "adapter": "nix",
                                "inputs": [
                                    {
                                        "directory": ".",
                                        "input": "pkgs",
                                        "repository": "example/packages",
                                        "branch": "stable",
                                    }
                                ],
                            },
                            "sdk": {
                                "adapter": "toolchain",
                                "tools": [
                                    {
                                        "provider": "github",
                                        "name": "nodejs/node",
                                        "command": ["node", "--version"],
                                        "version_pattern": r"v(?P<version>\d+\.\d+\.\d+)",
                                        "pins": [
                                            {
                                                "file": ".node-version",
                                                "format": "regex",
                                                "pattern": r"(?P<value>\d+\.\d+\.\d+)",
                                                "value": "{version}",
                                            }
                                        ],
                                    }
                                ],
                            },
                        },
                        "steps": [{"resolve": "nix"}, {"resolve": "sdk"}],
                    },
                    "tasks": {"verify": {"commands": [["true"]]}},
                }
            )
        )
        (self.root / "flake.lock").write_text(
            json.dumps(
                {
                    "version": 7,
                    "root": "root",
                    "nodes": {
                        "root": {"inputs": {"pkgs": "pkgs"}},
                        "pkgs": {
                            "locked": {
                                "type": "github",
                                "owner": "example",
                                "repo": "packages",
                                "rev": A,
                                "narHash": HASH,
                                "lastModified": 1,
                            },
                            "original": {
                                "type": "github",
                                "owner": "example",
                                "repo": "packages",
                                "ref": "stable",
                            },
                        },
                    },
                }
            )
        )
        (self.root / "flake.nix").write_text("{}\n")
        (self.root / ".node-version").write_text("24.2.2\n")

    def prepare(self, node_days=30, nix_days=29, expired=False):
        if expired:
            path = self.root / "policy.toml"
            path.write_text(path.read_text().replace("2100-01-01", "2000-01-01"))
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-m", "Declare paired SDK and source policy")
        self.before = updates.snapshot(self.root)
        self.fixture.prepare("--no-commit")
        self.at = (self.fixture.stage / "control/at").read_text().strip()
        self.now = datetime.fromisoformat(self.at)
        self.node_date = self.now - timedelta(days=node_days)
        self.nix_date = self.now - timedelta(days=nix_days)
        self.old_date = self.now - timedelta(days=100)
        self.node_override = None

        def probe(root, spec, tool):
            rev = json.loads((root / "flake.lock").read_text())["nodes"]["pkgs"][
                "locked"
            ]["rev"]
            return self.node_override or ("24.2.3" if rev == B else "24.2.2")

        def rewrite(root, *args, **kwargs):
            path = root / "flake.lock"
            lock = json.loads(path.read_text())
            lock["nodes"]["pkgs"]["locked"].update(
                rev=B, lastModified=int(self.nix_date.timestamp())
            )
            path.write_text(json.dumps(lock))

        patches = [
            patch.object(source_toolchain, "probe", side_effect=probe),
            patch.object(
                registry,
                "releases",
                side_effect=lambda *a, **k: [
                    registry.Release("24.2.2", self.old_date, D),
                    registry.Release("24.2.3", self.node_date, C),
                ],
            ),
            patch.object(
                registry,
                "github_commit",
                side_effect=lambda repo, version: C if version == "24.2.3" else D,
            ),
            patch.object(
                source_updates,
                "commit_time",
                side_effect=lambda repo, rev: {
                    A: self.old_date,
                    B: self.nix_date,
                    C: self.node_date,
                    D: self.old_date,
                }[rev],
            ),
            patch.object(
                source_updates,
                "nix_candidate",
                return_value={"revision": A, "published": self.old_date.isoformat()},
            ),
            patch.object(
                source_updates,
                "descendant",
                side_effect=lambda repo, base, head: head == "stable" or base == head,
            ),
            patch.object(source_updates, "nix_tree", return_value={"narHash": HASH}),
            patch.object(source_updates.chainman, "execute", side_effect=rewrite),
            patch.object(source_updates.tc, "environment", return_value={}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def resolve(self):
        api.run_steps(self.candidate, api.policy(self.candidate), self.now, [])

    def reaudit(self):
        update_staging.reaudit(self.candidate, self.at, ["--skip-chainman"])

    def test_node_retires_first_and_resume_preserves_young_nix_approval(self):
        self.prepare()
        self.resolve()
        policy = api.policy(self.candidate)
        self.assertEqual(policy["exceptions"], [])
        self.assertEqual(len(policy["nix_exceptions"]), 1)
        self.reaudit()
        update_staging.inspect(self.root, self.fixture.stage)
        update_staging.resume(self.root, self.fixture.stage)
        self.reaudit()
        update_staging.inspect(self.root, self.fixture.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)
        self.fixture.finish()
        self.assertEqual(api.policy(self.root)["exceptions"], [])
        self.assertEqual(len(api.policy(self.root)["nix_exceptions"]), 1)

    def test_both_mature_exceptions_retire_even_after_expiry(self):
        self.prepare(nix_days=30, expired=True)
        self.resolve()
        self.reaudit()
        for key in ("exceptions", "nix_exceptions"):
            self.assertEqual(api.policy(self.candidate)[key], [])
        update_staging.inspect(self.root, self.fixture.stage)
        self.fixture.finish()
        self.assertEqual(api.policy(self.root)["nix_exceptions"], [])

    def test_nix_maturity_does_not_retire_a_young_node_release(self):
        self.prepare(node_days=29, nix_days=30)
        self.resolve()
        self.reaudit()
        self.assertEqual(len(api.policy(self.candidate)["exceptions"]), 1)
        self.assertEqual(api.policy(self.candidate)["nix_exceptions"], [])

    def test_expired_needed_nix_approval_fails_without_cleanup(self):
        self.prepare(expired=True)
        policy = (self.candidate / "policy.toml").read_bytes()
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.resolve()
        self.assertEqual((self.candidate / "policy.toml").read_bytes(), policy)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_expired_needed_node_approval_fails_without_cleanup(self):
        self.prepare(node_days=29, nix_days=30, expired=True)
        policy = (self.candidate / "policy.toml").read_bytes()
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.resolve()
        self.assertEqual((self.candidate / "policy.toml").read_bytes(), policy)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_resumed_candidate_keeps_original_floor_after_cleanup(self):
        self.prepare(nix_days=30)
        self.resolve()
        update_staging.inspect(self.root, self.fixture.stage)
        update_staging.resume(self.root, self.fixture.stage)
        self.node_override = "24.2.2"
        with self.assertRaisesRegex(ValueError, "security policy"):
            self.reaudit()
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_resumed_candidate_cannot_drop_a_still_young_approval(self):
        self.prepare()
        self.resolve()
        path = self.candidate / "policy.toml"
        policy = tomlkit.parse(path.read_text())
        policy["nix_exceptions"] = []
        path.write_text(tomlkit.dumps(policy))
        with self.assertRaisesRegex(ValueError, "beyond proven"):
            self.reaudit()
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_failed_final_audit_keeps_original_policy(self):
        self.prepare(nix_days=30)
        self.resolve()
        lock = self.candidate / "flake.lock"
        value = json.loads(lock.read_text())
        value["nodes"]["pkgs"]["locked"]["narHash"] = "sha256-" + "B" * 43 + "="
        lock.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "content hash differs"):
            self.reaudit()
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_failed_project_gate_keeps_cleanup_inside_candidate(self):
        self.prepare(nix_days=30)
        head = updates.git(self.candidate, "rev-parse", "HEAD")

        def verify():
            self.assertEqual(api.policy(self.candidate)["exceptions"], [])
            self.assertEqual(api.policy(self.candidate)["nix_exceptions"], [])
            subprocess.run(["false"], cwd=self.candidate, check=True)

        with self.assertRaises(subprocess.CalledProcessError):
            updates.transaction(
                self.candidate,
                ["flake.lock", ".node-version", "policy.toml"],
                self.resolve,
                verify,
            )
        self.assertEqual(updates.git(self.candidate, "rev-parse", "HEAD"), head)
        self.assertEqual(updates.snapshot(self.root), self.before)


if __name__ == "__main__":
    unittest.main()
