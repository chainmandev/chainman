"""Published channel selection authenticates history, source identity and both ages."""

import copy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nix_channels as channels
import source_updates as sources

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=60)
MATURE = NOW - timedelta(days=30)
YOUNG = NOW - timedelta(days=29)
A, B, C = "a" * 40, "b" * 40, "c" * 40
SRI = "sha256-" + "A" * 43 + "="
ITEM = {
    "directory": ".",
    "input": "nixpkgs",
    "repository": "NixOS/nixpkgs",
    "branch": "nixos-unstable",
    "selection": "published-channel",
}
CHANNEL = channels.channel(ITEM)


def release(commit, date):
    return channels.Publication(
        "nixos/unstable/nixos-26.11pre123." + commit[:12], date, commit[:12]
    )


def page(publications, *, token=None, prefix="nixos/unstable/", truncated=None):
    root = ET.Element(channels.XML_NAMESPACE + "ListBucketResult")
    for name, value in (
        ("Name", "nix-releases"),
        ("Prefix", prefix),
        ("IsTruncated", truncated or ("true" if token is not None else "false")),
    ):
        ET.SubElement(root, channels.XML_NAMESPACE + name).text = value
    if token is not None:
        ET.SubElement(
            root, channels.XML_NAMESPACE + "NextContinuationToken"
        ).text = token
    for publication in publications:
        child = ET.SubElement(root, channels.XML_NAMESPACE + "Contents")
        ET.SubElement(child, channels.XML_NAMESPACE + "Key").text = publication.key
        ET.SubElement(
            child, channels.XML_NAMESPACE + "LastModified"
        ).text = publication.published.isoformat()
    return ET.tostring(root), {}


class ChannelHistoryTests(unittest.TestCase):
    def test_supported_channel_scopes_and_generic_git_default(self):
        self.assertIsNone(channels.channel({"repository": "other/repo", "branch": A}))
        for branch, prefix in (
            ("nixos-unstable", "nixos/unstable/"),
            ("nixos-unstable-small", "nixos/unstable-small/"),
            ("nixpkgs-unstable", "nixpkgs/"),
            ("nixos-26.05", "nixos/26.05/"),
            ("nixos-26.11-small", "nixos/26.11-small/"),
            ("nixpkgs-26.05-darwin", "nixpkgs/26.05-darwin/"),
        ):
            with self.subTest(branch=branch):
                self.assertEqual(
                    channels.channel({**ITEM, "branch": branch}).prefix, prefix
                )
        for changes in (
            {"selection": "latest"},
            {"selection": True},
            {"repository": "other/repo"},
            {"branch": A},
            {"branch": "release-26.05"},
            {"branch": "https://example.org/channel"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                channels.channel({**ITEM, **changes})

    def test_complete_pagination_selects_publication_age_not_commit_age(self):
        records = [
            channels.Publication(
                "nixos/unstable/nixos-26.05pre100." + A[:12], OLD, A[:12]
            ),
            release(C, YOUNG),
            release(B, MATURE),
        ]

        def fetch(url, accept, **kwargs):
            self.assertTrue(kwargs["anonymous"])
            if url.startswith(channels.LISTING):
                query = parse_qs(urlparse(url).query)
                self.assertEqual(query["prefix"], ["nixos/unstable/"])
                if "continuation-token" not in query:
                    return page(records[:1], token="next+/=")
                self.assertEqual(query["continuation-token"], ["next+/="])
                return page(records[1:])
            self.assertEqual(url, records[2].url + "git-revision")
            return (B + "\n").encode(), {"Last-Modified": format_datetime(MATURE)}

        with (
            patch.object(channels.registry, "fetch", side_effect=fetch),
            patch.object(sources, "commit_time", return_value=OLD),
            patch.object(sources, "descendant", return_value=True) as ancestry,
        ):
            chosen = sources.selected_nix_candidate(ITEM, {}, NOW)
        self.assertEqual(chosen["revision"], B)
        self.assertEqual(chosen["published"], OLD.isoformat())
        self.assertEqual(chosen["channel_publication"], MATURE.isoformat())
        ancestry.assert_called_once_with("NixOS/nixpkgs", B, "nixos-unstable")

    def test_no_mature_publication_is_an_error(self):
        with (
            patch.object(
                channels.registry, "fetch", return_value=page([release(A, YOUNG)])
            ),
            self.assertRaisesRegex(sources.NoMatureRevision, "No mature"),
        ):
            sources.selected_nix_candidate(ITEM, {}, NOW)

    def test_missing_inconsistent_and_unbounded_history_fails_closed(self):
        malformed = [
            (b"not XML", {}),
            (b"<!DOCTYPE x><ListBucketResult/>", {}),
            (b"x" * (1024 * 1024 + 1), {}),
            page([], prefix="nixos/other/"),
            page([release(A, OLD), release(A, OLD)]),
            page([], truncated="unknown"),
            page([], token=""),
        ]
        for index, body in enumerate(malformed):
            with (
                self.subTest(index=index),
                patch.object(channels.registry, "fetch", return_value=body),
                self.assertRaises(ValueError),
            ):
                channels.publications(CHANNEL)
        for limit, response in (
            ("PAGE_LIMIT", page([], token="repeated")),
            ("RELEASE_LIMIT", page([release(A, OLD), release(B, OLD)])),
        ):
            with (
                self.subTest(limit=limit),
                patch.object(channels, limit, 1),
                patch.object(channels.registry, "fetch", return_value=response),
                self.assertRaisesRegex(ValueError, "limit exceeded"),
            ):
                channels.publications(CHANNEL)
        with (
            patch.object(
                channels.registry, "fetch", return_value=page([], token="repeated")
            ),
            self.assertRaisesRegex(ValueError, "pagination"),
        ):
            channels.publications(CHANNEL)

    def test_release_identity_header_and_source_relationships_are_bound(self):
        publication = release(A, MATURE)
        for body, headers in (
            (B.encode(), {"Last-Modified": format_datetime(MATURE)}),
            (A[:12].encode(), {"Last-Modified": format_datetime(MATURE)}),
            (A.encode(), {}),
            (A.encode(), {"Last-Modified": "invalid"}),
            (A.encode(), {"Last-Modified": format_datetime(YOUNG)}),
        ):
            with (
                self.subTest(body=body, headers=headers),
                patch.object(channels.registry, "fetch", return_value=(body, headers)),
                self.assertRaises(ValueError),
            ):
                channels.revision(publication)
        for commit_date, reachable in (
            (YOUNG, True),
            (NOW + timedelta(days=1), True),
            (OLD, False),
        ):
            with (
                self.subTest(commit_date=commit_date, reachable=reachable),
                patch.object(channels, "revision", return_value=A),
                patch.object(sources, "commit_time", return_value=commit_date),
                patch.object(sources, "descendant", return_value=reachable),
                self.assertRaisesRegex(ValueError, "source branch/date"),
            ):
                sources.published_nix_revision(ITEM, publication)


class ChannelPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="published Nix channel test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "flake.nix").write_text("{}\n")
        self.spec = {"adapter": "nix", "profile": "core", "inputs": [ITEM]}
        self.lock = {
            "version": 7,
            "root": "root",
            "nodes": {
                "root": {"inputs": {"nixpkgs": "nixpkgs"}},
                "nixpkgs": {
                    "original": {
                        "owner": "NixOS",
                        "repo": "nixpkgs",
                        "ref": "nixos-unstable",
                        "type": "github",
                    },
                    "locked": {
                        "owner": "NixOS",
                        "repo": "nixpkgs",
                        "type": "github",
                        "rev": C,
                        "lastModified": int(YOUNG.timestamp()),
                        "narHash": SRI,
                    },
                },
            },
        }
        self.write_lock(self.lock)
        self.records = [release(B, YOUNG), release(A, MATURE)]
        for mock in (
            patch.object(channels, "publications", side_effect=lambda _: self.records),
            patch.object(
                channels,
                "revision",
                side_effect=lambda p: {A[:12]: A, B[:12]: B}[p.abbreviated_revision],
            ),
            patch.object(
                sources,
                "commit_time",
                side_effect=lambda repo, c: YOUNG if c == C else OLD,
            ),
            patch.object(
                sources,
                "descendant",
                side_effect=lambda repo, ancestor, target: (
                    target == ITEM["branch"] or ancestor == target or ancestor == A
                ),
            ),
            patch.object(sources, "nix_tree", return_value={"narHash": SRI}),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def write_lock(self, lock):
        (self.root / "flake.lock").write_text(json.dumps(lock))

    def before(self):
        return sources.nix_snapshot(self.root, self.spec)

    def approval(self, commit=B, expires=NOW + timedelta(days=1)):
        return {
            "nix_exceptions": [
                {
                    "directory": ".",
                    "input": "nixpkgs",
                    "repository": "NixOS/nixpkgs",
                    "revision": commit,
                    "reason": "Reviewed security floor",
                    "advisory": "https://example.org/advisory",
                    "expires": expires.isoformat(),
                }
            ]
        }

    def test_unpublished_newer_baseline_cannot_override_channel_selection(self):
        plan = sources.nix_plan(self.before(), self.spec, {}, NOW)[0]
        self.assertEqual(plan["revision"], A)
        self.assertEqual(plan["published"], OLD.isoformat())
        self.assertEqual(plan["channel_publication"], MATURE.isoformat())

    def test_final_audit_keeps_commit_timestamp_and_authenticates_unchanged_lock(self):
        baseline = self.before()
        selected = copy.deepcopy(self.lock)
        selected["nodes"]["nixpkgs"]["locked"].update(
            rev=A, lastModified=int(OLD.timestamp())
        )
        self.write_lock(selected)
        sources.audit_nix(self.root, self.spec, baseline, {}, NOW)
        selected["nodes"]["nixpkgs"]["locked"]["lastModified"] = int(MATURE.timestamp())
        self.write_lock(selected)
        with self.assertRaisesRegex(ValueError, "timestamp/hash"):
            sources.audit_nix(self.root, self.spec, baseline, {}, NOW)
        selected["nodes"]["nixpkgs"]["locked"]["lastModified"] = int(OLD.timestamp())
        self.write_lock(selected)
        unchanged = self.before()
        with (
            patch.object(
                sources,
                "nix_tree",
                return_value={"narHash": "sha256-" + "B" * 43 + "="},
            ),
            self.assertRaisesRegex(ValueError, "content hash differs"),
        ):
            sources.audit_nix(self.root, self.spec, unchanged, {}, NOW)
        self.write_lock(self.lock)
        with self.assertRaisesRegex(ValueError, "selected immutable source"):
            sources.audit_nix(self.root, self.spec, baseline, {}, NOW)

    def test_security_floor_requires_a_real_publication_and_checks_its_age(self):
        before = self.before()
        chosen = sources.nix_plan(before, self.spec, self.approval(), NOW)[0]
        self.assertEqual(chosen["revision"], B)
        self.assertEqual(chosen["published"], OLD.isoformat())
        self.assertEqual(chosen["channel_publication"], YOUNG.isoformat())
        with self.assertRaisesRegex(ValueError, "Expired"):
            sources.nix_plan(before, self.spec, self.approval(expires=NOW), NOW)
        with self.assertRaisesRegex(ValueError, "no official channel publication"):
            sources.nix_plan(before, self.spec, self.approval(commit=C), NOW)
        self.records = [release(B, NOW + timedelta(days=1)), release(A, MATURE)]
        with self.assertRaisesRegex(ValueError, "future date"):
            sources.nix_plan(before, self.spec, self.approval(), NOW)
        self.records = [release(B, MATURE), release(A, OLD)]
        self.assertEqual(
            sources.nix_plan(
                before, self.spec, self.approval(commit=A, expires=NOW), NOW
            )[0]["revision"],
            B,
        )

    def test_invalid_selection_is_rejected_before_metadata_or_mutations(self):
        for changes in (
            {"selection": "hold"},
            {"repository": "other/repo"},
            {"branch": C},
        ):
            spec = {**self.spec, "inputs": [{**ITEM, **changes}]}
            with (
                self.subTest(changes=changes),
                patch.object(
                    channels.registry,
                    "fetch",
                    side_effect=AssertionError("network before validation"),
                ),
                self.assertRaises(ValueError),
            ):
                sources.nix_snapshot(self.root, spec)


if __name__ == "__main__":
    unittest.main()
