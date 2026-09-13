"""Real SwiftPM update and audit with disposable Git repositories, no public fetches."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import lock_adapters
import registry
import source_updates
import toolchain as tc


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_SWIFT") == "1", "run just swift-test"
)
class NativeSwiftTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("swift"), "swift-test requires pinned Swift")
        temporary = tempfile.TemporaryDirectory(prefix="chainman swift fixture ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.releases, self.revisions, self.calls = {}, {}, []
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "SWIFTPM_"))
        }
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self.base / "gitconfig"),
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
            XDG_CACHE_HOME=str(self.base / "cache"),
            XDG_CONFIG_HOME=str(self.base / "config"),
            CLANG_MODULE_CACHE_PATH=str(self.base / "clang-cache"),
            SWIFTPM_MODULECACHE_OVERRIDE=str(self.base / "swift-cache"),
        )
        # Git rewrites only transport: SwiftPM still sees the declared GitHub
        # source identity. Reject all non-file transport to prevent public fetches.
        self.git(self.base, "config", "--global", "user.name", "Neutral Fixture")
        self.git(
            self.base, "config", "--global", "user.email", "fixture@example.invalid"
        )
        for name in ("neutral-parent", "neutral-leaf"):
            repository = self.base / name
            repository.mkdir()
            self.git(repository, "init", "-q")
            self.git(
                self.base,
                "config",
                "--global",
                f"url.{repository.as_uri()}.insteadOf",
                f"https://github.com/chainman-fixture/{name}",
            )
        self.release("neutral-leaf", "1.0.0", 90)
        self.release(
            "neutral-parent",
            "1.0.0",
            90,
            '.package(url: "https://github.com/chainman-fixture/neutral-leaf", from: "1.0.0")',
        )
        (self.root / "chainman.toml").write_text("schema=1\n")
        self.manifest = self.root / "Package.swift"
        source = self.root / "Sources/NeutralApp/Library.swift"
        source.parent.mkdir(parents=True)
        source.write_text("// unchanged application source\n")
        self.manifest.write_text(
            self.package(
                "NeutralApp",
                '.package(url: "https://github.com/chainman-fixture/neutral-parent", exact: "1.0.0")',
            )
        )
        self.swift(["swift", "package", "resolve"])
        self.manifest.write_text(
            self.manifest.read_text().replace('exact: "1.0.0"', 'from: "1.0.0"')
        )
        self.manifest.chmod(0o640)
        self.lock = self.root / "Package.resolved"
        self.lock.chmod(0o640)
        self.original_lock = native.cargo_file_state(self.root, "Package.resolved")
        self.release("neutral-leaf", "1.5.0", 60)
        self.release(
            "neutral-parent",
            "1.2.0",
            60,
            '.package(url: "https://github.com/chainman-fixture/neutral-leaf", from: "1.0.0")',
        )
        self.release(
            "neutral-parent",
            "1.2.1",
            1,
            '.package(url: "https://github.com/chainman-fixture/neutral-leaf", from: "1.0.0")',
        )
        self.spec = {"adapter": "swift", "mode": "compatible"}

    def package(self, name, dependency="", *, child=None):
        child = child or ("neutral-parent" if name == "NeutralApp" else "neutral-leaf")
        target_dependency = (
            f'.product(name: "{child}", package: "{child}")' if dependency else ""
        )
        return (
            '// swift-tools-version: 5.9\n// preserved project comment\nimport PackageDescription\nlet package = Package(name: "'
            + name
            + '", products: [.library(name: "'
            + name
            + '", targets: ["'
            + name
            + '"])], dependencies: ['
            + dependency
            + '], targets: [.target(name: "'
            + name
            + '", dependencies: ['
            + target_dependency
            + "])])\n"
        )

    def git(self, directory, *arguments):
        return tc.managed_run(
            ["git", *arguments],
            cwd=directory,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()

    def release(self, name, value, days, dependency=""):
        repository = self.base / name
        source = repository / "Sources" / name / "Library.swift"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("// neutral library fixture\n")
        (repository / "Package.swift").write_text(
            self.package(name, dependency) + f"// release {value}\n"
        )
        self.git(repository, "add", ".")
        self.git(repository, "commit", "-qm", value)
        self.git(repository, "tag", value)
        self.revisions[name, value] = self.git(repository, "rev-parse", "HEAD")
        published = self.now - timedelta(days=days)
        self.releases.setdefault("chainman-fixture/" + name, []).append(
            registry.Release(value, published, identity=value)
        )

    def swift(self, argv):
        options = [
            "--cache-path",
            str(self.base / "package-cache"),
            "--config-path",
            str(self.base / "package-config"),
            "--security-path",
            str(self.base / "package-security"),
        ]
        if "--scratch-path" not in argv:
            options += ["--scratch-path", str(self.root / ".build")]
        try:
            return tc.managed_run(
                [*argv[:2], *options, *argv[2:]],
                cwd=self.root,
                env=self.env,
                check=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.CalledProcessError as error:
            error.add_note(error.stdout + error.stderr)
            raise

    def execute(self, root, profile, argv, **kwargs):
        self.assertEqual(
            (root, profile, kwargs["cwd"]), (self.root, "swift", self.root)
        )
        self.calls.append((list(argv), self.manifest.read_bytes()))
        return self.swift(argv)

    def evaluate(self, root, profile, argv, **kwargs):
        self.assertEqual((root, profile), (self.root, "swift"))
        self.calls.append((list(argv), self.manifest.read_bytes()))
        return json.loads(self.swift(argv).stdout)

    def evidence(self):
        # Network evidence is fixture data; both native command entry points run
        # the real binary, including dump-package and frozen show-dependencies.
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch.object(native.chainman, "execute", side_effect=self.execute)
        )
        stack.enter_context(
            patch.object(lock_adapters, "native", side_effect=self.evaluate)
        )
        stack.enter_context(
            patch.object(
                registry,
                "releases",
                side_effect=lambda provider, name: self.releases[name],
            )
        )
        stack.enter_context(
            patch.object(
                registry,
                "github_commit",
                side_effect=lambda name, tag: self.revisions[name.split("/")[1], tag],
            )
        )
        stack.enter_context(
            patch.object(
                source_updates,
                "commit_time",
                return_value=self.now - timedelta(days=90),
            )
        )
        return stack

    def assert_public_manifest(self, value="1.2.0"):
        expected = self.package(
            "NeutralApp",
            f'.package(url: "https://github.com/chainman-fixture/neutral-parent", from: "{value}")',
        )
        self.assertEqual(self.manifest.read_text(), expected)
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o640)

    def test_real_selection_restoration_and_read_only_native_audit(self):
        with self.evidence():
            result = native.resolve(self.root, self.spec, {}, self.now)
            self.assert_public_manifest()
            self.assertEqual(
                result["swift_selected"],
                {"swift-0": {"chainman-fixture/neutral-parent": "1.2.0"}},
            )
            self.assertTrue(
                any(
                    argv == ["swift", "package", "update"] and b'exact: "1.2.0"' in body
                    for argv, body in self.calls
                )
            )
            pins = json.loads(self.lock.read_text())["pins"]
            self.assertEqual(
                {pin["identity"]: pin["state"]["version"] for pin in pins},
                {"neutral-parent": "1.2.0", "neutral-leaf": "1.5.0"},
            )
            self.assertTrue(
                all(
                    pin["state"]["revision"]
                    == self.revisions[pin["identity"], pin["state"]["version"]]
                    for pin in pins
                )
            )
            before = native.cargo_file_state(self.root, "Package.resolved")
            native.audit(
                self.root,
                self.spec,
                {"identities": [], "resolution": result},
                {},
                self.now,
            )
            self.assertEqual(
                native.cargo_file_state(self.root, "Package.resolved"), before
            )
            self.assert_public_manifest()
            self.assertTrue(any("show-dependencies" in argv for argv, _ in self.calls))
            json.dumps(result)

    def test_real_unsatisfiable_selection_restores_public_manifest(self):
        self.release(
            "neutral-parent",
            "1.3.0",
            60,
            '.package(url: "https://github.com/chainman-fixture/neutral-leaf", exact: "9.0.0")',
        )
        with (
            self.evidence(),
            self.assertRaises(subprocess.CalledProcessError) as failure,
        ):
            native.resolve(self.root, self.spec, {}, self.now)
        self.assertEqual(failure.exception.cmd[-1], "update")
        self.assertIn("9.0.0", failure.exception.stderr)
        self.assert_public_manifest("1.3.0")
        self.assertEqual(
            native.cargo_file_state(self.root, "Package.resolved"), self.original_lock
        )

    def test_real_remote_graph_through_local_package_is_audited_and_preserved(self):
        bridge = self.root / "bridge"
        source = bridge / "Sources/NeutralBridge/Library.swift"
        source.parent.mkdir(parents=True)
        source.write_text("// unchanged local bridge\n")
        child_manifest = bridge / "Package.swift"
        child_manifest.write_text(
            self.package(
                "NeutralBridge",
                '.package(url: "https://github.com/chainman-fixture/neutral-parent", exact: "1.2.0")',
                child="neutral-parent",
            )
        )
        child_manifest.chmod(0o640)
        # SwiftPM's local package identity is its path basename, while its product
        # name comes from Package.swift.
        self.manifest.write_text(
            self.package(
                "NeutralApp", '.package(path: "bridge")', child="NeutralBridge"
            ).replace('package: "NeutralBridge"', 'package: "bridge"')
        )
        original = {
            path: (path.read_bytes(), path.stat().st_mode)
            for path in (self.manifest, child_manifest, source)
        }
        with self.evidence():
            result = native.resolve(self.root, self.spec, {}, self.now)
            self.assertEqual(result["swift_selected"], {"swift-0": {}})
            self.assertEqual(
                set(result["swift_inputs"]), {"Package.swift", "bridge/Package.swift"}
            )
            pins = json.loads(self.lock.read_text())["pins"]
            self.assertEqual(
                {pin["identity"]: pin["state"]["version"] for pin in pins},
                {"neutral-parent": "1.2.0", "neutral-leaf": "1.5.0"},
            )
            before = native.cargo_file_state(self.root, "Package.resolved")
            baseline = {"identities": [], "resolution": result}
            native.audit(self.root, self.spec, baseline, {}, self.now)
            self.assertEqual(
                native.cargo_file_state(self.root, "Package.resolved"), before
            )
            self.assertEqual(
                {path: (path.read_bytes(), path.stat().st_mode) for path in original},
                original,
            )
            # A stale native cache must not conceal an incomplete command-root lock.
            lock = json.loads(self.lock.read_text())
            lock["pins"] = [
                pin for pin in lock["pins"] if pin["identity"] != "neutral-parent"
            ]
            self.lock.write_text(json.dumps(lock))
            incomplete = native.cargo_file_state(self.root, "Package.resolved")
            with self.assertRaisesRegex(ValueError, "lock|identity|identities"):
                native.audit(self.root, self.spec, baseline, {}, self.now)
            self.assertEqual(
                native.cargo_file_state(self.root, "Package.resolved"), incomplete
            )
            self.assertEqual(
                {path: (path.read_bytes(), path.stat().st_mode) for path in original},
                original,
            )

    def test_actual_resolution_rejects_disagreeing_revision_evidence(self):
        self.revisions["neutral-parent", "1.2.0"] = "0" * 40
        with self.evidence(), self.assertRaisesRegex(ValueError, "identity|artifact"):
            native.resolve(self.root, self.spec, {}, self.now)
        self.assert_public_manifest()
        # Swift preserves the resolver's candidate output for investigation.
        pins = json.loads(self.lock.read_text())["pins"]
        self.assertEqual(
            next(
                pin["state"]["version"]
                for pin in pins
                if pin["identity"] == "neutral-parent"
            ),
            "1.2.0",
        )
