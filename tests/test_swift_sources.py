"""Admitted prepublication Swift sources and real frozen native consumers."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import lock_adapters
import registry
import swift_sources as sources
import toolchain as tc
import updates
from dependency_identity import Identity


class SwiftSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swift candidate source spaces ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.producer = self.root / "owned library"
        self.producer.mkdir()
        (self.producer / "Package.swift").write_text(
            '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name: "Owned", products: [.library(name: "Fixture", targets: ["Fixture"])], targets: [.target(name: "Fixture")])\n'
        )
        self.source = self.producer / "Sources/Fixture/Fixture.swift"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("public func fixture() -> Int { 23 }\n")
        (self.producer / "release.json").write_text('{"version":"0.1.0"}\n')
        self.consumer = self.root / "consumer"
        self.consumer.mkdir()
        self.manifest = self.consumer / "Package.swift"
        self.manifest.write_text(
            '// swift-tools-version: 5.9\nimport PackageDescription\nlet package = Package(name: "Probe", dependencies: [.package(url: "https://github.com/neutral/owned.git", exact: "0.1.0")], targets: [.executableTarget(name: "Probe", dependencies: [.product(name: "Fixture", package: "owned")])])\n'
        )
        self.manifest.chmod(0o640)
        (self.consumer / "Sources/Probe").mkdir(parents=True)
        (self.consumer / "Sources/Probe/main.swift").write_text(
            "import Fixture\nprint(fixture())\n"
        )
        (self.root / "chainman.toml").write_text("schema=1\n")
        self.spec = {
            "adapter": "swift",
            "directory": "consumer",
            "swift_sources": {
                "neutral/owned": {
                    "directory": "owned library",
                    "version_file": "owned library/release.json",
                    "version_pointer": ["version"],
                    "paths": ["Package.swift", "Sources"],
                }
            },
        }
        self.mirrors = self.consumer / ".swiftpm/configuration/mirrors.json"
        self.now = datetime(2026, 10, 4, tzinfo=timezone.utc)

    def test_projection_preserves_public_declaration_and_restores_mirrors(self):
        before = self.manifest.read_bytes()
        with sources.bind(self.root, self.spec) as scope:
            self.assertEqual(scope.bindings["neutral/owned"].version, "0.1.0")
            self.assertEqual(self.manifest.read_bytes(), before)
            self.assertTrue(self.mirrors.is_file())
            self.assertEqual(
                json.loads(self.mirrors.read_text())["object"][0]["original"],
                "https://github.com/neutral/owned.git",
            )
            revision = scope.revisions["neutral/owned"]
            with sources.bind(self.root, self.spec, project=False) as nested:
                self.assertIs(scope, nested)
        self.assertFalse(self.mirrors.exists())
        with sources.bind(self.root, self.spec) as scope:
            self.assertEqual(scope.revisions["neutral/owned"], revision)

    def test_existing_unrelated_mirror_bytes_and_modes_are_restored(self):
        self.mirrors.parent.mkdir(parents=True)
        before = b'{"object":[{"original":"https://github.com/neutral/other","mirror":"https://github.com/neutral/fork"}],"version":1}\n'
        self.mirrors.write_bytes(before)
        self.mirrors.chmod(0o640)
        with sources.bind(self.root, self.spec):
            self.assertEqual(len(json.loads(self.mirrors.read_text())["object"]), 2)
        self.assertEqual(self.mirrors.read_bytes(), before)
        self.assertEqual(self.mirrors.stat().st_mode & 0o777, 0o640)

    def test_conflicting_mirror_is_rejected_without_changes(self):
        self.mirrors.parent.mkdir(parents=True)
        before = b'{"version":1,"object":[{"original":"https://github.com/neutral/owned.git","mirror":"file:///tmp/other"}]}\n'
        self.mirrors.write_bytes(before)
        with self.assertRaisesRegex(ValueError, "existing source mirror"):
            with sources.bind(self.root, self.spec):
                self.fail("conflicting mirror admitted")
        self.assertEqual(self.mirrors.read_bytes(), before)

    def test_unexpected_mirror_changes_are_preserved_and_fail(self):
        with self.assertRaisesRegex(ValueError, "changes preserved"):
            with sources.bind(self.root, self.spec):
                self.mirrors.write_text("concurrent change\n")
        self.assertEqual(self.mirrors.read_text(), "concurrent change\n")

    def test_missing_unreachable_unsafe_or_invalid_authority_fails(self):
        variants = []
        for field, value in (
            ("directory", "../outside"),
            ("version_file", "/tmp/version.json"),
            ("version_pointer", []),
            ("paths", ["."]),
            ("paths", ["Package.swift", "../other"]),
            ("paths", ["Sources"]),
            ("paths", ["Package.swift", "missing"]),
        ):
            spec = deepcopy(self.spec)
            spec["swift_sources"]["neutral/owned"][field] = value
            variants.append(spec)
        variants += [
            {**self.spec, "adapter": "rust"},
            {**self.spec, "pins": [{}]},
            {**self.spec, "resolve": [["swift", "package", "resolve"]]},
            {
                **self.spec,
                "swift_sources": {
                    "neutral/absent": self.spec["swift_sources"]["neutral/owned"]
                },
            },
        ]
        for spec in variants:
            with self.subTest(spec=spec), self.assertRaises((ValueError, OSError)):
                with sources.bind(self.root, spec):
                    self.fail("invalid source authority admitted")

    def test_version_requirement_and_security_policy_are_enforced(self):
        for version in ("0.2.0", "v0.1.0", "0.1.0-alpha"):
            (self.producer / "release.json").write_text(
                json.dumps({"version": version})
            )
            with self.subTest(version=version), self.assertRaises(ValueError):
                with sources.bind(self.root, self.spec):
                    self.fail("invalid version admitted")
        (self.producer / "release.json").write_text('{"version":"0.1.0"}')
        binding = sources.read(self.root, self.spec)["neutral/owned"]
        with (
            patch.object(
                registry,
                "minimum_safe",
                return_value=registry.stable_version("swift", "0.2.0"),
            ),
            self.assertRaises(ValueError),
        ):
            sources.check_version(binding, "0.1.0", {})

    def test_symlink_operational_files_and_external_local_dependencies_fail(self):
        self.source.unlink()
        self.source.symlink_to(self.root / "chainman.toml")
        with self.assertRaises(ValueError):
            sources.read(self.root, self.spec)
        self.source.unlink()
        self.source.write_text("// source\n")
        (self.source.parent / ".build").mkdir()
        with self.assertRaises(ValueError):
            sources.read(self.root, self.spec)
        (self.source.parent / ".build").rmdir()
        manifest = self.producer / "Package.swift"
        manifest.write_text(
            manifest.read_text().replace(
                "targets: [.target",
                'dependencies: [.package(path: "../consumer")], targets: [.target',
            )
        )
        with self.assertRaisesRegex(ValueError, "self-contained"):
            sources.read(self.root, self.spec)

    def test_bound_source_selection_never_asks_for_publication(self):
        with patch.object(
            registry,
            "releases",
            side_effect=AssertionError("public metadata requested"),
        ):
            pins = native.pins(
                self.root, self.spec, native.specifications(self.root, self.spec)
            )
            self.assertIsNone(
                native.choose(self.root, pins[0], self.spec, {}, self.now)
            )

    def test_candidate_identity_is_scoped_and_third_party_keeps_registry_audit(self):
        with sources.bind(self.root, self.spec) as scope:
            candidate = Identity(
                "swift",
                "neutral/owned",
                "0.1.0",
                "https://github.com/neutral/owned.git",
                "git:" + scope.revisions["neutral/owned"],
            )
            with patch.object(
                lock_adapters,
                "evidence",
                side_effect=AssertionError("ordinary public audit"),
            ):
                updates.audit_identities(self.root, {candidate}, set(), {}, self.now)
                with self.assertRaisesRegex(AssertionError, "ordinary public audit"):
                    updates.audit_identities(
                        self.root,
                        {
                            Identity(
                                "swift",
                                "neutral/other",
                                "0.1.0",
                                "https://github.com/neutral/other",
                                "git:" + "a" * 40,
                            )
                        },
                        set(),
                        {},
                        self.now,
                    )
            with self.assertRaisesRegex(ValueError, "identity"):
                sources.audit_identity(
                    self.root, candidate._replace(digest="git:" + "a" * 40), {}
                )
        self.assertFalse(sources.audit_identity(self.root, candidate, {}))

    def test_transitive_bound_sources_are_projected_at_consuming_root(self):
        leaf = self.root / "owned leaf"
        leaf.mkdir()
        (leaf / "Package.swift").write_text(
            (self.producer / "Package.swift").read_text()
        )
        (leaf / "release.json").write_text('{"version":"0.1.0"}')
        spec = deepcopy(self.spec)
        spec["swift_sources"]["neutral/leaf"] = {
            "directory": "owned leaf",
            "version_file": "owned leaf/release.json",
            "version_pointer": ["version"],
            "paths": ["Package.swift"],
        }
        manifest = self.producer / "Package.swift"
        manifest.write_text(
            manifest.read_text().replace(
                "targets: [.target",
                'dependencies: [.package(url: "https://github.com/neutral/leaf", from: "0.1.0")], targets: [.target',
            )
        )
        with sources.bind(self.root, spec) as scope:
            self.assertEqual(
                scope.groups, {"swift-0": {"neutral/owned", "neutral/leaf"}}
            )
            self.assertEqual(
                {m["original"] for m in json.loads(self.mirrors.read_text())["object"]},
                {
                    "https://github.com/neutral/owned.git",
                    "https://github.com/neutral/leaf",
                },
            )
        (leaf / "release.json").write_text('{"version":"1.0.0"}')
        with self.assertRaisesRegex(ValueError, "declaration or policy"):
            with sources.bind(self.root, spec):
                self.fail("incompatible transitive source admitted")

    def test_nested_authority_and_duplicate_package_identity_are_rejected(self):
        with sources.bind(self.root, self.spec):
            with self.assertRaisesRegex(ValueError, "different authority"):
                with sources.bind(self.root, {**self.spec, "mode": "compatible"}):
                    self.fail("nested authority admitted")
        spec = deepcopy(self.spec)
        spec["swift_sources"]["other/owned"] = spec["swift_sources"]["neutral/owned"]
        with self.assertRaisesRegex(ValueError, "colliding"):
            sources.read(self.root, spec)

    def test_committed_tree_and_source_bytes_are_both_checked(self):
        with sources.bind(self.root, self.spec) as scope:
            repo = scope.repositories["neutral/owned"]
            (repo / "Sources/Fixture/Fixture.swift").write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                sources.validate_checkout(
                    repo,
                    scope.bindings["neutral/owned"],
                    scope.revisions["neutral/owned"],
                )
            sources.git(repo, "add", "--all")
            sources.git(repo, "commit", "-qm", "Altered committed source")
            (repo / "Sources/Fixture/Fixture.swift").write_bytes(
                self.source.read_bytes()
            )
            with self.assertRaisesRegex(ValueError, "Git tree differs"):
                sources.validate_checkout(
                    repo,
                    scope.bindings["neutral/owned"],
                    sources.git(repo, "rev-parse", "HEAD"),
                )


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_SWIFT") == "1", "run just swift-test"
)
class NativeSwiftSourceTests(unittest.TestCase):
    def setUp(self):
        SwiftSourceTests.setUp(self)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GIT_", "SWIFTPM_"))
        }
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_OPTIONAL_LOCKS="0",
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
            XDG_CACHE_HOME=str(self.root / "cache"),
        )

    def command(self, argv):
        result = tc.managed_run(
            argv,
            cwd=self.consumer,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        return result

    def evidence(self):
        stack = ExitStack()
        stack.enter_context(
            patch.object(
                native.chainman,
                "execute",
                side_effect=lambda root, profile, argv, **kwargs: self.command(argv),
            )
        )
        stack.enter_context(
            patch.object(
                lock_adapters,
                "native",
                side_effect=lambda root, profile, argv, **kwargs: json.loads(
                    self.command(argv).stdout
                ),
            )
        )
        stack.enter_context(
            patch.object(
                registry,
                "releases",
                side_effect=AssertionError("candidate public metadata requested"),
            )
        )
        stack.enter_context(
            patch.object(
                lock_adapters,
                "evidence",
                side_effect=AssertionError("candidate publication evidence requested"),
            )
        )
        return stack

    def test_real_native_resolve_import_and_later_frozen_audit(self):
        original = self.manifest.read_bytes()
        before = native.snapshot(self.root, self.spec)
        with self.evidence():
            with sources.bind(self.root, self.spec):
                selected = native.resolve(self.root, self.spec, {}, self.now)
                self.assertEqual(
                    self.command(["swift", "run", "Probe"]).stdout.strip(), "23"
                )
            self.assertEqual(self.manifest.read_bytes(), original)
            self.assertFalse(self.mirrors.exists())
            before["resolution"] = selected
            native.audit(self.root, self.spec, before, {}, self.now)
            self.assertFalse(self.mirrors.exists())
            imported = (
                self.consumer / ".build/checkouts/owned/Sources/Fixture/Fixture.swift"
            )
            imported.chmod(0o644)
            imported.write_text("public func fixture() -> Int { 99 }\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                native.audit(self.root, self.spec, before, {}, self.now)

    def test_frozen_source_and_materialization_authority_cannot_change(self):
        before = native.snapshot(self.root, self.spec)
        with self.evidence():
            before["resolution"] = native.resolve(self.root, self.spec, {}, self.now)
            path = self.consumer / ".build/workspace-state.json"
            state = json.loads(path.read_text())
            state["object"]["dependencies"][0]["packageRef"]["location"] = (
                "file:///tmp/unadmitted/owned"
            )
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "imported identity"):
                native.audit(self.root, self.spec, before, {}, self.now)

    def test_frozen_source_content_revision_or_lock_cannot_change(self):
        before = native.snapshot(self.root, self.spec)
        with self.evidence():
            before["resolution"] = native.resolve(self.root, self.spec, {}, self.now)
            original = self.source.read_bytes()
            self.source.write_text("public func fixture() -> Int { 99 }\n")
            with self.assertRaisesRegex(ValueError, "lock identity"):
                native.audit(self.root, self.spec, before, {}, self.now)
            self.source.write_bytes(original)
            lock = self.consumer / "Package.resolved"
            state = json.loads(lock.read_text())
            state["pins"][0]["state"]["revision"] = "a" * 40
            lock.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, "lock identity"):
                native.audit(self.root, self.spec, before, {}, self.now)
