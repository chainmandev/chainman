"""Source intent: dated identities survive selection, native resolution and hooks."""

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import source_go
import source_toolchain
import source_updates as sources

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=100)
MATURE = NOW - timedelta(days=30)
YOUNG = NOW - timedelta(days=29)
A, B, C = "a" * 40, "b" * 40, "c" * 40
DA, DB = "sha256:" + "a" * 64, "sha256:" + "b" * 64
SRI = "sha256-" + base64.b64encode(b"a" * 32).decode()


class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source update fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write("chainman.toml", 'schema=1\n[project]\ndefault_profile="host"\n')

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def json(self, name, value):
        return self.write(name, json.dumps(value, indent=2) + "\n")


class ActionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.spec = {"adapter": "actions", "files": [".github/workflows/*.yml"]}
        self.file = self.write(
            ".github/workflows/build.yml",
            f"jobs:\n  test:\n    steps:\n      - uses: 'sample/action/subpath@{A}' # deps-update: release-major=v1\n",
        )
        release_patch = patch.object(
            sources,
            "action_refs",
            return_value={"v1.1.0": A, "v2": B, "v3": C},
        )
        release_patch.start()
        self.addCleanup(release_patch.stop)
        commits = patch.object(
            registry,
            "data",
            return_value=[
                {
                    "tag_name": tag,
                    "draft": False,
                    "prerelease": False,
                    "published_at": at.isoformat(),
                }
                for tag, at in (("v1.1.0", OLD), ("v2", MATURE), ("v3", YOUNG))
            ],
        )
        commits.start()
        self.addCleanup(commits.stop)
        time_patch = patch.object(
            sources,
            "commit_time",
            side_effect=lambda repo, rev: {A: OLD, B: MATURE, C: YOUNG}[rev],
        )
        time_patch.start()
        self.addCleanup(time_patch.stop)

    def test_mature_major_subpath_and_quotes_are_preserved(self):
        before = sources.snapshot(self.root, self.spec)
        result = sources.resolve(self.root, self.spec, {}, NOW)
        self.assertEqual(result["changed"], [".github/workflows/build.yml"])
        self.assertIn(f"'sample/action/subpath@{B}'", self.file.read_text())
        self.assertIn("release-major=v2", self.file.read_text())
        sources.audit(self.root, self.spec, before, {}, NOW)

    def test_major_hold_and_constraint_are_independent(self):
        for spec, policy in [
            ({**self.spec, "advance_major": False}, {}),
            (
                self.spec,
                {
                    "constraints": {
                        "github:sample/action": {
                            "range": "<2",
                            "reason": "API constraint",
                        }
                    }
                },
            ),
        ]:
            with self.subTest(spec=spec, policy=policy):
                result = sources.resolve(self.root, spec, policy, NOW)
                self.assertEqual(result["changed"], [])

    def test_standard_version_comment_is_advanced(self):
        self.file.write_text(f"steps:\n  - uses: sample/action@{A} # v1.1.0\n")
        before = sources.snapshot(self.root, self.spec)
        sources.resolve(self.root, self.spec, {}, NOW)
        self.assertIn("# v2.0.0", self.file.read_text())
        sources.audit(self.root, self.spec, before, {}, NOW)

    def test_pin_needs_no_new_metadata(self):
        self.file.write_text(
            f"steps:\n  - uses: sample/action@{A} # deps-update: pin\n"
        )
        with patch.object(
            sources, "action_refs", side_effect=AssertionError("fixed pin queried")
        ):
            before = sources.snapshot(self.root, self.spec)
            self.assertEqual(
                sources.resolve(self.root, self.spec, {}, NOW)["changed"], []
            )
            sources.audit(self.root, self.spec, before, {}, NOW)

    def test_post_hook_identity_tracking_and_inventory_changes_are_rejected(self):
        before = sources.snapshot(self.root, self.spec)
        sources.resolve(self.root, self.spec, {}, NOW)
        valid = self.file.read_text()
        for text in (
            valid.replace(B, C),
            valid.replace("release-major=v2", "release-major=v9"),
            valid + f"      - uses: sample/extra@{A}\n",
        ):
            self.file.write_text(text)
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ValueError, "identity|tracking|inventory"),
            ):
                sources.audit(self.root, self.spec, before, {}, NOW)

    def test_unparsed_symbolic_flow_and_symlinked_workflows_fail(self):
        for text in (
            "steps:\n  - uses: sample/action@main\n",
            f"steps: [{{uses: sample/action@{A}}}]\n",
        ):
            self.file.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                sources.snapshot(self.root, self.spec)
        self.file.unlink()
        self.file.symlink_to(self.root / "chainman.toml")
        with self.assertRaisesRegex(ValueError, "symlink"):
            sources.snapshot(self.root, self.spec)

    def test_channel_has_immutable_age_and_does_not_downgrade(self):
        tracking = {"kind": "channel", "channel": "stable"}
        with patch.object(
            sources,
            "nix_candidate",
            return_value={"revision": B, "published": MATURE.isoformat()},
        ):
            self.assertEqual(
                sources.select_action("sample/action", A, tracking, {}, NOW)[
                    "revision"
                ],
                B,
            )
            self.assertEqual(
                sources.select_action("sample/action", C, tracking, {}, NOW)[
                    "revision"
                ],
                C,
            )


class EvidenceTests(unittest.TestCase):
    def test_integer_release_tags_bind_publication_and_current_commit(self):
        entries = [
            {
                "tag_name": "v2",
                "draft": False,
                "prerelease": False,
                "published_at": OLD.isoformat(),
            }
        ]
        with patch.object(registry, "data", return_value=entries):
            releases = sources.action_release_batch("sample/action")
        self.assertEqual(releases["v2"].version, "2.0.0")
        self.assertEqual(releases["v2"].published, OLD)
        with (
            patch.object(sources, "action_refs", return_value={"v2": B}),
            patch.object(sources, "action_release_batch", return_value=releases),
            patch.object(sources, "commit_time", return_value=YOUNG),
            self.assertRaisesRegex(ValueError, "eligible"),
        ):
            sources.select_action("sample/action", A, {"kind": "release"}, {}, NOW)

    def test_missing_release_date_is_not_a_noop(self):
        item = {"tag_name": "v2", "draft": False, "prerelease": False}
        with (
            patch.object(registry, "data", return_value=[item]),
            self.assertRaisesRegex(ValueError, "publication"),
        ):
            sources.action_release_batch("sample/action")

    def test_branch_selection_checks_the_returned_identity_age(self):
        with (
            patch.object(registry, "data", return_value=[{"sha": B}]),
            patch.object(sources, "commit_time", return_value=YOUNG),
            self.assertRaisesRegex(ValueError, "younger"),
        ):
            sources.nix_candidate("sample/tool", "main", {}, NOW)


class ActionRetentionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.spec = {"adapter": "actions", "files": ["workflow.yml"]}

    def metadata(self, refs, dates):
        records = [
            {
                "tag_name": tag,
                "published_at": (YOUNG if commit == A else OLD).isoformat(),
                "draft": False,
                "prerelease": False,
            }
            for tag, commit in refs.items()
        ]
        for mocked in (
            patch.object(sources, "action_refs", return_value=refs),
            patch.object(registry, "data", return_value=records),
            patch.object(sources, "commit_time", side_effect=lambda r, c: dates[c]),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def assert_resolved_and_final_audit_bound(self, annotation, policy):
        file = self.write(
            "workflow.yml", f"steps:\n  - uses: sample/action@{A} # {annotation}\n"
        )
        original = file.read_text()
        before = sources.snapshot(self.root, self.spec)
        sources.resolve(self.root, self.spec, policy, NOW)
        self.assertIn(B, file.read_text())
        sources.audit(self.root, self.spec, before, policy, NOW)
        file.write_text(original)
        with self.assertRaisesRegex(ValueError, "identity|tracking"):
            sources.audit(self.root, self.spec, before, policy, NOW)

    def test_newer_current_cannot_escape_explicit_version_constraint(self):
        self.metadata({"v2.1.0": B, "v3.0.0": A}, {A: YOUNG, B: OLD})
        policy = {
            "constraints": {
                "github:sample/action": {"range": "<3", "reason": "API contract"}
            }
        }
        self.assert_resolved_and_final_audit_bound(
            "deps-update: release-major=v3", policy
        )

    def test_newer_current_cannot_escape_security_safe_floor(self):
        self.metadata({"v2.0.0": A, "v2.1.0": B}, {A: YOUNG, B: OLD})
        policy = {
            "exceptions": [
                {
                    "package": "github:sample/action",
                    "version": "2.1.0",
                    "minimum_safe": "2.1.0",
                    "reason": "security fix",
                    "advisory": "TEST-2",
                    "expires": OLD.isoformat(),
                }
            ]
        }
        self.assert_resolved_and_final_audit_bound("v2.0.0", policy)

    def test_unknown_current_version_cannot_be_inferred_from_major_comment(self):
        self.metadata({"v2.1.0": B}, {A: YOUNG, B: OLD})
        for policy, advance in (
            (
                {
                    "constraints": {
                        "github:sample/action": {"range": "<3", "reason": "API"}
                    }
                },
                True,
            ),
            ({}, False),
        ):
            with (
                self.subTest(policy=policy, advance=advance),
                self.assertRaisesRegex(ValueError, "current.*version evidence"),
            ):
                sources.select_action(
                    "sample/action",
                    A,
                    {"kind": "release", "major": 2},
                    policy,
                    NOW,
                    advance_major=advance,
                )

    def test_compatible_current_keeps_its_baseline_age_and_no_downgrade(self):
        self.metadata({"v2.2.0": A, "v2.1.0": B}, {A: YOUNG, B: OLD})
        policy = {
            "constraints": {"github:sample/action": {"range": "<3", "reason": "API"}}
        }
        selected = sources.select_action(
            "sample/action", A, {"kind": "release", "major": 2}, policy, NOW
        )
        self.assertEqual(
            selected, {"revision": A, "reason": "retained newer current release"}
        )

    def test_current_version_uses_highest_published_identity_not_lower_alias(self):
        self.metadata({"v2": A, "v2.1.0": B, "v3.0.0": A}, {A: YOUNG, B: OLD})
        policy = {
            "constraints": {"github:sample/action": {"range": "<3", "reason": "API"}}
        }
        selected = sources.select_action(
            "sample/action", A, {"kind": "release", "major": 2}, policy, NOW
        )
        self.assertEqual(selected["revision"], B)

    def test_known_current_outside_major_hold_yields_the_eligible_candidate(self):
        self.metadata({"v2.1.0": B, "v3.0.0": A}, {A: YOUNG, B: OLD})
        selected = sources.select_action(
            "sample/action",
            A,
            {"kind": "release", "major": 2},
            {},
            NOW,
            advance_major=False,
        )
        self.assertEqual(selected["revision"], B)


class ActionInventoryTests(unittest.TestCase):
    @staticmethod
    def publication(tag, at=OLD, **extra):
        return {
            "tag_name": tag,
            "published_at": at.isoformat(),
            "draft": False,
            "prerelease": False,
            **extra,
        }

    @staticmethod
    def advertisement(refs):
        values = [b"# service=git-upload-pack\n", None]
        values.append(f"{A} HEAD\0object-format=sha1\n".encode())
        values.extend(f"{oid} {name}\n".encode() for name, oid in refs)
        values.append(None)
        return b"".join(
            b"0000" if value is None else f"{len(value) + 4:04x}".encode() + value
            for value in values
        )

    def fetched_refs(self, body):
        return patch.object(
            registry,
            "fetch",
            return_value=(
                body,
                {"Content-Type": "application/x-git-upload-pack-advertisement"},
            ),
        )

    def test_complete_inventory_beyond_1000_and_lazy_exact_release_lookup(self):
        refs = [(f"refs/tags/v1.0.{i}", B) for i in range(1500)]
        body = self.advertisement(refs)
        urls = []

        def metadata(url):
            urls.append(url)
            if url.endswith("releases?per_page=100&page=1"):
                # A full, deliberately unrelated page cannot establish completeness.
                return [self.publication(f"tool-{i}") for i in range(100)]
            if url.endswith("releases/tags/v1.0.1499"):
                return self.publication("v1.0.1499", MATURE)
            raise AssertionError(url)

        with (
            self.fetched_refs(body),
            patch.object(registry, "data", side_effect=metadata),
            patch.object(sources, "commit_time", side_effect=lambda r, c: OLD),
        ):
            self.assertEqual(len(sources.action_refs("sample/action")), 1500)
            selected = sources.select_action(
                "sample/action", A, {"kind": "release", "major": 0}, {}, NOW
            )
        self.assertEqual((selected["revision"], selected["tag"]), (B, "v1.0.1499"))
        self.assertEqual(selected["published"], MATURE.isoformat())
        self.assertEqual(len(urls), 2)

    def test_peeling_and_alias_dates_are_bound_before_selection(self):
        body = self.advertisement(
            [("refs/tags/v2", C), ("refs/tags/v2^{}", B), ("refs/tags/v2.0.0", A)]
        )
        with self.fetched_refs(body):
            refs = sources.action_refs("sample/action")
        self.assertEqual(refs, {"v2": B, "v2.0.0": A})
        with (
            patch.object(sources, "action_refs", return_value=refs),
            patch.object(
                registry, "data", return_value=[self.publication(tag) for tag in refs]
            ),
            patch.object(
                sources,
                "commit_time",
                side_effect=lambda r, c: YOUNG if c == B else OLD,
            ),
            self.assertRaisesRegex(ValueError, "eligible"),
        ):
            sources.select_action("sample/action", A, {"kind": "release"}, {}, NOW)

    def test_truncated_conflicting_or_unbounded_refs_fail_before_selection(self):
        valid = self.advertisement([("refs/tags/v2", B)])
        invalid = (
            valid[:-4],
            valid[:-8] + b"0000",
            valid + b"0000",
            valid.replace(b"001e", b"0003", 1),
            valid.replace(b"001e", b"ffff", 1),
            valid.replace(b"service=git-upload-pack", b"service=git-receive-pack"),
            self.advertisement([("refs/tags/v2", A), ("refs/tags/v2", B)]),
            self.advertisement([("refs/tags/v2^{}", B)]),
            self.advertisement([("refs/tags/v" + "1" * 129, B)]),
            valid.replace(b"object-format=sha1", b"object-format=sha256"),
        )
        for body in invalid:
            with (
                self.subTest(body=body[-90:]),
                self.fetched_refs(body),
                self.assertRaises(ValueError),
            ):
                sources.action_refs("sample/action")
        with (
            self.fetched_refs(valid),
            patch.object(registry, "MAX_RESPONSE_BYTES", len(valid) - 1),
            self.assertRaisesRegex(ValueError, "bound"),
        ):
            sources.action_refs("sample/action")
        with (
            self.fetched_refs(valid),
            patch.object(sources, "ACTION_REF_LIMIT", 1),
            self.assertRaisesRegex(ValueError, "bound"),
        ):
            sources.action_refs("sample/action")
        with (
            patch.object(
                registry, "fetch", return_value=(valid, {"Content-Type": "text/plain"})
            ),
            self.assertRaisesRegex(ValueError, "media type"),
        ):
            sources.action_refs("sample/action")

    def test_batch_is_bounded_and_conflicting_identities_fail(self):
        item = self.publication("v2")
        for values in (
            None,
            [item] * 101,
            [item, {**item, "published_at": YOUNG.isoformat()}],
        ):
            with (
                self.subTest(values=values),
                patch.object(registry, "data", return_value=values),
                self.assertRaises(ValueError),
            ):
                sources.action_release_batch("sample/action")

    def test_unpublished_tags_are_excluded_but_missing_or_wrong_evidence_fails(self):
        with patch.object(
            registry,
            "data",
            side_effect=registry.RegistryHTTPError(404, "api.github.com"),
        ):
            self.assertIsNone(sources.action_release_by_tag("sample/action", "v2"))
        for value in (
            self.publication("v3"),
            self.publication("v2", draft=None),
            self.publication("v2", prerelease="false"),
            self.publication("v2", published_at=None),
        ):
            with (
                patch.object(registry, "data", return_value=value),
                self.assertRaises(ValueError),
            ):
                sources.action_release_by_tag("sample/action", "v2")
        for status in (403, 422, 500):
            with (
                patch.object(
                    registry,
                    "data",
                    side_effect=registry.RegistryHTTPError(status, "api.github.com"),
                ),
                self.assertRaises(registry.RegistryHTTPError),
            ):
                sources.action_release_by_tag("sample/action", "v2")

    def test_young_batch_tag_does_not_hide_an_older_eligible_release(self):
        def metadata(url):
            if url.endswith("page=1"):
                return [self.publication("v3", YOUNG)]
            if url.endswith("tags/v2"):
                return self.publication("v2", MATURE)
            raise AssertionError(url)

        def commit_date(repository, commit):
            self.assertNotEqual(
                commit, C, "An already young release needs no commit query"
            )
            return OLD

        with (
            patch.object(sources, "action_refs", return_value={"v2": B, "v3": C}),
            patch.object(registry, "data", side_effect=metadata),
            patch.object(sources, "commit_time", side_effect=commit_date),
        ):
            selected = sources.select_action(
                "sample/action", A, {"kind": "release", "major": 1}, {}, NOW
            )
        self.assertEqual(selected["revision"], B)

    def test_positive_batch_evidence_is_never_discarded_by_an_exact_query(self):
        with (
            patch.object(sources, "action_refs", return_value={"v2": B}),
            patch.object(
                registry, "data", return_value=[self.publication("v2", MATURE)]
            ),
            patch.object(
                sources,
                "action_release_by_tag",
                side_effect=AssertionError("Published batch release was reclassified"),
            ),
            patch.object(sources, "commit_time", return_value=OLD),
        ):
            selected = sources.select_action(
                "sample/action", A, {"kind": "release", "major": 1}, {}, NOW
            )
        self.assertEqual(selected["revision"], B)
        with (
            patch.object(sources, "action_refs", return_value={"v2": B}),
            patch.object(registry, "data", return_value=[self.publication("v3")]),
            self.assertRaisesRegex(ValueError, "lacks its advertised immutable tag"),
        ):
            sources.select_action("sample/action", A, {"kind": "release"}, {}, NOW)

    def test_post_anchor_release_preserves_older_eligible_selection(self):
        def metadata(url):
            if url.endswith("page=1"):
                return [self.publication("v3", NOW + timedelta(minutes=1))]
            if url.endswith("tags/v2"):
                return self.publication("v2", MATURE)
            raise AssertionError(url)

        def commit_date(repository, commit):
            self.assertNotEqual(commit, C, "A post-anchor release cannot be bound")
            return OLD

        for age in (0, 30):
            with (
                self.subTest(minimum_age_days=age),
                patch.object(
                    registry,
                    "observation_time",
                    return_value=NOW + timedelta(minutes=2),
                ),
                patch.object(sources, "action_refs", return_value={"v2": B, "v3": C}),
                patch.object(registry, "data", side_effect=metadata),
                patch.object(sources, "commit_time", side_effect=commit_date),
            ):
                selected = sources.select_action(
                    "sample/action",
                    A,
                    {"kind": "release", "major": 1},
                    {"minimum_age_days": age},
                    NOW,
                )
            self.assertEqual((selected["revision"], selected["version"]), (B, "2.0.0"))

    def test_post_anchor_alias_excludes_whole_version_even_with_exception(self):
        for age in (0, 30):
            for post_anchor_tag in ("v3", "v3.0.0"):
                for exception in (False, True):
                    policy = {"minimum_age_days": age}
                    if exception:
                        policy["exceptions"] = [
                            {
                                "package": "github:sample/action",
                                "version": "3.0.0",
                                "minimum_safe": "3.0.0",
                                "reason": "security fix",
                                "advisory": "TEST-1",
                                "expires": (NOW + timedelta(days=1)).isoformat(),
                            }
                        ]
                    looked_up = []

                    def metadata(url):
                        if url.endswith("page=1"):
                            return [self.publication("v2", MATURE)]
                        tag = url.rsplit("/", 1)[1]
                        self.assertIn(tag, ("v3", "v3.0.0"))
                        looked_up.append(tag)
                        date = (
                            NOW + timedelta(minutes=1)
                            if tag == post_anchor_tag
                            else OLD
                        )
                        return self.publication(tag, date)

                    def commit_date(repository, commit):
                        self.assertNotEqual(
                            commit,
                            C,
                            "Every alias of the excluded version is ineligible",
                        )
                        return OLD

                    with (
                        self.subTest(
                            minimum_age_days=age,
                            post_anchor_tag=post_anchor_tag,
                            exception=exception,
                        ),
                        patch.object(
                            registry,
                            "observation_time",
                            return_value=NOW + timedelta(minutes=2),
                        ),
                        patch.object(
                            sources,
                            "action_refs",
                            return_value={"v2": B, "v3": C, "v3.0.0": C},
                        ),
                        patch.object(registry, "data", side_effect=metadata),
                        patch.object(sources, "commit_time", side_effect=commit_date),
                    ):
                        if exception:
                            with self.assertRaisesRegex(
                                ValueError, "No eligible stable release"
                            ):
                                sources.select_action(
                                    "sample/action", A, {"kind": "release"}, policy, NOW
                                )
                        else:
                            selected = sources.select_action(
                                "sample/action",
                                A,
                                {"kind": "release", "major": 1},
                                policy,
                                NOW,
                            )
                            self.assertEqual(selected["revision"], B)
                    self.assertEqual(looked_up, ["v3", "v3.0.0"])

    def test_post_anchor_alias_does_not_hide_invalid_later_alias_metadata(self):
        observed = NOW + timedelta(minutes=2)
        for invalid, error in (
            (self.publication("v4"), "mismatched GitHub release identity"),
            (
                self.publication("v3.0.0", published_at=None),
                "Missing registry publication age",
            ),
            (
                self.publication("v3.0.0", observed + timedelta(seconds=1)),
                "Future registry publication time",
            ),
        ):
            looked_up = []

            def metadata(url):
                if url.endswith("page=1"):
                    return [self.publication("v3", NOW + timedelta(minutes=1))]
                self.assertTrue(url.endswith("tags/v3.0.0"))
                looked_up.append("v3.0.0")
                return invalid

            with (
                self.subTest(error=error),
                patch.object(registry, "observation_time", return_value=observed),
                patch.object(
                    sources, "action_refs", return_value={"v3": C, "v3.0.0": C}
                ),
                patch.object(registry, "data", side_effect=metadata),
                patch.object(
                    sources,
                    "commit_time",
                    side_effect=AssertionError(
                        "Invalid metadata reached commit binding"
                    ),
                ),
                self.assertRaisesRegex(ValueError, error),
            ):
                sources.select_action("sample/action", A, {"kind": "release"}, {}, NOW)
            self.assertEqual(looked_up, ["v3.0.0"])

    def test_future_publication_commit_and_wrong_commit_identity_fail(self):
        future = NOW + timedelta(seconds=1)
        for publication, commit_date, observed, error in (
            (future, OLD, NOW, "Future registry publication time"),
            (
                OLD,
                future,
                NOW + timedelta(seconds=2),
                "Actions selected commit has future age evidence",
            ),
        ):
            with (
                self.subTest(publication=publication, commit_date=commit_date),
                patch.object(registry, "observation_time", return_value=observed),
                patch.object(sources, "action_refs", return_value={"v2": B}),
                patch.object(
                    registry, "data", return_value=[self.publication("v2", publication)]
                ),
                patch.object(sources, "commit_time", return_value=commit_date),
                self.assertRaisesRegex(ValueError, error),
            ):
                sources.select_action("sample/action", A, {"kind": "release"}, {}, NOW)
        with (
            patch.object(sources, "action_refs", return_value={"v2": B}),
            patch.object(
                registry, "data", side_effect=[[self.publication("v2")], {"sha": C}]
            ),
            self.assertRaisesRegex(ValueError, "different commit identity"),
        ):
            sources.select_action("sample/action", A, {"kind": "release"}, {}, NOW)

    def test_newer_current_release_is_retained(self):
        with (
            patch.object(sources, "action_refs", return_value={"v2": B}),
            patch.object(
                registry, "data", return_value=[self.publication("v2", MATURE)]
            ),
            patch.object(
                sources,
                "commit_time",
                side_effect=lambda r, c: YOUNG if c == C else OLD,
            ),
        ):
            selected = sources.select_action(
                "sample/action", C, {"kind": "release", "major": 2}, {}, NOW
            )
        self.assertEqual(
            selected, {"revision": C, "reason": "retained newer current release"}
        )

    def test_exceptions_retire_only_after_a_compatible_mature_commit_exists(self):
        policy = {
            "exceptions": [
                {
                    "package": "github:sample/action",
                    "version": "3.0.0",
                    "minimum_safe": "2.0.0",
                    "reason": "security fix",
                    "advisory": "TEST-1",
                    "expires": (NOW - timedelta(days=1)).isoformat(),
                }
            ]
        }
        batch = [self.publication("v3", YOUNG), self.publication("v2", OLD)]
        with (
            patch.object(sources, "action_refs", return_value={"v2": B, "v3": C}),
            patch.object(registry, "data", return_value=batch),
        ):
            for mature, expected in ((YOUNG, "Expired"), (MATURE, None)):
                with patch.object(
                    sources,
                    "commit_time",
                    side_effect=lambda r, c, at=mature: {A: OLD, B: at, C: YOUNG}[c],
                ):
                    if expected:
                        with self.assertRaisesRegex(ValueError, expected):
                            sources.select_action(
                                "sample/action",
                                A,
                                {"kind": "release", "major": 1},
                                policy,
                                NOW,
                            )
                    else:
                        selected = sources.select_action(
                            "sample/action",
                            A,
                            {"kind": "release", "major": 1},
                            policy,
                            NOW,
                        )
                        self.assertEqual(selected["revision"], B)
            constrained = {
                **policy,
                "constraints": {
                    "github:sample/action": {"range": ">=3", "reason": "API"}
                },
            }
            with (
                patch.object(sources, "commit_time", return_value=YOUNG),
                self.assertRaisesRegex(ValueError, "Expired"),
            ):
                sources.select_action(
                    "sample/action", A, {"kind": "release"}, constrained, NOW
                )
            active = copy.deepcopy(policy)
            active["exceptions"][0]["expires"] = (NOW + timedelta(days=1)).isoformat()
            with patch.object(sources, "commit_time", return_value=YOUNG):
                selected = sources.select_action(
                    "sample/action", A, {"kind": "release", "major": 1}, active, NOW
                )
            self.assertEqual(selected["revision"], C)
            malformed = copy.deepcopy(policy)
            malformed["exceptions"][0]["expires"] = "missing"
            with self.assertRaises(ValueError):
                sources.select_action(
                    "sample/action", A, {"kind": "release"}, malformed, NOW
                )


class OCITests(Fixture):
    def setUp(self):
        super().setUp()
        self.spec = {"adapter": "oci", "file": "images.json", "pointer": ["images"]}
        self.entry = {
            "repository": "sample/tool",
            "tag": "1.2.3-alpine",
            "digest": DA,
            "versionSource": "dockerHub",
            "envVar": "IMAGE_TOOL",
        }
        self.file = self.json("images.json", {"images": {"tool": self.entry}})

    def candidates(self, values):
        mocked = patch.object(sources, "oci_candidates", return_value=values)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_variant_precision_and_strict_compatible(self):
        self.candidates(
            [
                registry.Release("1.2.4-alpine", MATURE, DB),
                registry.Release("2.0.0-alpine", MATURE, DB),
                registry.Release("9.0.0-bookworm", OLD, DB),
                registry.Release("9-alpine", OLD, DB),
            ]
        )
        before = sources.snapshot(self.root, self.spec)
        selected = sources.resolve(
            self.root, {**self.spec, "mode": "compatible"}, {}, NOW
        )
        self.assertEqual(selected["selected"]["tool"]["tag"], "1.2.4-alpine")
        sources.audit(self.root, {**self.spec, "mode": "compatible"}, before, {}, NOW)
        self.assertEqual(
            json.loads(self.file.read_text())["images"]["tool"]["envVar"], "IMAGE_TOOL"
        )

    def test_same_tag_new_digest_requires_its_own_maturity(self):
        self.candidates([registry.Release("1.2.3-alpine", YOUNG, DB)])
        with self.assertRaisesRegex(ValueError, "eligible"):
            sources.resolve(self.root, self.spec, {}, NOW)
        self.assertEqual(
            json.loads(self.file.read_text())["images"]["tool"]["digest"], DA
        )

    def test_mature_retag_is_pinned_and_hook_swap_is_rejected(self):
        self.candidates([registry.Release("1.2.3-alpine", MATURE, DB)])
        before = sources.snapshot(self.root, self.spec)
        sources.resolve(self.root, self.spec, {}, NOW)
        sources.audit(self.root, self.spec, before, {}, NOW)
        content = json.loads(self.file.read_text())
        content["images"]["tool"]["digest"] = DA
        self.json("images.json", content)
        with self.assertRaisesRegex(ValueError, "identity"):
            sources.audit(self.root, self.spec, before, {}, NOW)

    def test_hub_uses_manifest_digest_and_bounded_same_repository_pagination(self):
        tag = {"name": "1.2.3-alpine", "last_updated": MATURE.isoformat(), "digest": DB}
        with patch.object(
            registry, "data", return_value={"results": [tag], "next": None}
        ):
            chosen = sources.oci_candidates("sample/tool", "dockerHub")[0]
            self.assertEqual(chosen.identity, DB)
        with (
            patch.object(
                registry,
                "data",
                return_value={
                    "results": [tag],
                    "next": "https://hub.docker.com/v2/repositories/other/tool/tags?page=2",
                },
            ),
            self.assertRaisesRegex(ValueError, "pagination"),
        ):
            sources.oci_candidates("sample/tool", "dockerHub")

    def test_gcr_dated_digest_and_conflicting_tag_negative(self):
        item = {
            "tag": ["1.2.3-alpine"],
            "timeUploadedMs": str(int(MATURE.timestamp() * 1000)),
        }
        with patch.object(registry, "data", return_value={"manifest": {DB: item}}):
            release = sources.oci_candidates("gcr.io/sample/tool", "gcr")[0]
            self.assertEqual((release.identity, release.published), (DB, MATURE))
        with (
            patch.object(
                registry, "data", return_value={"manifest": {DA: item, DB: item}}
            ),
            self.assertRaisesRegex(ValueError, "conflicting"),
        ):
            sources.oci_candidates("gcr.io/sample/tool", "gcr")

    def test_dated_digestless_inventory_does_not_hide_selected_evidence_gaps(self):
        old = {"name": "1.0.0-alpine", "last_updated": OLD.isoformat(), "digest": None}
        valid = {
            "name": "2.0.0-alpine",
            "last_updated": MATURE.isoformat(),
            "digest": DB,
        }
        young = {
            "name": "3.0.0-alpine",
            "last_updated": YOUNG.isoformat(),
            "digest": None,
        }
        with patch.object(
            registry, "data", return_value={"results": [old, valid, young]}
        ):
            selected = sources.select_oci(self.entry, self.spec, {}, NOW)
            self.assertEqual((selected["tag"], selected["digest"]), (valid["name"], DB))
        # The latest eligible tag cannot disappear from ranking for lack of a hash.
        with (
            patch.object(
                registry,
                "data",
                return_value={
                    "results": [
                        old,
                        valid,
                        {**young, "last_updated": MATURE.isoformat()},
                    ]
                },
            ),
            self.assertRaisesRegex(ValueError, "digest"),
        ):
            sources.resolve(self.root, self.spec, {}, NOW)
        self.assertEqual(
            json.loads(self.file.read_text())["images"]["tool"], self.entry
        )

    def test_docker_missing_dates_and_malformed_digest_are_never_ignored(self):
        valid = {
            "name": "2.0.0-alpine",
            "last_updated": MATURE.isoformat(),
            "digest": DB,
        }
        for invalid in (
            {"name": "1.0.0-alpine", "digest": None},
            {"name": "1.0.0-alpine", "last_updated": OLD.isoformat(), "digest": ""},
            {
                "name": "1.0.0-alpine",
                "last_updated": OLD.isoformat(),
                "digest": "latest",
            },
        ):
            with (
                self.subTest(invalid=invalid),
                patch.object(
                    registry, "data", return_value={"results": [invalid, valid]}
                ),
                self.assertRaises(ValueError),
            ):
                sources.select_oci(self.entry, self.spec, {}, NOW)

    def test_security_age_exception_does_not_exempt_selected_manifest_digest(self):
        policy = {
            "exceptions": [
                {
                    "package": "docker:sample/tool",
                    "version": "2.0.0",
                    "minimum_safe": "2.0.0",
                    "reason": "Required fix",
                    "advisory": "https://example.org/advisories/fix",
                    "expires": (NOW + timedelta(days=7)).isoformat(),
                }
            ]
        }
        self.candidates([registry.Release("2.0.0-alpine", YOUNG, "")])
        with self.assertRaisesRegex(ValueError, "digest"):
            sources.select_oci(self.entry, self.spec, policy, NOW)

    def test_newer_current_image_retention_does_not_claim_older_digest_evidence(self):
        self.candidates([registry.Release("1.0.0-alpine", OLD, "")])
        selected = sources.select_oci(self.entry, self.spec, {}, NOW)
        self.assertEqual(
            selected, {**self.entry, "reason": "retained newer immutable image"}
        )


class NixEvidenceTests(Fixture):
    def test_non_object_and_missing_hash_evidence_fail_explicitly(self):
        for value in ("/nix/store/example", {}, {"narHash": "invalid"}):
            with (
                self.subTest(value=value),
                patch.object(
                    sources.chainman,
                    "execute",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(value)),
                ),
                self.assertRaisesRegex(ValueError, "SHA-256 content hash"),
            ):
                sources.nix_tree(self.root, {}, "sample/packages", A)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_NIX_NATIVE") == "1",
        "explicit real Nix GitHub source-evidence lane",
    )
    def test_real_nix_fetch_tree_keeps_hash_instead_of_coercing_to_store_path(self):
        evidence = sources.nix_tree(
            self.root,
            {},
            "NixOS/nixpkgs",
            "30f0d59baf33968c9104a7fa2ec87406d63c5fbf",
        )
        self.assertEqual(
            evidence,
            {"narHash": "sha256-oidxGOnOJ9zUXQut4kXbokyQyd/gonADFo0YwNBE/5k="},
        )


class NixTests(Fixture):
    def setUp(self):
        super().setUp()
        self.write("flake.nix", "{}\n")
        self.spec = {
            "adapter": "nix",
            "profile": "core",
            "inputs": [
                {
                    "directory": ".",
                    "input": "pkgs",
                    "repository": "sample/packages",
                    "branch": "stable",
                },
                {
                    "directory": ".",
                    "input": "overlay",
                    "repository": "sample/overlay",
                    "branch": "main",
                },
            ],
        }
        self.lock = {
            "version": 7,
            "root": "root",
            "nodes": {
                "root": {"inputs": {"pkgs": "pkgs", "overlay": "overlay"}},
                **{
                    name: {
                        "locked": {
                            "type": "github",
                            "owner": "sample",
                            "repo": repo,
                            "rev": A,
                            "lastModified": int(OLD.timestamp()),
                            "narHash": SRI,
                        },
                        "original": {
                            "owner": "sample",
                            "repo": repo,
                            "ref": "main",
                            "type": "github",
                        },
                    }
                    for name, repo in [("pkgs", "packages"), ("overlay", "overlay")]
                },
            },
        }
        self.json("flake.lock", self.lock)
        selected = patch.object(
            sources,
            "nix_candidate",
            return_value={"revision": B, "published": MATURE.isoformat()},
        )
        selected.start()
        self.addCleanup(selected.stop)
        times = patch.object(
            sources,
            "commit_time",
            side_effect=lambda repository, commit: OLD if commit == A else MATURE,
        )
        times.start()
        self.addCleanup(times.stop)
        tree = patch.object(sources, "nix_tree", return_value={"narHash": SRI})
        tree.start()
        self.addCleanup(tree.stop)

    def apply_selection(self, *_args, **_kwargs):
        changed = copy.deepcopy(self.lock)
        for key in ("pkgs", "overlay"):
            changed["nodes"][key]["locked"].update(
                rev=B, lastModified=int(MATURE.timestamp())
            )
        self.json("flake.lock", changed)

    def test_multiple_inputs_are_exact_coordinated_overrides(self):
        before = sources.snapshot(self.root, self.spec)
        with patch.object(
            sources.chainman, "execute", side_effect=self.apply_selection
        ) as run:
            result = sources.resolve(self.root, self.spec, {}, NOW)
        self.assertEqual(result["changed"], ["flake.lock"])
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[2]
        self.assertEqual(argv.count("--override-input"), 2)
        self.assertNotIn("update", argv)
        self.assertIn("github:sample/packages/" + B, argv)
        sources.audit(self.root, self.spec, before, {}, NOW)

    def test_hook_identity_and_undeclared_structure_changes_fail(self):
        before = sources.snapshot(self.root, self.spec)
        self.apply_selection()
        for mutate in (
            lambda lock: lock["nodes"]["pkgs"]["locked"].update(rev=C),
            lambda lock: lock["nodes"]["root"]["inputs"].update(extra="overlay"),
            lambda lock: lock["nodes"]["pkgs"].update(inputs={"injected": "overlay"}),
            lambda lock: lock["nodes"]["pkgs"]["original"].update(repo="unrelated"),
            lambda lock: lock["nodes"]["pkgs"]["locked"].update(dir="unexpected"),
            lambda lock: lock["nodes"]["pkgs"]["locked"].update(
                narHash="sha256-" + base64.b64encode(b"b" * 32).decode()
            ),
        ):
            self.apply_selection()
            content = json.loads((self.root / "flake.lock").read_text())
            mutate(content)
            self.json("flake.lock", content)
            with self.assertRaisesRegex(ValueError, "Nix"):
                sources.audit(self.root, self.spec, before, {}, NOW)

    def test_selected_input_dependency_structure_cannot_be_removed(self):
        self.lock["nodes"]["pkgs"]["inputs"] = {"overlay": "overlay"}
        self.json("flake.lock", self.lock)
        before = sources.snapshot(self.root, self.spec)
        self.apply_selection()
        content = json.loads((self.root / "flake.lock").read_text())
        del content["nodes"]["pkgs"]["inputs"]
        self.json("flake.lock", content)
        with self.assertRaisesRegex(ValueError, "dependency structure"):
            sources.audit(self.root, self.spec, before, {}, NOW)

    def test_exact_override_original_is_a_permitted_nix_representation(self):
        before = sources.snapshot(self.root, self.spec)
        self.apply_selection()
        content = json.loads((self.root / "flake.lock").read_text())
        content["nodes"]["pkgs"]["original"] = {
            "type": "github",
            "owner": "sample",
            "repo": "packages",
            "rev": B,
        }
        self.json("flake.lock", content)
        sources.audit(self.root, self.spec, before, {}, NOW)

    def test_missing_declared_source_and_symlink_fail_before_execution(self):
        self.spec["inputs"][0]["repository"] = "other/packages"
        with (
            patch.object(sources.chainman, "execute") as run,
            self.assertRaisesRegex(ValueError, "source repository"),
        ):
            sources.resolve(self.root, self.spec, {}, NOW)
        run.assert_not_called()


class GoTests(Fixture):
    def setUp(self):
        super().setUp()
        self.spec = {"adapter": "go", "directories": ["module"], "mode": "compatible"}
        self.write("module/go.mod", "module example.test/local\n\ngo 1.20\n")
        self.module = {
            "Module": {"Path": "example.test/local"},
            "Require": [{"Path": "example.test/library", "Version": "v1.2.0"}],
        }
        self.before = {
            "adapter": "go",
            "members": {"module": self.module},
            "workspaces": {},
            "identities": [],
        }

    def test_local_module_identity_uses_go_import_path_rules(self):
        for module in (
            "api",
            "local_app",
            "local+tool",
            "Example/local.v1",
            ".local/api",
        ):
            with self.subTest(module=module):
                source_go.local_module_path(module)
        for module in (
            None,
            "",
            "-local",
            "/local",
            "local/",
            "local//api",
            "local/..",
            "local/...",
            "local/end.",
            "local/CON.txt",
            "local/lpt9",
            "local/x~12.go",
            "local/a b",
            "local/@v",
            "local/%2f",
            "local\\api",
            "local/é",
        ):
            with (
                self.subTest(module=module),
                self.assertRaisesRegex(ValueError, "local Go"),
            ):
                source_go.local_module_path(module)

    def test_local_names_never_relax_remote_queries_or_checksums(self):
        for module in ("api", "local_app", "local+tool"):
            with (
                self.subTest(module=module),
                patch.object(source_go, "native_json") as native,
                self.assertRaisesRegex(ValueError, "public Go"),
            ):
                source_go.query(self.root, self.spec, module, "latest")
            native.assert_not_called()
            self.write(
                "module/go.sum",
                module + " v1.2.0 h1:" + base64.b64encode(b"x" * 32).decode() + "\n",
            )
            with self.assertRaisesRegex(ValueError, "public Go"):
                source_go.checksum_identities(self.root, {"module": self.module}, {})
        self.assertEqual(
            registry.go_path("example.test/library"), "example.test/library"
        )

    def test_undeclared_local_requirement_fails_before_resolution_or_native_audit(self):
        state = copy.deepcopy(self.before)
        state["members"]["module"]["Require"][0]["Path"] = "undeclared_local"
        with (
            patch.object(source_go, "snapshot", return_value=state),
            patch.object(source_go, "go_candidates") as candidates,
            patch.object(source_go, "execute") as native,
        ):
            with self.assertRaisesRegex(ValueError, "public Go"):
                source_go.resolve(self.root, self.spec, {}, NOW)
            with self.assertRaisesRegex(ValueError, "public Go"):
                source_go.audit(self.root, self.spec, state, {}, NOW)
        candidates.assert_not_called()
        native.assert_not_called()

    def test_compatible_never_falls_through_to_a_major(self):
        candidates = [registry.Release("v1.3.0", OLD), registry.Release("v2.0.0", OLD)]
        with (
            patch.object(source_go, "go_candidates", return_value=candidates),
            self.assertRaisesRegex(ValueError, "eligible"),
        ):
            source_go.select(
                self.root, self.spec, "example.test/library", "v1.2.0", {}, NOW
            )

    def test_security_floor_applies_to_baseline_requirements_and_all_checksums(self):
        policy = {
            "exceptions": [
                {
                    "package": "go:example.test/library",
                    "version": "v1.2.2",
                    "minimum_safe": "v1.2.2",
                    "reason": "Required security repair",
                    "advisory": "https://example.invalid/advisory",
                    "expires": "2030-01-01T00:00:00Z",
                }
            ]
        }
        for kind in (
            "requirement",
            "baseline-checksum",
            "new-checksum",
            "pseudoversion",
        ):
            before = copy.deepcopy(self.before)
            after = copy.deepcopy(self.before)
            if kind != "requirement":
                before["members"]["module"]["Require"] = []
                after["members"]["module"]["Require"] = []
                value = (
                    "v1.2.2-0.20260101000000-aaaaaaaaaaaa"
                    if kind == "pseudoversion"
                    else "v1.2.0"
                )
                identity = [
                    "example.test/library",
                    value,
                    "https://proxy.golang.org/example.test/library/@v/"
                    + value
                    + ".zip",
                    "h1:" + base64.b64encode(b"x" * 32).decode(),
                ]
                after["identities"] = [identity]
                if kind == "baseline-checksum":
                    before["identities"] = [identity]
            with (
                self.subTest(kind=kind),
                patch.object(source_go, "snapshot", return_value=after),
                self.assertRaisesRegex(ValueError, "safe floor"),
            ):
                source_go.audit(self.root, self.spec, before, policy, NOW)

    def test_native_metadata_rejects_worktree_escape_and_remote_replacement(self):
        body = copy.deepcopy(self.module)
        body["Replace"] = [
            {
                "Old": {"Path": "example.test/library"},
                "New": {"Path": "example.test/other", "Version": "v1.2.0"},
            }
        ]
        with (
            patch.object(source_go, "native_json", return_value=body),
            self.assertRaisesRegex(ValueError, "Remote"),
        ):
            source_go.snapshot(self.root, self.spec)
        with self.assertRaisesRegex(ValueError, "escapes"):
            source_go.local_directory(self.root, self.root, "../outside")

    def test_unchanged_go_requirement_needs_valid_exception_expiry_and_retirement_evidence(
        self,
    ):
        exception = {
            "package": "go:example.test/library",
            "version": "v1.2.0",
            "minimum_safe": "v1.2.0",
            "reason": "Required security repair",
            "advisory": "https://example.invalid/advisory",
            "expires": NOW.isoformat(),
        }
        for published, accepted in [(YOUNG, False), (OLD, True)]:
            with (
                self.subTest(published=published),
                patch.object(source_go, "snapshot", return_value=self.before),
                patch.object(
                    source_go,
                    "go_candidates",
                    return_value=[registry.Release("v1.2.0", published)],
                ),
                patch.object(source_go, "execute"),
            ):
                if accepted:
                    source_go.audit(
                        self.root,
                        self.spec,
                        self.before,
                        {"exceptions": [exception]},
                        NOW,
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "Expired"):
                        source_go.audit(
                            self.root,
                            self.spec,
                            self.before,
                            {"exceptions": [exception]},
                            NOW,
                        )
        with (
            patch.object(source_go, "snapshot", return_value=self.before),
            patch.object(
                source_go,
                "go_candidates",
                side_effect=AssertionError(
                    "Malformed policy must fail before fetching"
                ),
            ),
            self.assertRaises(ValueError),
        ):
            source_go.audit(
                self.root,
                self.spec,
                self.before,
                {"exceptions": [{**exception, "expires": "invalid"}]},
                NOW,
            )

    def test_new_transitive_and_reused_checksum_require_age_audit(self):
        checksum = "h1:" + base64.b64encode(b"x" * 32).decode()
        url = "https://proxy.golang.org/example.test/library/@v/v1.2.1.zip"
        identity = ["example.test/library", "v1.2.1", url, checksum]
        after = copy.deepcopy(self.before)
        after["members"]["module"]["Require"][0]["Version"] = "v1.2.1"
        after["identities"] = [identity]
        for reuse in (False, True):
            before = copy.deepcopy(self.before)
            if reuse:
                before["identities"] = [identity]
            with (
                self.subTest(reused=reuse),
                patch.object(source_go, "snapshot", return_value=after),
                patch.object(source_go, "query", return_value={}),
                patch.object(
                    registry,
                    "go_artifacts",
                    return_value=(registry.Artifact(url, checksum, YOUNG),),
                ),
                patch.object(source_go, "go_candidates", return_value=[]),
                self.assertRaisesRegex(ValueError, "mature"),
            ):
                source_go.audit(self.root, self.spec, before, {}, NOW)

    def test_checksum_mismatch_and_compatible_transitive_escape(self):
        checksum = "h1:" + base64.b64encode(b"x" * 32).decode()
        url = "https://proxy.golang.org/example.test/library/@v/v1.3.0.zip"
        after = copy.deepcopy(self.before)
        after["identities"] = [["example.test/library", "v1.3.0", url, checksum]]
        with (
            patch.object(source_go, "snapshot", return_value=after),
            patch.object(source_go, "query", return_value={}),
            patch.object(
                registry,
                "go_artifacts",
                return_value=(registry.Artifact(url, checksum, OLD),),
            ),
            self.assertRaisesRegex(ValueError, "compatible"),
        ):
            source_go.audit(self.root, self.spec, self.before, {}, NOW)
        url = url.replace("v1.3.0", "v1.2.1")
        after["identities"] = [["example.test/library", "v1.2.1", url, checksum]]
        with (
            patch.object(source_go, "snapshot", return_value=after),
            patch.object(source_go, "query", return_value={}),
            patch.object(
                registry,
                "go_artifacts",
                return_value=(registry.Artifact(url, "wrong", OLD),),
            ),
            self.assertRaisesRegex(ValueError, "checksum"),
        ):
            source_go.audit(self.root, self.spec, self.before, {}, NOW)

    def test_audit_runs_native_verification_and_refuses_self_repair(self):
        checksum = "h1:" + base64.b64encode(b"x" * 32).decode()
        url = "https://proxy.golang.org/example.test/library/@v/v1.2.1.zip"
        after = copy.deepcopy(self.before)
        after["identities"] = [["example.test/library", "v1.2.1", url, checksum]]
        with (
            patch.object(source_go, "snapshot", return_value=after),
            patch.object(source_go, "query", return_value={}),
            patch.object(
                registry,
                "go_artifacts",
                return_value=(registry.Artifact(url, checksum, OLD),),
            ),
            patch.object(source_go, "execute") as run,
        ):
            source_go.audit(self.root, self.spec, self.before, {}, NOW)
            self.assertEqual(
                [call.args[2] for call in run.call_args_list],
                [["go", "mod", "download"], ["go", "mod", "verify"]],
            )
        with (
            patch.object(source_go, "snapshot", side_effect=[after, self.before]),
            patch.object(source_go, "query", return_value={}),
            patch.object(
                registry,
                "go_artifacts",
                return_value=(registry.Artifact(url, checksum, OLD),),
            ),
            patch.object(source_go, "execute"),
            self.assertRaisesRegex(ValueError, "verification changed"),
        ):
            source_go.audit(self.root, self.spec, self.before, {}, NOW)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_GO_NATIVE") == "1", "explicit native Go lane"
    )
    def test_real_go_workspace_no_dependencies(self):
        self.write("workspace/go.work", "go 1.20\nuse ./member\n")
        self.write("workspace/member/go.mod", "module example.test/member\n\ngo 1.20\n")
        self.write("workspace/member/main.go", "package main\nfunc main() {}\n")
        spec = {"adapter": "go", "directories": ["workspace"]}
        before = sources.snapshot(self.root, spec)
        result = sources.resolve(self.root, spec, {}, NOW)
        self.assertEqual(result["changed"], [])
        sources.audit(self.root, spec, before, {}, NOW)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_GO_NATIVE") == "1", "explicit native Go lane"
    )
    def test_real_go_workspace_with_declared_local_names_and_replacement(self):
        self.write("workspace/go.work", "go 1.20\n\nuse (\n\t./app\n\t./lib\n)\n")
        self.write(
            "workspace/app/go.mod",
            "module api+cli\n\ngo 1.20\n\nrequire local_lib v0.0.0\n\nreplace local_lib => ../lib\n",
        )
        self.write(
            "workspace/app/main.go",
            'package main\nimport "local_lib"\nfunc main() { _ = local_lib.Value }\n',
        )
        self.write("workspace/lib/go.mod", "module local_lib\n\ngo 1.20\n")
        self.write("workspace/lib/value.go", "package local_lib\nconst Value = 1\n")
        spec = {"adapter": "go", "directories": ["workspace"]}
        with patch.object(
            source_go, "query", side_effect=AssertionError("local source fetched")
        ):
            before = sources.snapshot(self.root, spec)
            self.assertEqual(before["identities"], [])
            result = sources.resolve(self.root, spec, {}, NOW)
            self.assertEqual(result["changed"], [])
            sources.audit(self.root, spec, before, {}, NOW)
            source_go.execute(
                self.root,
                spec,
                ["go", "test", "./..."],
                directory=self.root / "workspace/app",
                workspace="workspace/go.work",
            )
        with self.assertRaisesRegex(ValueError, "not a declared workspace module"):
            sources.snapshot(
                self.root, {"adapter": "go", "directories": ["workspace/app"]}
            )


class ToolchainTests(Fixture):
    def setUp(self):
        super().setUp()
        self.json("package.json", {"engines": {"node": "20.x"}})
        self.write(".runtime-version", "v20.0.0\n")
        self.spec = {
            "adapter": "toolchain",
            "profile": "core",
            "tools": [
                {
                    "provider": "github",
                    "name": "sample/runtime",
                    "command": ["runtime", "--version"],
                    "version_pattern": r"^v(?P<version>\d+\.\d+\.\d+)$",
                    "pins": [
                        {
                            "file": "package.json",
                            "pointer": ["engines", "node"],
                            "value": "{major}.x",
                        },
                        {
                            "file": ".runtime-version",
                            "format": "regex",
                            "pattern": r"^(?P<value>v\d+\.\d+\.\d+)$",
                            "value": "v{version}",
                        },
                    ],
                }
            ],
        }
        versions = patch.object(source_toolchain, "probe", return_value="22.1.0")
        versions.start()
        self.addCleanup(versions.stop)
        self.versions = versions
        releases = patch.object(
            registry, "releases", return_value=[registry.Release("v22.1.0", MATURE)]
        )
        releases.start()
        self.addCleanup(releases.stop)
        commits = patch.object(registry, "github_commit", return_value=B)
        commits.start()
        self.addCleanup(commits.stop)
        times = patch.object(sources, "commit_time", return_value=MATURE)
        times.start()
        self.addCleanup(times.stop)

    def test_exact_sdk_evidence_drives_declared_pins(self):
        before = sources.snapshot(self.root, self.spec)
        result = sources.resolve(self.root, self.spec, {}, NOW)
        before["resolution"] = result
        self.assertEqual(
            json.loads((self.root / "package.json").read_text())["engines"]["node"],
            "22.x",
        )
        self.assertEqual((self.root / ".runtime-version").read_text(), "v22.1.0\n")
        sources.audit(self.root, self.spec, before, {}, NOW)

    def current_pins(self):
        self.json("package.json", {"engines": {"node": "22.x"}})
        self.write(".runtime-version", "v22.1.0\n")

    def npm_release(self, *, digest_byte=b"a", published=YOUNG, value="22.1.0"):
        self.spec["tools"][0].update(provider="npm", name="sample-sdk")
        return registry.Release(
            value,
            OLD,
            artifacts=(
                registry.Artifact(
                    f"https://registry.npmjs.org/sample-sdk/-/sample-sdk-{value}.tgz",
                    "sha512:" + (digest_byte * 64).hex(),
                    published,
                ),
            ),
        )

    def test_same_young_sdk_and_all_pins_retain_complete_artifact_evidence(self):
        self.current_pins()
        release = self.npm_release()
        with patch.object(registry, "releases", return_value=[release]):
            before = sources.snapshot(self.root, self.spec)
            result = sources.resolve(self.root, self.spec, {}, NOW, before=before)
            self.assertEqual(result["changed"], [])
            self.assertEqual(
                result["tools"][0]["artifacts"][0][1], release.artifacts[0].digest
            )
            before["resolution"] = result
            sources.audit(self.root, self.spec, before, {}, NOW)

    def test_same_version_changed_young_artifact_is_not_grandfathered(self):
        self.current_pins()
        with patch.object(registry, "releases", return_value=[self.npm_release()]):
            before = sources.snapshot(self.root, self.spec)
        with (
            patch.object(
                registry, "releases", return_value=[self.npm_release(digest_byte=b"b")]
            ),
            self.assertRaisesRegex(ValueError, "sample-sdk@22.1.0.*eligible"),
        ):
            sources.resolve(self.root, self.spec, {}, NOW, before=before)

    def test_mature_artifact_changes_after_resolution_fail_audit(self):
        self.current_pins()
        with patch.object(
            registry, "releases", return_value=[self.npm_release(published=OLD)]
        ):
            before = sources.snapshot(self.root, self.spec)
            before["resolution"] = sources.resolve(
                self.root, self.spec, {}, NOW, before=before
            )
        with (
            patch.object(
                registry,
                "releases",
                return_value=[self.npm_release(digest_byte=b"b", published=OLD)],
            ),
            self.assertRaisesRegex(ValueError, "changed after resolution"),
        ):
            sources.audit(self.root, self.spec, before, {}, NOW)

    def test_same_rendered_major_does_not_grandfather_changed_actual_sdk(self):
        self.current_pins()
        self.spec["tools"][0]["pins"] = self.spec["tools"][0]["pins"][:1]
        with patch.object(registry, "releases", return_value=[self.npm_release()]):
            before = sources.snapshot(self.root, self.spec)
        with (
            patch.object(
                registry, "releases", return_value=[self.npm_release(value="22.2.0")]
            ),
            patch.object(source_toolchain, "probe", return_value="22.2.0"),
            self.assertRaisesRegex(ValueError, "eligible"),
        ):
            sources.resolve(self.root, self.spec, {}, NOW, before=before)

    def test_retained_sdk_cannot_bypass_constraints_safe_floor_or_expiry(self):
        self.current_pins()
        release = self.npm_release()
        exception = {
            "package": "npm:sample-sdk",
            "version": "23.0.0",
            "minimum_safe": "23.0.0",
            "reason": "Required security repair",
            "advisory": "https://example.invalid/advisory",
            "expires": (NOW + timedelta(days=1)).isoformat(),
        }
        with patch.object(registry, "releases", return_value=[release]):
            before = sources.snapshot(self.root, self.spec)
            for policy in (
                {
                    "constraints": {
                        "npm:sample-sdk": {
                            "range": "<22",
                            "reason": "Required runtime API",
                        }
                    }
                },
                {"exceptions": [exception]},
                {
                    "exceptions": [
                        {
                            **exception,
                            "version": "22.1.0",
                            "minimum_safe": "22.1.0",
                            "expires": NOW.isoformat(),
                        }
                    ]
                },
            ):
                with self.subTest(policy=policy), self.assertRaises(ValueError):
                    sources.resolve(self.root, self.spec, policy, NOW, before=before)

    def test_retained_github_sdk_requires_same_commit(self):
        self.current_pins()
        with (
            patch.object(
                registry, "releases", return_value=[registry.Release("v22.1.0", YOUNG)]
            ),
            patch.object(sources, "commit_time", return_value=YOUNG),
        ):
            before = sources.snapshot(self.root, self.spec)
            self.assertEqual(
                sources.resolve(self.root, self.spec, {}, NOW, before=before)[
                    "changed"
                ],
                [],
            )
            with (
                patch.object(registry, "github_commit", return_value=C),
                self.assertRaisesRegex(ValueError, "eligible"),
            ):
                sources.resolve(self.root, self.spec, {}, NOW, before=before)

    def test_sdk_baseline_requires_artifacts_and_current_dates(self):
        self.current_pins()
        self.npm_release()
        with (
            patch.object(
                registry, "releases", return_value=[registry.Release("22.1.0", OLD)]
            ),
            self.assertRaisesRegex(ValueError, "immutable release artifacts"),
        ):
            sources.snapshot(self.root, self.spec)
        with (
            patch.object(registry, "observation_time", return_value=NOW),
            patch.object(
                registry,
                "releases",
                side_effect=lambda *args, **kwargs: [
                    self.npm_release(published=NOW + timedelta(days=1))
                ],
            ),
            self.assertRaisesRegex(ValueError, "Future"),
        ):
            sources.snapshot(self.root, self.spec)

    def test_observed_post_anchor_sdk_remains_policy_ineligible(self):
        self.current_pins()
        with patch.object(
            registry, "observation_time", return_value=NOW + timedelta(days=2)
        ):
            release = self.npm_release(published=NOW + timedelta(days=1))
            with patch.object(registry, "releases", return_value=[release]):
                before = sources.snapshot(self.root, self.spec)
                for age in (0, 30):
                    with (
                        self.subTest(minimum_age_days=age),
                        self.assertRaisesRegex(
                            ValueError,
                            "violates its active compatibility or security policy",
                        ),
                    ):
                        sources.resolve(
                            self.root,
                            self.spec,
                            {"minimum_age_days": age},
                            NOW,
                            before=before,
                        )
        self.assertEqual((self.root / ".runtime-version").read_text(), "v22.1.0\n")

    def test_immature_sdk_is_an_error_not_a_pin_downgrade(self):
        with (
            patch.object(
                registry, "releases", return_value=[registry.Release("v22.1.0", YOUNG)]
            ),
            self.assertRaisesRegex(ValueError, "eligible"),
        ):
            sources.resolve(self.root, self.spec, {}, NOW)
        self.assertEqual((self.root / ".runtime-version").read_text(), "v20.0.0\n")

    def test_older_nix_sdk_does_not_lower_project_pin(self):
        self.write(".runtime-version", "v24.0.0\n")
        with self.assertRaisesRegex(ValueError, "downgrade"):
            sources.resolve(self.root, self.spec, {}, NOW)

    def test_post_hook_changed_tool_and_pin_fail(self):
        before = sources.snapshot(self.root, self.spec)
        before["resolution"] = sources.resolve(self.root, self.spec, {}, NOW)
        self.write(".runtime-version", "v22.2.0\n")
        with self.assertRaisesRegex(ValueError, "pins differ"):
            sources.audit(self.root, self.spec, before, {}, NOW)
        with (
            patch.object(source_toolchain, "probe", return_value="22.2.0"),
            self.assertRaisesRegex(ValueError, "evidence"),
        ):
            sources.audit(self.root, self.spec, before, {}, NOW)


if __name__ == "__main__":
    unittest.main()
