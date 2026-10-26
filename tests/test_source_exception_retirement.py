"""Source approvals require complete audited inventories and frozen authority."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_api as api
import exception_retirement as retirement
import source_artifact
import source_updates

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)
URL = "https://example.org/releases/1.2.3/archive.tar.gz"
DIGEST = "sha256:" + "a" * 64


class SourceRetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "chainman.toml").write_text(
            'schema=3\n[updates]\npolicy_file="policy.toml"\n'
        )
        self.entry = dict(
            url=URL,
            digest=DIGEST,
            reason="Reviewed fix",
            advisory="https://example.org/advisory",
            expires=(NOW + timedelta(days=1)).isoformat(),
        )
        self.path = self.root / "policy.toml"
        self.path.write_text(tomlkit.dumps({"artifact_exceptions": [self.entry]}))
        self.originals = retirement.documents(self.root)
        self.spec = {
            "adapter": "artifact",
            "entries": [{"file": "artifact.json", "pointer": []}],
        }
        (self.root / "artifact.json").write_text(
            '{"url":"' + URL + '","digest":"' + DIGEST + '"}'
        )
        self.policy = api.policy(self.root)
        self.settings = {**self.policy, "adapters": {"raw": self.spec}}

    def retire(self, audited=True):
        return retirement.retire(
            self.root,
            self.originals,
            self.settings,
            {"raw": (self.spec, self.policy)} if audited else {},
            {"raw": {}},
            NOW,
        )

    def test_query_only_or_unselected_scope_retains_approval(self):
        original = self.path.read_bytes()
        self.assertEqual(self.retire(False), [])
        self.settings["adapters"] = {}
        self.assertEqual(self.retire(), [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_exact_inventory_maturity_controls_retirement(self):
        with patch.object(
            source_artifact,
            "inspect",
            return_value={"published": (NOW - timedelta(days=1)).isoformat()},
        ) as inspect:
            self.assertEqual(self.retire(), [])
            inspect.assert_called_once()
        with patch.object(
            source_artifact,
            "inspect",
            return_value={"published": (NOW - timedelta(days=30)).isoformat()},
        ):
            self.assertEqual(len(self.retire()), 1)
        self.assertEqual(api.policy(self.root)["artifact_exceptions"], [])
        self.assertEqual(len(self.policy["artifact_exceptions"]), 1)
        # A resumed transaction must re-prove removal with the original policy.
        with (
            patch.object(
                source_artifact,
                "inspect",
                return_value={"published": (NOW - timedelta(days=1)).isoformat()},
            ),
            self.assertRaisesRegex(ValueError, "beyond proven"),
        ):
            self.retire()

    def test_unapproved_source_policy_edits_fail(self):
        self.path.write_text(
            tomlkit.dumps(
                {
                    "artifact_exceptions": [
                        {**self.entry, "digest": "sha256:" + "b" * 64}
                    ]
                }
            )
        )
        with (
            patch.object(
                source_artifact,
                "inspect",
                return_value={"published": (NOW - timedelta(days=30)).isoformat()},
            ),
            self.assertRaisesRegex(ValueError, "beyond proven"),
        ):
            self.retire()

    def test_nix_retirement_uses_selected_verified_commit_age(self):
        entry = {
            "directory": ".",
            "input": "pkgs",
            "repository": "example/packages",
            "revision": "a" * 40,
            **{key: self.entry[key] for key in ("reason", "advisory", "expires")},
        }
        for days, expected in ((1, False), (30, True)):
            with patch.object(
                source_updates,
                "nix_plan",
                return_value=[
                    {**entry, "published": (NOW - timedelta(days=days)).isoformat()}
                ],
            ):
                self.assertEqual(
                    retirement.mature_source_scope(
                        self.root, {}, {}, {}, "nix_exceptions", entry, NOW
                    ),
                    expected,
                )
        document = retirement.Document(
            "policy.toml", tomlkit.dumps({"nix_exceptions": [entry]}).encode(), ()
        )
        self.assertIn(("nix_exceptions",), retirement.lists(document))
        self.assertFalse(
            retirement.permitted(
                document,
                tomlkit.dumps(
                    {"nix_exceptions": [{**entry, "revision": "b" * 40}]}
                ).encode(),
                {},
            )
        )


if __name__ == "__main__":
    unittest.main()
