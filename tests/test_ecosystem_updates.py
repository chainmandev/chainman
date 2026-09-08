"""Native workspace policies and manifest preservation, independent of project layouts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
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

    def pub_plan(self):
        paths = [
            self.put(
                "pubspec.yaml",
                "# public range\nname: root\ndependencies:\n  sample: '^1.0.0'\n",
            ),
            self.put(
                "example/pubspec.yaml",
                "name: example\ndev_dependencies:\n  sample: ^1.0.0\n",
            ),
        ]
        paths[0].chmod(0o640)
        spec = {"adapter": "flutter", "directories": [".", "example"]}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        return paths, [(pin, registry.Release("1.2.0", NOW)) for pin in pins]

    def test_pub_resolution_binds_all_workspaces_and_restores_bytes_and_modes(self):
        paths, planned = self.pub_plan()
        before = [(p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in paths]
        for failed in (False, True):
            with self.subTest(failed=failed):
                try:
                    with native.pub_resolution_pins(self.root, planned):
                        for pin, _ in planned:
                            self.assertEqual(
                                native.old_requirement(self.root, pin), "1.2.0"
                            )
                        if failed:
                            raise RuntimeError("resolver exit 23")
                except RuntimeError as error:
                    self.assertEqual(str(error), "resolver exit 23")
                self.assertEqual(
                    [(p.read_bytes(), stat.S_IMODE(p.stat().st_mode)) for p in paths],
                    before,
                )

    def test_pub_resolution_preserves_unexpected_bytes_modes_and_symlinks(self):
        for change in ("bytes", "mode", "symlink"):
            with self.subTest(change=change):
                paths, planned = self.pub_plan()
                before_second = paths[1].read_bytes()
                outside = self.put("outside.txt", "outside retained\n")
                with self.assertRaisesRegex(ValueError, "preserve and inspect"):
                    with native.pub_resolution_pins(self.root, planned):
                        if change == "bytes":
                            paths[0].write_text("concurrent edit\n")
                        elif change == "mode":
                            paths[0].chmod(0o600)
                        else:
                            paths[0].unlink()
                            paths[0].symlink_to(outside)
                self.assertEqual(paths[1].read_bytes(), before_second)
                if change == "bytes":
                    self.assertEqual(paths[0].read_text(), "concurrent edit\n")
                elif change == "mode":
                    self.assertEqual(stat.S_IMODE(paths[0].stat().st_mode), 0o600)
                else:
                    self.assertTrue(paths[0].is_symlink())
                    self.assertEqual(outside.read_text(), "outside retained\n")
                    paths[0].unlink()

    def test_pub_native_resolution_cannot_float_a_chosen_direct_pin_to_young_release(
        self,
    ):
        path = self.put("pubspec.yaml", "name: root\ndependencies:\n  sample: ^1.0.0\n")
        spec = {"adapter": "flutter"}
        inventories = [
            registry.Release(
                value,
                NOW - timedelta(days=age),
                artifacts=(
                    registry.Artifact(
                        f"https://pub.dev/api/archives/sample-{value}.tar.gz",
                        "sha256:" + digit * 64,
                        NOW - timedelta(days=age),
                    ),
                ),
            )
            for value, age, digit in (
                ("1.2.0", 60, "a"),
                ("2.0.0", 40, "b"),
                ("2.1.0", 1, "c"),
            )
        ]

        def resolver(*args, **kwargs):
            requirement = native.manifests.document(path)[0]["dependencies"]["sample"]
            # Model Pub choosing the newest version its manifest permits.
            selected = max(
                (
                    item
                    for item in inventories
                    if native.accepts("pub", item.version, requirement)
                ),
                key=lambda item: registry.version("pub", item.version),
            )
            self.put(
                "pubspec.lock",
                f"packages:\n  sample:\n    source: hosted\n    version: '{selected.version}'\n    description:\n      name: sample\n      url: https://pub.dev\n      sha256: '{selected.artifacts[0].digest.split(':')[1]}'\n",
            )

        with (
            patch.object(registry, "releases", return_value=inventories),
            patch.object(native.chainman, "execute", side_effect=resolver),
        ):
            native.resolve(self.root, spec, {}, NOW)
        self.assertEqual(
            native.manifests.document(path)[0]["dependencies"]["sample"], "^2.0.0"
        )
        self.assertIn("version: '2.0.0'", (self.root / "pubspec.lock").read_text())

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

    def test_incompatible_mature_release_cannot_retire_needed_security_exception(self):
        self.put("Cargo.toml", '[dependencies]\nsample="^1.8.0"\n')
        spec = {"adapter": "rust", "mode": "compatible"}
        pin = native.pins(self.root, spec, native.specifications(self.root, spec))[0]
        policy = {
            "exceptions": [
                {
                    "package": "crates:sample",
                    "version": "1.9.0",
                    "minimum_safe": "1.9.0",
                    "reason": "Specific security fix",
                    "advisory": "https://example.invalid/advisory",
                    "expires": NOW.isoformat(),
                }
            ]
        }
        releases = [
            registry.Release("1.8.0", NOW - timedelta(days=90)),
            registry.Release("1.9.0", NOW - timedelta(days=1)),
            registry.Release("2.0.0", NOW - timedelta(days=60)),
        ]
        with (
            patch.object(registry, "releases", return_value=releases),
            self.assertRaisesRegex(ValueError, "Expired"),
        ):
            native.choose(self.root, pin, spec, policy, NOW)

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

    def test_coordinated_exception_cannot_retire_against_partly_young_release(self):
        self.put("Cargo.toml", '[dependencies]\nsample="1.8.0"\n')
        spec = {"adapter": "rust"}
        pin = native.pins(self.root, spec, native.specifications(self.root, spec))[0]
        pin["coordinated"] = ["sample", "companion"]
        policy = {
            "exceptions": [
                {
                    "package": "crates:sample",
                    "version": "1.9.0",
                    "minimum_safe": "1.9.0",
                    "reason": "Specific security fix",
                    "advisory": "https://example.invalid/advisory",
                    "expires": NOW.isoformat(),
                }
            ]
        }

        def inventory(provider, name):
            return [
                registry.Release("1.8.0", NOW - timedelta(days=90)),
                registry.Release(
                    "1.9.0", NOW - timedelta(days=60 if name == "sample" else 1)
                ),
            ]

        with (
            patch.object(registry, "releases", side_effect=inventory),
            self.assertRaisesRegex(ValueError, "Expired"),
        ):
            native.choose(self.root, pin, spec, policy, NOW)

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

    def test_gradle_initial_adoption_requires_explicit_empty_baseline_and_final_locks(
        self,
    ):
        self.put("build.gradle.kts", "")
        spec = {"adapter": "gradle", "bootstrap_verification": True}
        before = native.snapshot(self.root, spec)
        self.assertEqual(before["identities"], [])
        with self.assertRaisesRegex(ValueError, "required component locks"):
            native.audit(self.root, spec, before, {}, NOW)
        self.put("gradle.lockfile", "sample:library:1.0.0=runtimeClasspath\n")
        with self.assertRaisesRegex(ValueError, "partial"):
            native.snapshot(self.root, spec)
        with self.assertRaisesRegex(ValueError, "verification metadata"):
            native.audit(self.root, spec, before, {}, NOW)
        (self.root / "gradle.lockfile").unlink()
        self.put("gradle/verification-metadata.xml", "<invalid/>")
        with self.assertRaisesRegex(ValueError, "partial"):
            native.snapshot(self.root, spec)

    def test_native_initial_adoption_cannot_omit_its_final_lock(self):
        for adapter, manifest, body, lock, empty in (
            (
                "rust",
                "Cargo.toml",
                '[package]\nname="fixture"\nversion="0.1.0"\n',
                "Cargo.lock",
                "version=4\npackage=[]\n",
            ),
            (
                "python",
                "pyproject.toml",
                '[project]\nname="fixture"\nversion="0.1.0"\ndependencies=[]\n',
                "uv.lock",
                "version=1\npackage=[]\n",
            ),
            (
                "flutter",
                "pubspec.yaml",
                "name: fixture\nversion: 0.1.0\n",
                "pubspec.lock",
                "packages: {}\n",
            ),
        ):
            with self.subTest(adapter=adapter):
                self.put(adapter + "/" + manifest, body)
                spec = {"adapter": adapter, "directory": adapter}
                before = native.snapshot(self.root, spec)
                self.assertEqual(before["identities"], [])
                with self.assertRaisesRegex(
                    ValueError, "Missing resolved dependency lock"
                ):
                    native.audit(self.root, spec, before, {}, NOW)
                path = self.put(adapter + "/" + lock, empty)
                native.audit(self.root, spec, before, {}, NOW)
                path.unlink()
                with self.assertRaisesRegex(
                    ValueError, "Missing resolved dependency lock"
                ):
                    native.audit(self.root, spec, before, {}, NOW)

    def test_gradle_bootstrap_flag_cannot_exempt_another_ecosystem(self):
        self.put("Cargo.toml", "[dependencies]\n")
        with self.assertRaisesRegex(ValueError, "Gradle-only"):
            native.snapshot(
                self.root, {"adapter": "rust", "bootstrap_verification": True}
            )

    def test_gradle_local_catalog_coordinates_require_bound_source_and_no_external_lock(
        self,
    ):
        import lock_adapters

        self.put("build.gradle.kts", "")
        self.put("local/library/build.gradle.kts", "")
        self.put(
            "gradle/libs.versions.toml",
            '[libraries]\nlocal={module="sample:local",version="0.1.0"}\nexternal={module="sample:external",version="1.0.0"}\n',
        )
        spec = {
            "adapter": "gradle",
            "ecosystem": "maven",
            "directory": ".",
            "catalogs": ["gradle/libs.versions.toml"],
            "local_projects": {"sample:local": "local/library"},
        }
        self.assertEqual(
            [p["name"] for p in native.gradle_pins(self.root, spec)],
            ["sample:external"],
        )
        self.put("gradle.lockfile", "sample:local:0.1.0=runtimeClasspath\n")
        with self.assertRaisesRegex(ValueError, "external module"):
            lock_adapters.identities(self.root, spec)
        for relative in ("../outside", "missing"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                native.gradle_pins(
                    self.root, {**spec, "local_projects": {"sample:local": relative}}
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
