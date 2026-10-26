"""Security policy scopes apply to actual versions and immutable identities."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import security_policy
import updates

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def exception(major, **changes):
    return dict(
        package="npm:demo",
        version=f"{major}.2.0",
        minimum_safe=f"{major}.2.0",
        applies_to=f">={major}.0.0 <{major + 1}.0.0",
        reason="Reviewed fix",
        advisory="https://example.invalid/advisory",
        expires=(NOW + timedelta(days=7)).isoformat(),
        **changes,
    )


def release(version, days):
    published = NOW - timedelta(days=days)
    return registry.Release(
        version,
        published,
        artifacts=(
            registry.Artifact(
                "https://example.invalid/" + version, "sha256:" + "a" * 64, published
            ),
        ),
    )


class ScopedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = {"exceptions": [exception(7), exception(8)]}

    def test_concurrent_lines_and_maturity_are_independent(self):
        releases = [
            release("7.1.0", 90),
            release("7.2.0", 40),
            release("8.1.0", 90),
            release("8.2.0", 1),
        ]
        security_policy.validate(self.policy)
        self.assertEqual(
            {
                r.version
                for r in registry.eligible("npm", releases, self.policy, "demo", NOW)
            },
            {"7.2.0", "8.2.0"},
        )
        self.assertEqual(
            [
                r.version
                for r in registry.active_exceptions(
                    "npm", releases, self.policy, "demo", NOW
                )
            ],
            ["8.2.0"],
        )
        self.policy["constraints"] = {
            "npm:demo": {"range": "<8", "reason": "Supported API"}
        }
        self.assertEqual(
            registry.select("npm", releases, self.policy, "demo", NOW).version, "7.2.0"
        )

    def test_overlapping_floors_choose_strictest_for_each_version(self):
        overlap = exception(7)
        overlap.update(version="7.3.0", minimum_safe="7.3.0")
        self.policy["exceptions"].append(overlap)
        self.assertFalse(registry.meets_safe_floor("npm", self.policy, "demo", "7.2.0"))
        self.assertTrue(registry.meets_safe_floor("npm", self.policy, "demo", "7.3.0"))
        self.assertTrue(registry.meets_safe_floor("npm", self.policy, "demo", "8.2.0"))

    def test_retained_unsafe_version_cannot_hide_behind_other_line(self):
        identity = ("npm", "demo", "7.1.0", "", "sha256:" + "a" * 64)
        with self.assertRaisesRegex(ValueError, "safe floor"):
            updates.audit_identities(
                Path("."), {identity}, {identity}, self.policy, NOW
            )

    def test_expired_line_cannot_be_disabled_by_other_mature_line(self):
        self.policy["exceptions"][1]["expires"] = NOW.isoformat()
        with self.assertRaisesRegex(ValueError, "Expired"):
            registry.eligible(
                "npm",
                [release("7.2.0", 40), release("8.2.0", 1)],
                self.policy,
                "demo",
                NOW,
            )

    def test_invalid_declarations_fail_even_for_unselected_packages(self):
        with self.assertRaisesRegex(ValueError, "Unknown dependency policy"):
            security_policy.validate({"minimum_ag_days": 30})
        for change in (
            {"unknown": True},
            {"applies_to": "garbage"},
            {"applies_to": ">=8"},
            {"advisory": "http://example.invalid"},
            {"expires": "tomorrow"},
            {"reason": "  "},
        ):
            entry = {**exception(7), **change}
            with self.subTest(change=change), self.assertRaises(ValueError):
                security_policy.validate({"exceptions": [entry]})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            security_policy.validate({"exceptions": [exception(7), exception(7)]})


if __name__ == "__main__":
    unittest.main()
