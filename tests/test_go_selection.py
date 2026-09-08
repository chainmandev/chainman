"""Independent Go selection oracles: prove a winner without lower broken history."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_api as api
import lock_adapters
import registry
import source_go

NOW = datetime(2026, 9, 8, 22, 12, tzinfo=timezone.utc)
PACKAGE = "example.com/storage"
PREFIX = f"https://proxy.golang.org/{PACKAGE}/@v/"


class GoSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Go selection fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "chainman.toml").write_text("schema=1\n[updates]\n")
        self.inventory = ["v1.0.0", "v3.0.0", "v2.0.0"]
        self.available = list(self.inventory)
        self.info = {
            "v1.0.0": registry.RegistryHTTPError(404, "proxy.golang.org"),
            "v2.0.0": self.metadata("v2.0.0", days=40),
            "v3.0.0": self.metadata("v3.0.0", days=1),
        }
        self.events = []
        self.native_error = None
        self.native_identity = PACKAGE
        self.enterContext(patch.object(registry, "fetch", side_effect=self.fetch))
        self.enterContext(
            patch.object(source_go, "native_json", side_effect=self.native)
        )
        self.enterContext(
            patch.object(lock_adapters, "native", side_effect=self.native)
        )
        self.enterContext(
            patch.object(
                registry, "observation_time", return_value=NOW + timedelta(hours=3)
            )
        )

    @staticmethod
    def metadata(value, *, days=40, published=None):
        return {
            "Version": value,
            "Time": (published or NOW - timedelta(days=days)).isoformat(),
        }

    def fetch(self, url, *_args, **_kwargs):
        self.assertTrue(url.startswith(PREFIX), url)
        suffix = url.removeprefix(PREFIX)
        if suffix == "list":
            self.events.append(("list", None))
            return "\n".join(self.inventory).encode(), {}
        self.assertTrue(suffix.endswith(".info"), url)
        value = unquote(suffix.removesuffix(".info"))
        self.events.append(("info", value))
        item = self.info[value]
        if isinstance(item, Exception):
            raise item
        return json.dumps(item).encode(), {}

    def native(self, *_args):
        query = _args[-1][-1]
        self.events.append(("native", query))
        if self.native_error:
            raise self.native_error
        self.assertEqual(query, PACKAGE + "@latest")
        return {
            "Path": self.native_identity,
            "Version": "v3.0.0",
            "Versions": self.available,
        }

    def info_reads(self):
        return [value for kind, value in self.events if kind == "info"]

    def selected(self, *, policy=None, current="v1.0.0", mode="aggressive"):
        return source_go.select(
            self.root,
            {"adapter": "go", "profile": "go", "mode": mode},
            PACKAGE,
            current,
            {} if policy is None else policy,
            NOW,
        )

    def public(self, *, policy=None, **fields):
        request = {
            "schema": 1,
            "operation": "select",
            "provider": "go",
            "package": PACKAGE,
            **fields,
        }
        with patch.object(api, "policy", return_value={} if policy is None else policy):
            return api.query(self.root, request, now=NOW)

    @staticmethod
    def exception(*, expired=False):
        return {
            "package": "go:" + PACKAGE,
            "version": "v3.0.0",
            "minimum_safe": "v2.0.0",
            "reason": "Verified exact security correction",
            "advisory": "https://example.invalid/advisory",
            "expires": (NOW + timedelta(days=-1 if expired else 30)).isoformat(),
        }

    def test_source_proves_greatest_mature_winner_without_lower_history(self):
        self.assertEqual(self.selected().version, "v2.0.0")
        self.assertEqual(self.info_reads(), ["v3.0.0", "v2.0.0"])

    def test_public_retention_preserves_newer_pin_without_claiming_maturity(self):
        result = self.public(current="v3.0.0")
        self.assertEqual(result["disposition"], "retained")
        self.assertEqual(result["version"], "v3.0.0")
        self.assertEqual(result["eligible_candidate"]["version"], "v2.0.0")
        self.assertNotIn("published", result)
        self.assertEqual(self.info_reads(), ["v3.0.0", "v2.0.0"])

    def test_missing_competitive_upper_evidence_never_falls_back(self):
        self.inventory = ["v3.0.0", "v2.0.0"]
        for invalid in (
            registry.RegistryHTTPError(404, "proxy.golang.org"),
            {"Version": "v3.0.0"},
            self.metadata("v9.0.0"),
        ):
            with self.subTest(invalid=type(invalid).__name__):
                self.events.clear()
                self.info["v3.0.0"] = invalid
                with self.assertRaises(ValueError):
                    self.selected()
                self.assertEqual(self.info_reads(), ["v3.0.0"])

    def test_native_retraction_filter_precedes_version_metadata(self):
        self.inventory = ["v3.0.0", "v2.0.0", "v1.0.0"]
        self.available = ["v2.0.0"]
        self.info["v3.0.0"] = registry.RegistryHTTPError(404, "proxy.golang.org")
        self.assertEqual(self.selected().version, "v2.0.0")
        self.assertEqual(self.info_reads(), ["v2.0.0"])
        native = next(i for i, event in enumerate(self.events) if event[0] == "native")
        info = next(i for i, event in enumerate(self.events) if event[0] == "info")
        self.assertLess(native, info)

    def test_native_failure_and_identity_mismatch_do_not_touch_metadata(self):
        for failure in ("unavailable", "identity", "inventory"):
            with self.subTest(failure=failure):
                self.events.clear()
                self.native_error = (
                    ValueError("Native Go inventory unavailable")
                    if failure == "unavailable"
                    else None
                )
                self.native_identity = (
                    "example.com/wrong" if failure == "identity" else PACKAGE
                )
                self.available = "not a version list" if failure == "inventory" else []
                with self.assertRaises(ValueError):
                    self.selected()
                self.assertEqual(self.info_reads(), [])

    def test_unordered_duplicates_and_nonstable_versions_cannot_change_winner(self):
        self.inventory = [
            "v1.0.0",
            "v3.0.0",
            "v9.0.0-rc.1",
            "v2.0.0",
            "v3.0.0",
            "v0.0.0-20200101000000-abcdef123456",
        ]
        self.available = list(self.inventory)
        self.assertEqual(self.selected().version, "v2.0.0")
        self.assertEqual(self.info_reads(), ["v3.0.0", "v2.0.0"])

    def test_equal_numeric_rank_cannot_hide_missing_build_metadata_identity(self):
        tied = ["v2.0.0", "v2.0.0+incompatible"]
        self.inventory = [*tied, "v1.0.0"]
        self.available = list(self.inventory)
        # Both placements matter: a deterministic tie order must encounter a
        # healthy first member in one case before the later missing identity.
        for missing in tied:
            with self.subTest(missing=missing):
                self.events.clear()
                self.info.update({value: self.metadata(value) for value in tied})
                self.info[missing] = registry.RegistryHTTPError(404, "proxy.golang.org")
                with self.assertRaises(registry.RegistryHTTPError):
                    self.public()
                self.assertIn(missing, self.info_reads())
                self.assertNotIn("v1.0.0", self.info_reads())

    def test_request_and_configured_bounds_filter_before_metadata(self):
        self.info["v3.0.0"] = registry.RegistryHTTPError(404, "proxy.golang.org")
        policy = {
            "constraints": {
                "go:" + PACKAGE: {"range": ">=2", "reason": "Supported API floor"}
            }
        }
        result = self.public(
            policy=policy,
            constraint={"range": "<3", "reason": "Supported project API ceiling"},
        )
        self.assertEqual(result["version"], "v2.0.0")
        self.assertEqual(self.info_reads(), ["v2.0.0"])

    def test_compatible_source_mode_does_not_fetch_outside_minor_bound(self):
        self.inventory = ["v2.5.0", "v2.4.2", "v2.3.0"]
        self.available = list(self.inventory)
        self.info = {
            "v2.5.0": registry.RegistryHTTPError(404, "proxy.golang.org"),
            "v2.4.2": self.metadata("v2.4.2"),
            "v2.3.0": registry.RegistryHTTPError(404, "proxy.golang.org"),
        }
        self.assertEqual(
            self.selected(current="v2.4.1", mode="compatible").version, "v2.4.2"
        )
        self.assertEqual(self.info_reads(), ["v2.4.2"])

    def test_invalid_request_bound_fails_before_metadata(self):
        for bound in (
            {"range": "not a version range", "reason": "Explicit invalid fixture"},
            {"range": "<3"},
        ):
            with self.subTest(bound=bound):
                self.events.clear()
                with self.assertRaises(ValueError):
                    self.public(constraint=bound)
                self.assertEqual(self.info_reads(), [])

    def test_retention_cannot_escape_either_compatibility_bound(self):
        rule = {"range": "<3", "reason": "Required API compatibility"}
        for source in ("request", "configured"):
            with self.subTest(source=source):
                self.events.clear()
                policy = (
                    {"constraints": {"go:" + PACKAGE: rule}}
                    if source == "configured"
                    else {}
                )
                fields = {"constraint": rule} if source == "request" else {}
                with self.assertRaisesRegex(ValueError, "Retained.*compatibility"):
                    self.public(current="v3.0.0", policy=policy, **fields)

    def test_exact_metadata_ignores_unrelated_history_without_claiming_eligibility(
        self,
    ):
        self.info["v3.0.0"] = registry.RegistryHTTPError(404, "proxy.golang.org")
        self.info["v2.0.0"] = self.metadata("v2.0.0", days=1)
        result = self.public(operation="metadata", version="v2.0.0")
        self.assertEqual(result["disposition"], "metadata")
        self.assertEqual(result["version"], "v2.0.0")
        self.assertEqual(self.info_reads(), ["v2.0.0"])

    def test_exact_missing_mismatched_or_future_metadata_stays_strict(self):
        self.inventory = ["v2.0.0"]
        self.available = list(self.inventory)
        for invalid in (
            registry.RegistryHTTPError(404, "proxy.golang.org"),
            {"Version": "v2.0.0"},
            self.metadata("v3.0.0"),
            self.metadata("v2.0.0", published=NOW + timedelta(hours=4)),
        ):
            with self.subTest(invalid=type(invalid).__name__):
                self.info["v2.0.0"] = invalid
                with self.assertRaises(ValueError):
                    self.public(operation="metadata", version="v2.0.0")

    def test_mature_safe_candidate_retires_even_an_expired_higher_exception(self):
        for expired in (False, True):
            with self.subTest(expired=expired):
                self.events.clear()
                result = self.selected(
                    policy={"exceptions": [self.exception(expired=expired)]}
                )
                self.assertEqual(result.version, "v2.0.0")
                self.assertEqual(self.info_reads(), ["v3.0.0", "v2.0.0"])

    def test_exact_exception_requires_complete_safe_search_and_valid_expiry(self):
        self.inventory = ["v1.0.0", "v3.0.0", "v4.0.0"]
        self.available = list(self.inventory)
        self.info["v4.0.0"] = self.metadata("v4.0.0", days=2)
        result = self.selected(policy={"exceptions": [self.exception()]})
        self.assertEqual(result.version, "v3.0.0")
        self.assertEqual(self.info_reads(), ["v4.0.0", "v3.0.0"])
        self.events.clear()
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.selected(policy={"exceptions": [self.exception(expired=True)]})
        self.assertEqual(self.info_reads(), ["v4.0.0", "v3.0.0"])

    def test_no_eligible_release_never_widens_declared_bounds(self):
        policy = {
            "constraints": {
                "go:" + PACKAGE: {"range": ">=3", "reason": "Required new API"}
            }
        }
        with self.assertRaisesRegex(ValueError, "No eligible"):
            self.selected(policy=policy)
        self.assertEqual(self.info_reads(), ["v3.0.0"])

    def test_age_boundary_and_post_anchor_versions_keep_frozen_eligibility(self):
        self.info["v3.0.0"] = self.metadata(
            "v3.0.0", published=NOW - timedelta(days=30) + timedelta(seconds=1)
        )
        self.info["v2.0.0"] = self.metadata("v2.0.0", days=30)
        self.assertEqual(self.selected().version, "v2.0.0")
        self.events.clear()
        self.info["v3.0.0"] = self.metadata(
            "v3.0.0", published=NOW + timedelta(seconds=1)
        )
        self.assertEqual(
            self.selected(policy={"minimum_age_days": 0}).version, "v2.0.0"
        )
        self.assertEqual(self.info_reads(), ["v3.0.0", "v2.0.0"])


if __name__ == "__main__":
    unittest.main()
