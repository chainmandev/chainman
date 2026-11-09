"""Official source releases keep publication, commit and archive evidence distinct."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import source_updates as sources

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=100)
MATURE = NOW - timedelta(days=30)
YOUNG = NOW - timedelta(days=29)
A, B = "a" * 40, "b" * 40
SRI = "sha256-" + "A" * 43 + "="
PATTERN = r"^v(?P<version>\d+\.\d+\.\d+)$"
TEMPLATE = (
    "https://github.com/sample/library/releases/download/{tag}/library-{version}.tar.gz"
)


def release(tag, published):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "published_at": published.isoformat(),
    }


class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nix source fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "chainman.toml").write_text("schema=1\n")
        (self.root / "flake.nix").write_text("{}\n")
        self.item = {
            "input": "library",
            "repository": "sample/library",
            "tag_pattern": PATTERN,
        }
        self.spec = {"adapter": "nix", "inputs": [self.item]}
        self.lock = {
            "version": 7,
            "root": "root",
            "nodes": {
                "root": {"inputs": {"library": "library"}},
                "library": {
                    "flake": False,
                    "locked": {
                        "type": "github",
                        "owner": "sample",
                        "repo": "library",
                        "rev": A,
                        "lastModified": int(YOUNG.timestamp()),
                        "narHash": SRI,
                    },
                    "original": {
                        "type": "github",
                        "owner": "sample",
                        "repo": "library",
                        "rev": A,
                    },
                },
            },
        }
        self.write_lock(self.lock)
        for mocked in (
            patch.object(registry, "data", return_value=[release("v2.0.0", MATURE)]),
            patch.object(registry, "github_commit", return_value=B),
            patch.object(
                sources,
                "commit_time",
                side_effect=lambda repo, rev: YOUNG if rev == A else OLD,
            ),
            patch.object(sources, "nix_tree", return_value={"narHash": SRI}),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    def write_lock(self, lock):
        (self.root / "flake.lock").write_text(json.dumps(lock))

    def tarball(self):
        self.item["url_template"] = TEMPLATE
        self.lock["nodes"]["library"].update(
            locked={
                "type": "tarball",
                "url": TEMPLATE.format(tag="v1.0.0", version="1.0.0"),
                "narHash": SRI,
                "lastModified": int(OLD.timestamp()),
            },
            original={
                "type": "tarball",
                "url": TEMPLATE.format(tag="v1.0.0", version="1.0.0"),
            },
        )
        self.write_lock(self.lock)

    def test_explicit_release_replaces_newer_branch_baseline_and_keeps_both_dates(self):
        before = sources.nix_snapshot(self.root, self.spec)
        plan = sources.nix_plan(before, self.spec, {}, NOW)[0]
        self.assertEqual(plan["revision"], B)
        self.assertEqual(plan["published"], OLD.isoformat())
        self.assertEqual(plan["release_publication"], MATURE.isoformat())
        self.assertEqual(
            (plan["release_version"], plan["release_tag"]), ("2.0.0", "v2.0.0")
        )
        changed = copy.deepcopy(self.lock)
        changed["nodes"]["library"]["locked"].update(
            rev=B, lastModified=int(OLD.timestamp())
        )
        self.write_lock(changed)
        sources.audit_nix(self.root, self.spec, before, {}, NOW)
        changed["nodes"]["library"]["locked"]["lastModified"] = int(MATURE.timestamp())
        self.write_lock(changed)
        with self.assertRaisesRegex(ValueError, "timestamp/hash"):
            sources.audit_nix(self.root, self.spec, before, {}, NOW)

    def test_young_publication_cannot_mature_through_older_contents(self):
        with (
            patch.object(registry, "data", return_value=[release("v2.0.0", YOUNG)]),
            self.assertRaisesRegex(ValueError, "eligible"),
        ):
            sources.selected_nix_candidate(self.item, {}, NOW)

    def test_official_tarball_update_preserves_flake_false_and_archive_timestamp(self):
        self.tarball()
        before = sources.nix_snapshot(self.root, self.spec)
        url = TEMPLATE.format(tag="v2.0.0", version="2.0.0")
        archive_time = int((MATURE - timedelta(days=1)).timestamp())

        def apply(*args, **kwargs):
            self.assertIn(url, args[2])
            self.assertNotIn("github:sample/library/" + B, args[2])
            changed = copy.deepcopy(self.lock)
            changed["nodes"]["library"].update(
                locked={
                    "type": "tarball",
                    "url": url,
                    "narHash": SRI,
                    "lastModified": archive_time,
                },
                original={"type": "tarball", "url": url},
            )
            self.write_lock(changed)

        with (
            patch.object(sources.chainman, "execute", side_effect=apply) as run,
            patch.object(
                sources,
                "nix_tarball_tree",
                return_value={"narHash": SRI, "lastModified": archive_time},
            ),
        ):
            result = sources.resolve_nix(self.root, self.spec, {}, NOW)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["changed"], ["flake.lock"])
            self.assertEqual(
                result["selected"][0]["release_publication"], MATURE.isoformat()
            )
            sources.audit_nix(self.root, self.spec, before, {}, NOW)
            result = sources.resolve_nix(self.root, self.spec, {}, NOW)
            self.assertEqual(result["changed"], [])
            self.assertEqual(run.call_count, 1)
            changed = json.loads((self.root / "flake.lock").read_text())
            self.assertIs(changed["nodes"]["library"]["flake"], False)
            for field, value in (
                ("narHash", "sha256-" + "B" * 43 + "="),
                ("lastModified", int(OLD.timestamp())),
            ):
                changed["nodes"]["library"]["locked"][field] = value
                self.write_lock(changed)
                with self.assertRaisesRegex(ValueError, "timestamp/hash"):
                    sources.audit_nix(self.root, self.spec, before, {}, NOW)
                changed["nodes"]["library"]["locked"].update(
                    narHash=SRI, lastModified=archive_time
                )

    def test_tarball_binding_rejects_wrong_source_before_execution(self):
        self.tarball()
        self.lock["nodes"]["library"]["locked"]["url"] = (
            "https://example.org/library-1.0.0.tar.gz"
        )
        self.write_lock(self.lock)
        with (
            patch.object(sources.chainman, "execute") as run,
            self.assertRaisesRegex(ValueError, "declared release source"),
        ):
            sources.resolve_nix(self.root, self.spec, {}, NOW)
        run.assert_not_called()

    def test_only_changed_inputs_are_overridden_together_and_all_sources_are_audited(
        self,
    ):
        self.tarball()
        archive_url = TEMPLATE.format(tag="v2.0.0", version="2.0.0")
        self.lock["nodes"]["library"]["locked"]["url"] = archive_url
        self.lock["nodes"]["library"]["original"]["url"] = archive_url
        for name, current in (("pkgs", A), ("overlay", A), ("unchanged", B)):
            self.spec["inputs"].append(
                {"input": name, "repository": "sample/" + name, "branch": "main"}
            )
            self.lock["nodes"]["root"]["inputs"][name] = name
            self.lock["nodes"][name] = {
                "locked": {
                    "type": "github",
                    "owner": "sample",
                    "repo": name,
                    "rev": current,
                    "narHash": SRI,
                    "lastModified": int((OLD if current == A else MATURE).timestamp()),
                },
                "original": {
                    "type": "github",
                    "owner": "sample",
                    "repo": name,
                    "ref": "main",
                },
            }
        self.write_lock(self.lock)
        before = sources.nix_snapshot(self.root, self.spec)

        def apply(*args, **kwargs):
            changed = copy.deepcopy(self.lock)
            for name in ("pkgs", "overlay"):
                changed["nodes"][name]["locked"].update(
                    rev=B, lastModified=int(MATURE.timestamp())
                )
            self.write_lock(changed)

        with (
            patch.object(
                sources,
                "nix_candidate",
                return_value={"revision": B, "published": MATURE.isoformat()},
            ),
            patch.object(
                sources,
                "commit_time",
                side_effect=lambda repo, rev: OLD if rev == A else MATURE,
            ),
            patch.object(
                sources,
                "nix_tarball_tree",
                return_value={"narHash": SRI, "lastModified": int(OLD.timestamp())},
            ) as archive_audit,
            patch.object(sources.chainman, "execute", side_effect=apply) as run,
        ):
            result = sources.resolve_nix(self.root, self.spec, {}, NOW)
            self.assertEqual(result["changed"], ["flake.lock"])
            self.assertEqual(len(result["selected"]), 4)
            run.assert_called_once()
            argv = run.call_args.args[2]
            self.assertEqual(argv.count("--override-input"), 2)
            self.assertIn("github:sample/pkgs/" + B, argv)
            self.assertIn("github:sample/overlay/" + B, argv)
            self.assertNotIn("library", argv)
            self.assertNotIn(archive_url, argv)
            self.assertNotIn("unchanged", argv)
            archive_audit.assert_called_once_with(self.root, self.spec, archive_url)
            final = json.loads((self.root / "flake.lock").read_text())
            self.assertEqual(
                final["nodes"]["library"],
                before["locks"]["flake.lock"]["nodes"]["library"],
            )

    def test_selection_conflicts_and_unsupported_provider_fail(self):
        for extra in (
            {"branch": "main"},
            {"selection": "published-channel"},
            {"release_provider": "custom"},
            {"release_provider": "botan", "url_template": TEMPLATE},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                sources.nix_specs(
                    self.root, {**self.spec, "inputs": [{**self.item, **extra}]}
                )
        for template in (
            "https://example.org/latest.tar.gz",
            "https://example.org/{version.__class__}.tar.gz",
            "https://example.org/{version}.tar.gz",
            "https://github.com/sample/library/releases/download/{tag}/${version}.tar.gz",
        ):
            with self.subTest(template=template), self.assertRaises(ValueError):
                sources.nix_release_url(
                    {**self.item, "url_template": template}, "v2.0.0", "2.0.0"
                )


class BotanReleaseTests(unittest.TestCase):
    def test_official_notes_select_mature_release_without_github_release_api(self):
        body = b'<h2>Version 3.14.0, 2026-08-20<a class="headerlink">\n<h2>Version 3.13.0, 2026-07-30<a class="headerlink">'
        item = {
            "input": "botan",
            "repository": "randombit/botan",
            "release_provider": "botan",
            "tag_pattern": r"^(?P<version>\d+\.\d+\.\d+)$",
            "url_template": "https://botan.randombit.net/releases/Botan-{version}.tar.xz",
        }
        with (
            patch.object(registry, "fetch", return_value=(body, {})),
            patch.object(
                registry,
                "data",
                side_effect=AssertionError("GitHub releases requested"),
            ),
            patch.object(registry, "github_commit", return_value=B) as bind,
            patch.object(sources, "commit_time", return_value=OLD),
        ):
            result = sources.selected_nix_candidate(item, {}, NOW)
        self.assertEqual(result["release_version"], "3.13.0")
        self.assertEqual(
            result["release_publication"], "2026-07-30T23:59:59.999999+00:00"
        )
        self.assertEqual(
            result["url"], "https://botan.randombit.net/releases/Botan-3.13.0.tar.xz"
        )
        bind.assert_called_once_with("randombit/botan", "3.13.0")

    def test_missing_malformed_duplicate_and_unbounded_publication_fail(self):
        heading = b'<h2>Version 3.13.0, 2026-07-30<a class="headerlink">'
        for body in (
            b"no release metadata",
            heading + heading,
            heading.replace(b"07-30", b"02-30"),
            b"x" * (1024 * 1024 + 1),
        ):
            with (
                self.subTest(body=body[:80]),
                patch.object(registry, "fetch", return_value=(body, {})),
                self.assertRaises(ValueError),
            ):
                sources.botan_releases()


class ArchiveEvidenceTests(unittest.TestCase):
    def test_fetch_tree_preserves_archive_timestamp_without_store_path_coercion(self):
        evidence = {"narHash": SRI, "lastModified": int(OLD.timestamp())}
        with (
            patch.object(sources.tc, "environment", return_value={}),
            patch.object(
                sources.chainman,
                "execute",
                return_value=type("Result", (), {"stdout": json.dumps(evidence)})(),
            ) as run,
        ):
            self.assertEqual(
                sources.nix_tarball_tree(
                    Path("."), {}, TEMPLATE.format(tag="v2.0.0", version="2.0.0")
                ),
                evidence,
            )
        expression = run.call_args.args[2][-1]
        self.assertIn("inherit (tree) narHash lastModified", expression)
        self.assertNotIn("outPath", expression)
        for malformed in (
            {"narHash": SRI},
            {**evidence, "lastModified": True},
            {**evidence, "narHash": "/nix/store/source"},
        ):
            with (
                patch.object(sources.tc, "environment", return_value={}),
                patch.object(
                    sources.chainman,
                    "execute",
                    return_value=type(
                        "Result", (), {"stdout": json.dumps(malformed)}
                    )(),
                ),
                self.assertRaisesRegex(ValueError, "timestamp/hash"),
            ):
                sources.nix_tarball_tree(
                    Path("."), {}, TEMPLATE.format(tag="v2.0.0", version="2.0.0")
                )


if __name__ == "__main__":
    unittest.main()
