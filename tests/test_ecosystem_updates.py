"""Native workspace policies and manifest preservation, independent of project layouts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import registry


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native workspace ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "chainman.toml").write_text("schema=1\n")

    def put(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def inventory(self, provider, package):
        return [
            registry.Release("1.2.0", NOW - timedelta(days=60)),
            registry.Release("2.0.0", NOW - timedelta(days=40)),
            registry.Release("3.0.0", NOW - timedelta(days=10)),
        ]

    def test_declared_cargo_workspace_and_renamed_dependency(self):
        self.put(
            "Cargo.toml",
            '[workspace]\nmembers=["crates/*"]\nexclude=["crates/ignored"]\n[workspace.dependencies]\nrenamed={package="upstream",version="1.0"}\n',
        )
        self.put(
            "crates/member/Cargo.toml",
            '[package]\nname="member"\nversion="0.1.0"\n[dependencies]\nrenamed={workspace=true}\nlocal={path="../other"}\n',
        )
        self.put("crates/ignored/Cargo.toml", '[dependencies]\nnever="1"\n')
        spec = {"adapter": "rust", "profile": "rust"}
        specs = native.specifications(self.root, spec)
        pins = native.pins(self.root, spec, specs)
        self.assertEqual(
            [(pin["name"], pin["file"]) for pin in pins], [("upstream", "Cargo.toml")]
        )
        with patch.object(registry, "releases", side_effect=self.inventory):
            self.assertEqual(
                native.choose(self.root, pins[0], spec, {}, NOW).version, "2.0.0"
            )
            self.assertEqual(
                native.choose(
                    self.root, pins[0], {**spec, "mode": "compatible"}, {}, NOW
                ).version,
                "1.2.0",
            )

    def test_compatible_policy_never_falls_back_to_unrestricted_latest(self):
        self.put("Cargo.toml", '[dependencies]\nsample="^4.0"\n')
        spec = {"adapter": "rust", "mode": "compatible"}
        pin = native.pins(self.root, spec, native.specifications(self.root, spec))[0]
        with (
            patch.object(registry, "releases", side_effect=self.inventory),
            self.assertRaisesRegex(ValueError, "No eligible"),
        ):
            native.choose(self.root, pin, spec, {}, NOW)

    def test_cargo_compatibility_uses_cargo_caret_semantics(self):
        self.assertTrue(native.accepts("crates", "1.9.0", "1.2"))
        self.assertFalse(native.accepts("crates", "2.0.0", "1.2"))
        self.assertFalse(native.accepts("crates", "0.3.0", "0.2.1"))

    def test_swift_literal_dependency_preserves_the_declaration(self):
        path = self.put(
            "Package.swift",
            '// declaration\nlet deps = [.package(url: "https://github.com/example/package", from: "1.0.0")]\n',
        )
        spec = {"adapter": "swift"}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        self.assertEqual(len(pins), 1)
        with patch.object(registry, "releases", side_effect=self.inventory):
            chosen = native.choose(
                self.root, pins[0], {**spec, "mode": "compatible"}, {}, NOW
            )
        native.manifests.replace(pins[0], chosen, self.root)
        self.assertEqual(
            path.read_text(),
            '// declaration\nlet deps = [.package(url: "https://github.com/example/package", from: "1.2.0")]\n',
        )

    def test_undeclared_swift_branch_and_outside_local_source_fail(self):
        for declaration in (
            '.package(url: "https://github.com/example/package", branch: "main")',
            '.package(path: "../outside")',
        ):
            self.put("Package.swift", declaration)
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                native.snapshot(self.root, {"adapter": "swift"})

    def test_python_workspace_markers_extras_and_build_dependencies(self):
        self.put(
            "pyproject.toml",
            '[project]\nname="sample"\nversion="1.0.0"\ndependencies=["library[extra]>=1; python_version >= \'3.10\'"]\n[build-system]\nrequires=["backend>=1"]\n[tool.uv.workspace]\nmembers=["packages/*"]\n',
        )
        self.put(
            "packages/member/pyproject.toml",
            '[project]\nname="member"\nversion="1.0.0"\ndependencies=[]\n',
        )
        spec = {"adapter": "python"}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        self.assertEqual({pin["name"] for pin in pins}, {"library", "backend"})
        library = next(pin for pin in pins if pin["name"] == "library")
        native.manifests.replace(library, registry.Release("2.0.0", NOW), self.root)
        self.assertIn(
            'library[extra]==2.0.0; python_version >= \\"3.10\\"',
            (self.root / "pyproject.toml").read_text(),
        )

    def test_gradle_shared_version_requires_candidate_intersection(self):
        self.put("build.gradle.kts", "")
        self.put(
            "gradle/libs.versions.toml",
            '[versions]\nshared="1.0.0"\n[libraries]\na={module="sample:a",version.ref="shared"}\nb={module="sample:b",version.ref="shared"}\n',
        )
        spec = {"adapter": "gradle", "catalogs": ["gradle/libs.versions.toml"]}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        self.assertEqual(len(pins), 1)
        inventory = lambda package, repo: (
            self.inventory("maven", package)
            if package.endswith(":a")
            else [registry.Release("1.2.0", NOW - timedelta(days=50))]
        )
        with patch.object(registry, "maven_releases", side_effect=inventory):
            self.assertEqual(
                native.choose(self.root, pins[0], spec, {}, NOW).version, "1.2.0"
            )

    def test_hook_cannot_replace_selected_pin_after_resolution(self):
        self.put("Cargo.toml", '[dependencies]\nsample="2.0.0"\n')
        spec = {"adapter": "rust"}
        pin = native.pins(self.root, spec, native.specifications(self.root, spec))[0]
        baseline = {
            "identities": [],
            "resolution": {"pins": [{"pin": pin, "value": "1.0.0"}]},
        }
        with self.assertRaisesRegex(ValueError, "hook changed"):
            native.audit(self.root, spec, baseline, {}, NOW)

    def test_missing_evidence_prevents_all_manifest_mutation(self):
        manifest = self.put("Cargo.toml", '[dependencies]\na="1.0.0"\nz="1.0.0"\n')
        original = manifest.read_bytes()

        def candidates(provider, package):
            if package == "z":
                raise ValueError("Missing publication evidence")
            return self.inventory(provider, package)

        with (
            patch.object(registry, "releases", side_effect=candidates),
            self.assertRaisesRegex(ValueError, "Missing publication"),
        ):
            native.resolve(self.root, {"adapter": "rust"}, {}, NOW)
        self.assertEqual(manifest.read_bytes(), original)

    def test_flutter_workspace_members_are_dependency_inputs(self):
        self.put("pubspec.yaml", "name: root\nworkspace: [packages/member]\n")
        self.put(
            "packages/member/pubspec.yaml",
            "name: member\nresolution: workspace\ndependencies:\n  sample: ^1.0.0\n",
        )
        spec = {"adapter": "flutter"}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        self.assertEqual(
            [(pin["file"], pin["name"]) for pin in pins],
            [("packages/member/pubspec.yaml", "sample")],
        )

    def test_local_sources_reject_symlink_before_parent_normalization(self):
        (self.root / "actual").mkdir()
        (self.root / "alias").symlink_to(self.root / "actual", target_is_directory=True)
        for kind, filename, body in (
            ("rust", "Cargo.toml", '[dependencies]\nlocal={path="alias/../actual"}\n'),
            (
                "python",
                "pyproject.toml",
                '[project]\ndependencies=["local"]\n[tool.uv.sources]\nlocal={path="alias/../actual"}\n',
            ),
            (
                "flutter",
                "pubspec.yaml",
                "name: sample\ndependencies:\n  local:\n    path: alias/../actual\n",
            ),
            ("swift", "Package.swift", '.package(path: "alias/../actual")'),
        ):
            self.put(filename, body)
            spec = {"adapter": kind}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "symlink"):
                native.pins(self.root, spec, native.specifications(self.root, spec))


if __name__ == "__main__":
    unittest.main()
