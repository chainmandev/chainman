"""Built-in examples obey the same adapter policies and final audit boundary."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import module_updates as modules


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="module adapter spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_native_commands_and_coordinated_pins_retain_module_policy(self):
        module = {
            "name": "compose",
            "profile": "compose",
            "directory": "examples/compose",
            "ecosystem": "maven",
            "maven_repositories": ["central", "plugins"],
            "commands": {"resolve": [["gradle", "dependencies", "--write-locks"]]},
        }
        pin = {
            "module": "compose",
            "provider": "maven",
            "name": "example:plugin",
            "file": "versions.toml",
            "pointer": ["version"],
        }
        with patch.object(modules.tc, "module", return_value=module):
            result = modules.adapters(self.root, ["compose"], {"pins": [pin]})[
                "compose"
            ]
        self.assertEqual(result["adapter"], "gradle")
        self.assertEqual(result["pins"], [pin])
        self.assertEqual(result["resolve"], module["commands"]["resolve"])
        self.assertEqual(result["maven_repositories"], ["central", "plugins"])

    def test_unclassified_global_pin_is_rejected_before_any_resolution(self):
        with self.assertRaisesRegex(ValueError, "explicitly configured"):
            modules.adapters(
                self.root, [], {"pins": [{"provider": "npm", "name": "sample"}]}
            )

    def test_all_original_snapshots_precede_sdk_edits_and_final_audits_follow_resolvers(
        self,
    ):
        events = []

        class Engine:
            @staticmethod
            def snapshot(root, spec):
                events.append(("snapshot", spec["adapter"]))
                return {"old": spec["adapter"]}

            @staticmethod
            def resolve(root, spec, policy, now):
                events.append(("resolve", spec["adapter"]))
                return {"selected": spec["adapter"]}

            @staticmethod
            def audit(root, spec, before, policy, now):
                self.assertEqual(
                    before,
                    {
                        "old": spec["adapter"],
                        "resolution": {"selected": spec["adapter"]},
                    },
                )
                events.append(("audit", spec["adapter"]))

        with (
            patch.object(
                modules,
                "adapters",
                return_value={
                    "one": {"adapter": "javascript"},
                    "two": {"adapter": "rust"},
                },
            ),
            patch.object(modules.dependency_api, "implementation", return_value=Engine),
            patch.object(
                modules.sdk_versions,
                "synchronize",
                side_effect=lambda *a, **kw: events.append(
                    ("sdk", kw.get("check", False))
                ),
            ),
        ):
            modules.resolve(
                self.root,
                [],
                {"docker": {"enabled": False}},
                datetime(2026, 9, 7, tzinfo=timezone.utc),
            )
        self.assertEqual(
            events,
            [
                ("snapshot", "javascript"),
                ("snapshot", "rust"),
                ("sdk", False),
                ("resolve", "javascript"),
                ("resolve", "rust"),
                ("audit", "javascript"),
                ("audit", "rust"),
                ("sdk", True),
            ],
        )

    def test_runtime_image_requires_identical_pinned_bootstrap_bytes(self):
        (self.root / "nix").mkdir()
        (self.root / "bootstrap").mkdir()
        image = "docker.io/nixos/nix:2.0.0@sha256:" + "a" * 64
        (self.root / "nix/container-image.txt").write_text(image + "\n")
        bootstrap = self.root / "bootstrap/chainman.sh"
        bootstrap.write_text("image=" + image + "\n")
        self.assertEqual(modules.image_snapshot(self.root, {})["tag"], "2.0.0")
        bootstrap.write_text("image=docker.io/nixos/nix:latest\n")
        with self.assertRaisesRegex(ValueError, "disagree"):
            modules.image_snapshot(self.root, {})


if __name__ == "__main__":
    unittest.main()
