"""Maven precedence and artifact identity have independent policy boundaries."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from maven_version import MavenVersion
import registry
import updates
import ecosystem_updates


class MavenVersionTests(unittest.TestCase):
    def test_native_selector_uses_maven_order_for_qualified_literal_pins(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        releases = [
            registry.Release(v, now - timedelta(days=40))
            for v in ("33.4.7-jre", "33.4.8-jre", "33.5.0-jre", "34.0.0-jre")
        ]
        pin = {"provider": "maven", "name": "com.example:library", "bound": ">=33,<34"}
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("ecosystem_updates.old_requirement", return_value="33.4.8-jre"),
            patch("cargo_sources.read", return_value={}),
            patch("swift_sources.read", return_value={}),
            patch.object(registry, "maven_releases", return_value=releases),
        ):
            chosen = ecosystem_updates.choose(
                Path(temporary), pin, {"mode": "compatible"}, {}, now
            )
            self.assertEqual(chosen.version, "33.5.0-jre")
            pin["bound"] = "<33.4.8"
            self.assertIsNone(
                ecosystem_updates.choose(
                    Path(temporary), pin, {"mode": "compatible"}, {}, now
                )
            )

    def test_maven_qualifier_and_nested_numeric_precedence(self):
        ordered = [
            "1.0-alpha1",
            "1.0-alpha2",
            "1.0-beta1",
            "1.0-m1",
            "1.0-rc1",
            "1.0-SNAPSHOT",
            "1.0",
            "1.0-sp1",
            "1.0-foo",
            "1.0-1",
            "1.0.1",
            "1.1",
            "2.0",
        ]
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                with self.subTest(left=left, right=right):
                    self.assertLess(MavenVersion(left), MavenVersion(right))
        for left, right in [
            ("1", "1.0.0"),
            ("1.0", "1.0.Final"),
            ("1.0", "1-ga"),
            ("1.0", "1-release"),
            ("1.0-a1", "1.0-alpha-1"),
            ("1.0-cr1", "1.0-rc1"),
            ("1.0-foo2", "1.0-FOO2"),
            ("1.0-0", "1.0"),
        ]:
            with self.subTest(left=left, right=right):
                self.assertEqual(MavenVersion(left), MavenVersion(right))
        self.assertLess(MavenVersion("1.0.0.X1"), MavenVersion("1.0.0-X2"))
        self.assertGreater(MavenVersion("0.9.4.2"), MavenVersion("0.9.4"))

    def test_stable_build_forms_and_raw_identity_are_preserved(self):
        for value in [
            "33.4.8-jre",
            "32.1.3-android",
            "4.1.110.Final",
            "1.0.SP2",
            "8.10.1-12782657",
            "9-robolectric-4913185-2-i7",
            "15-robolectric-13954326-i7",
            "11-robolectric-6757853",
        ]:
            with self.subTest(value=value):
                parsed = registry.version("maven", value)
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.raw, value)
        for value in ["1.0-beta10", "1.0-SNAPSHOT", "1.0-dev", "1.0-mystery"]:
            self.assertIsNone(registry.version("maven", value))
            self.assertEqual(registry.lock_version("maven", value).raw, value)
        for invalid in [
            "v1.0",
            "../1",
            "1/2",
            "1..0",
            "1-",
            "1%2f2",
            "1 2",
            "1\n",
            "1" * 257,
        ]:
            self.assertIsNone(registry.lock_version("maven", invalid))

    def test_numeric_and_exact_qualified_constraints(self):
        for value, bound, expected in [
            ("4.1.110.Final", ">=4.1.100,<4.2", True),
            ("33.4.8-jre", ">=33,<34", True),
            ("1.0.Final", "==1.0", True),
            ("1.0.Final", "===1.0", False),
            ("9-robolectric-4913185-2-i7", "9-robolectric-4913185-2-i7", True),
            ("9-robolectric-4913185-2-i8", "9-robolectric-4913185-2-i7", False),
            ("1.4.9-jre", "~=1.4.0", True),
            ("1.5", "~=1.4.0", False),
            ("1.9", "~=1.4", True),
            ("2", "~=1.4", False),
            ("1.2.3-jre", "==1.2.*", True),
            ("1.3", "!=1.2.*", True),
        ]:
            with self.subTest(value=value, bound=bound):
                self.assertEqual(registry.compatible("maven", value, bound), expected)
        for bound in [">=2,bogus!", "[1,2)", "~=1", "==foo.*", "==1,,<2"]:
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                registry.compatible("maven", "1.0", bound)

    def test_qualified_releases_obey_cutoff_constraints_and_safe_floor(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        releases = [
            registry.Release("33.4.8-jre", now - timedelta(days=40)),
            registry.Release("33.5.0-jre", now - timedelta(days=10)),
            registry.Release("34.0.0", now - timedelta(days=40)),
        ]
        policy = {
            "constraints": {
                "maven:com.example:library": {
                    "range": ">=33,<34",
                    "reason": "API family",
                }
            }
        }
        self.assertEqual(
            registry.select(
                "maven", releases, policy, "com.example:library", now
            ).version,
            "33.4.8-jre",
        )
        policy["exceptions"] = [
            {
                "package": "maven:com.example:library",
                "version": "33.5.0-jre",
                "minimum_safe": "33.5.0-jre",
                "reason": "security fix",
                "advisory": "https://example.invalid/advisory",
                "expires": (now + timedelta(days=1)).isoformat(),
            }
        ]
        self.assertEqual(
            registry.select(
                "maven", releases, policy, "com.example:library", now
            ).version,
            "33.5.0-jre",
        )

    def test_retained_prerelease_requires_the_exact_same_artifact(self):
        now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        value = "1.0.0-beta10"
        url = f"https://dl.google.com/dl/android/maven2/com/android/example/{value}/example-{value}.jar"
        identity = updates.Identity(
            "maven", "com.android:example", value, url, "sha256:" + "a" * 64
        )
        evidence = registry.Release(
            value,
            now - timedelta(days=2),
            artifacts=(
                registry.Artifact(url, identity.digest, now - timedelta(days=2)),
            ),
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("lock_adapters.evidence", return_value=[evidence]),
        ):
            root = Path(temporary)
            updates.audit_identities(root, {identity}, {identity}, {}, now)
            with self.assertRaisesRegex(ValueError, "explicit project migration"):
                updates.audit_identities(root, {identity}, set(), {}, now)
            changed = identity._replace(digest="sha256:" + "b" * 64)
            with self.assertRaisesRegex(ValueError, "explicit project migration"):
                updates.audit_identities(root, {changed}, {identity}, {}, now)

    def test_artifact_url_and_filename_keep_the_original_qualifier(self):
        modified = "Mon, 01 Jun 2026 00:00:00 GMT"

        def fetch(url, accept="application/json", method="GET"):
            if method == "HEAD":
                return b"", {"Last-Modified": modified}
            return b"a" * 64, {}

        with patch.object(registry, "fetch", side_effect=fetch):
            artifact = registry.maven_artifact(
                "io.netty:netty-common",
                "4.1.110.Final",
                "netty-common-4.1.110.Final.jar",
            )
        self.assertIn("/4.1.110.Final/netty-common-4.1.110.Final.jar", artifact.url)
        with self.assertRaises(ValueError):
            registry.maven_artifact(
                "io.netty:netty-common", "4.1.110.Final", "netty-common-4.1.110.jar"
            )


if __name__ == "__main__":
    unittest.main()
