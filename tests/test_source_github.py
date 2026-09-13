"""Nonstandard public tags preserve publication, constraints, and commit identity."""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import source_github
import source_updates

PATTERN = r"^language-(?P<version>[0-9]+(?:\.[0-9]+){1,2})-RELEASE$"
NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)
OLD = datetime(2026, 6, 1, tzinfo=timezone.utc)
YOUNG = datetime(2026, 9, 1, tzinfo=timezone.utc)
A, B = "a" * 40, "b" * 40


class GitHubTagTests(unittest.TestCase):
    def setUp(self):
        rows = [
            {
                "tag_name": "language-6.2-RELEASE",
                "published_at": OLD.isoformat(),
                "draft": False,
                "prerelease": False,
            },
            {
                "tag_name": "language-6.3.1-RELEASE",
                "published_at": OLD.isoformat(),
                "draft": False,
                "prerelease": False,
            },
        ]
        self.data = patch.object(registry, "data", return_value=rows)
        self.data.start()
        self.addCleanup(self.data.stop)
        tags = patch.object(
            registry,
            "github_commit",
            side_effect=lambda _repo, tag: A if "6.2" in tag else B,
        )
        tags.start()
        self.addCleanup(tags.stop)
        dates = patch.object(
            source_updates,
            "commit_time",
            side_effect=lambda _repo, commit: OLD if commit == A else YOUNG,
        )
        dates.start()
        self.addCleanup(dates.stop)

    def test_two_components_normalize_without_changing_actual_tag(self):
        result = source_github.metadata("example/language", PATTERN, "6.2.0")
        self.assertEqual(result["tag"], "language-6.2-RELEASE")
        self.assertEqual(result["release"].identity, A)

    def test_old_release_tag_with_young_contents_does_not_win(self):
        result = source_github.select("example/language", PATTERN, {}, NOW)
        self.assertEqual(result["release"].version, "6.2.0")

    def test_malformed_inventory_entry_has_a_registry_diagnostic(self):
        with (
            patch.object(registry, "data", return_value=[None]),
            self.assertRaisesRegex(ValueError, "GitHub release must be an object"),
        ):
            source_github.releases("example/language", PATTERN)

    def test_constraints_do_not_admit_an_immature_tag(self):
        with self.assertRaisesRegex(ValueError, "No eligible"):
            source_github.select(
                "example/language",
                PATTERN,
                {
                    "constraints": {
                        "github:example/language": {
                            "range": ">=6.3.0",
                            "reason": "Declared version family",
                        }
                    }
                },
                NOW,
            )

    def test_unsafe_regex_and_missing_tag_fail(self):
        for value in (
            r"^(?P<version>(a+)+)$",
            r".*(?P<version>\d+\.\d+\.\d+).*",
            r"^prefix-(?P<other>\d+)$",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source_github.pattern(value)
        with self.assertRaisesRegex(ValueError, "missing"):
            source_github.metadata("example/language", PATTERN, "9.9.9")


if __name__ == "__main__":
    unittest.main()
