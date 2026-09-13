"""Native workspace policies and manifest preservation, independent of project layouts."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import registry


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def swift_graph_node(url, version="unspecified", dependencies=(), *, path=None):
    return {
        "identity": url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower(),
        "name": url.rstrip("/").rsplit("/", 1)[-1],
        "url": url,
        "version": version,
        "path": str(path) if path is not None else url,
        "dependencies": list(dependencies),
    }


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

        def inventory(package, repo):
            return (
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

    def pub_transitive_fixture(self):
        path = self.put(
            "pubspec.yaml",
            "# public contract\nname: root\ndependencies:\n  consumer:\n    path: consumer\n",
        )
        path.chmod(0o640)
        self.put(
            "consumer/pubspec.yaml",
            "name: consumer\nversion: 1.0.0\ndependencies:\n  sample: '>=1.0.0 <3.0.0'\n",
        )
        releases = [
            registry.Release(
                value,
                NOW - timedelta(days=days),
                artifacts=(
                    registry.Artifact(
                        f"https://pub.dev/api/archives/sample-{value}.tar.gz",
                        "sha256:" + digit * 64,
                        NOW - timedelta(days=days),
                    ),
                ),
            )
            for value, days, digit in [
                ("1.5.0", 60, "a"),
                ("2.0.0", 1, "b"),
                ("3.0.0", 60, "c"),
            ]
        ]
        calls = []

        def write(directory, value, role="transitive", digit=None):
            item = next(item for item in releases if item.version == value)
            digest = digit * 64 if digit else item.artifacts[0].digest.split(":")[1]
            (directory / "pubspec.lock").write_text(
                f"packages:\n  sample:\n    dependency: '{role}'\n    source: hosted\n    version: '{value}'\n    description:\n      name: sample\n      url: https://pub.dev\n      sha256: '{digest}'\n"
                f"  consumer:\n    dependency: direct main\n    source: path\n    version: 1.0.0\n    description:\n      path: {os.path.relpath(self.root / 'consumer', directory)}\n      relative: true\n"
            )

        def resolver(root, profile, argv, **kwargs):
            directory = kwargs["cwd"]
            document = native.manifests.document(directory / "pubspec.yaml")[0]
            selected = document.get("dev_dependencies", {}).get("sample")
            calls.append(
                (str(directory.relative_to(self.root)), selected, "--offline" in argv)
            )
            if selected and not native.accepts(
                "pub",
                selected,
                native.manifests.document(self.root / "consumer/pubspec.yaml")[0][
                    "dependencies"
                ]["sample"],
            ):
                raise subprocess.CalledProcessError(
                    1,
                    argv,
                    output="Because the parent requires <3.0.0, version solving failed.\n",
                )
            if selected is None:
                selected = (
                    native.manifests.document(directory / "pubspec.lock")[0][
                        "packages"
                    ]["sample"]["version"]
                    if "--offline" in argv
                    else "2.0.0"
                )
            write(
                directory,
                selected,
                "direct dev"
                if document.get("dev_dependencies", {}).get("sample")
                else "transitive",
            )
            return subprocess.CompletedProcess(argv, 0, stdout="native Pub completed\n")

        return path, releases, calls, write, resolver

    def test_pub_transitive_backtracks_through_native_constraints_and_normalizes(self):
        path, releases, calls, _, resolver = self.pub_transitive_fixture()
        before = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual([entry[1] for entry in calls], [None, "3.0.0", "1.5.0", None])
        self.assertTrue(calls[-1][2])
        self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), before)
        self.assertIn(
            "dependency: 'transitive'", (self.root / "pubspec.lock").read_text()
        )
        self.assertIn("version: '1.5.0'", (self.root / "pubspec.lock").read_text())

    def test_pub_transitive_constraints_cover_each_workspace(self):
        path, releases, calls, _, resolver = self.pub_transitive_fixture()
        second = self.put(
            "example/pubspec.yaml",
            "name: example\ndependencies:\n  consumer:\n    path: ../consumer\n",
        )
        before = (path.read_bytes(), second.read_bytes())
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
        ):
            native.resolve(
                self.root,
                {"adapter": "flutter", "directories": [".", "example"]},
                {},
                NOW,
            )
        self.assertEqual((path.read_bytes(), second.read_bytes()), before)
        for directory in (self.root, self.root / "example"):
            self.assertIn("version: '1.5.0'", (directory / "pubspec.lock").read_text())
        self.assertEqual({entry[0] for entry in calls if entry[2]}, {".", "example"})

    def test_pub_transitive_search_has_a_fixed_state_bound(self):
        path, releases, calls, _, resolver = self.pub_transitive_fixture()
        before = path.read_bytes()
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
            patch.object(native, "PUB_SOLVER_STATES", 2),
            self.assertRaisesRegex(ValueError, "2-state bound"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 2)
        self.assertEqual(path.read_bytes(), before)

    def test_pub_transitive_no_eligible_release_never_falls_back_to_young(self):
        path, releases, calls, _, resolver = self.pub_transitive_fixture()
        before = path.read_bytes()
        with (
            patch.object(registry, "releases", return_value=[releases[1]]),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "No eligible Pub transitive graph"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_pub_transitive_all_mature_candidates_incompatible_fail(self):
        path, releases, calls, _, resolver = self.pub_transitive_fixture()
        before = path.read_bytes()
        with (
            patch.object(registry, "releases", return_value=releases[1:]),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "native constraints"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 2)
        self.assertEqual(path.read_bytes(), before)

    def test_pub_transitive_unrelated_command_failure_keeps_original_status(self):
        path, releases, calls, _, resolver = self.pub_transitive_fixture()
        before = path.read_bytes()

        def fail(*args, **kwargs):
            if native.manifests.document(path)[0].get("dev_dependencies"):
                raise subprocess.CalledProcessError(
                    23,
                    args[2],
                    output="unrelated exit23 quoting version solving failed\n",
                )
            return resolver(*args, **kwargs)

        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=fail),
            self.assertRaises(subprocess.CalledProcessError) as raised,
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(raised.exception.returncode, 23)
        self.assertEqual(path.read_bytes(), before)

    def test_pub_transitive_never_repairs_a_hash_mismatch(self):
        path, releases, calls, write, resolver = self.pub_transitive_fixture()

        def corrupt(*args, **kwargs):
            result = resolver(*args, **kwargs)
            write(self.root, "2.0.0", digit="f")
            return result

        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=corrupt),
            self.assertRaisesRegex(ValueError, "identity is absent"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 1)

    def test_pub_normalization_cannot_replace_the_selected_artifact_graph(self):
        path, releases, _, write, resolver = self.pub_transitive_fixture()
        before = path.read_bytes()

        def change(*args, **kwargs):
            result = resolver(*args, **kwargs)
            if "--offline" in args[2]:
                write(self.root, "2.0.0")
            return result

        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=change),
            self.assertRaisesRegex(ValueError, "normalization changed"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(path.read_bytes(), before)

    def test_pub_transitive_preserves_declared_overrides_and_rejects_their_conflict(
        self,
    ):
        path, releases, _, write, resolver = self.pub_transitive_fixture()
        override = self.put(
            "pubspec_overrides.yaml", "dependency_overrides:\n  sample: 2.0.0\n"
        )
        before = (path.read_bytes(), override.read_bytes())

        def overridden(*args, **kwargs):
            self.assertEqual(override.read_bytes(), before[1])
            write(self.root, "2.0.0")
            return subprocess.CompletedProcess(
                args[2], 0, stdout="explicit override honored\n"
            )

        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=overridden),
            self.assertRaisesRegex(ValueError, "declared overrides"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual((path.read_bytes(), override.read_bytes()), before)

    def test_pub_transitive_preserves_concurrent_manifest_and_override_edits(self):
        for target in ("pubspec.yaml", "pubspec_overrides.yaml"):
            with self.subTest(target=target):
                path, releases, _, _, resolver = self.pub_transitive_fixture()
                changed = self.root / target

                def concurrent(*args, **kwargs):
                    result = resolver(*args, **kwargs)
                    changed.write_text("concurrent author edit\n")
                    return result

                with (
                    patch.object(registry, "releases", return_value=releases),
                    patch.object(native.chainman, "execute", side_effect=concurrent),
                    self.assertRaisesRegex(ValueError, "preserve and inspect"),
                ):
                    native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
                self.assertEqual(changed.read_text(), "concurrent author edit\n")
                if target != "pubspec.yaml":
                    changed.unlink()

    def test_pub_transitive_security_floor_cannot_fall_back_below_it(self):
        _, releases, calls, _, resolver = self.pub_transitive_fixture()
        policy = {
            "exceptions": [
                {
                    "package": "pub:sample",
                    "version": "3.0.0",
                    "minimum_safe": "2.5.0",
                    "reason": "fix",
                    "advisory": "https://example.invalid/fix",
                    "expires": (NOW + timedelta(days=5)).isoformat(),
                }
            ]
        }
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "No eligible Pub transitive graph"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, policy, NOW)
        self.assertEqual([entry[1] for entry in calls], [None, "3.0.0"])

    def test_pub_transitive_expired_exception_cannot_authorize_young_release(self):
        _, releases, _, _, resolver = self.pub_transitive_fixture()
        policy = {
            "exceptions": [
                {
                    "package": "pub:sample",
                    "version": "2.0.0",
                    "minimum_safe": "2.0.0",
                    "reason": "fix",
                    "advisory": "https://example.invalid/fix",
                    "expires": NOW.isoformat(),
                }
            ]
        }
        with (
            patch.object(registry, "releases", return_value=releases[:2]),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "Expired"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, policy, NOW)

    def test_pub_transitive_future_artifact_age_is_not_a_retry_candidate(self):
        _, releases, calls, _, resolver = self.pub_transitive_fixture()
        young = releases[1]
        future = registry.Release(
            young.version,
            NOW + timedelta(days=1),
            artifacts=(
                registry.Artifact(
                    young.artifacts[0].url,
                    young.artifacts[0].digest,
                    NOW + timedelta(days=1),
                ),
            ),
        )
        with (
            patch.object(
                registry, "releases", return_value=[releases[0], future, releases[2]]
            ),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "Future Pub artifact"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 1)

    def test_pub_transitive_retains_only_the_exact_existing_young_artifact(self):
        _, releases, calls, write, resolver = self.pub_transitive_fixture()
        write(self.root, "2.0.0")
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 1)
        self.assertIn("version: '2.0.0'", (self.root / "pubspec.lock").read_text())

    def test_pub_transitive_preserves_concurrent_override_mode_and_symlink(self):
        for action in ("mode", "symlink"):
            with self.subTest(action=action):
                _, releases, _, _, resolver = self.pub_transitive_fixture()
                override = self.put(
                    "pubspec_overrides.yaml", "dependency_overrides: {}\n"
                )
                override.chmod(0o640)
                outside = self.put("author.txt", "author-owned content\n")

                def concurrent(*args, **kwargs):
                    result = resolver(*args, **kwargs)
                    if action == "mode":
                        override.chmod(0o600)
                    else:
                        override.unlink()
                        override.symlink_to(outside)
                    return result

                with (
                    patch.object(registry, "releases", return_value=releases),
                    patch.object(native.chainman, "execute", side_effect=concurrent),
                    self.assertRaisesRegex(ValueError, "preserve and inspect"),
                ):
                    native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
                if action == "mode":
                    self.assertEqual(stat.S_IMODE(override.stat().st_mode), 0o600)
                else:
                    self.assertTrue(override.is_symlink())
                    self.assertEqual(outside.read_text(), "author-owned content\n")
                override.unlink()

    def test_pub_transitive_repairs_baseline_below_a_new_security_floor(self):
        _, releases, calls, write, resolver = self.pub_transitive_fixture()
        self.put(
            "consumer/pubspec.yaml",
            "name: consumer\nversion: 1.0.0\ndependencies:\n  sample: '>=1.0.0 <4.0.0'\n",
        )
        write(self.root, "2.0.0")
        policy = {
            "exceptions": [
                {
                    "package": "pub:sample",
                    "version": "3.0.0",
                    "minimum_safe": "2.5.0",
                    "reason": "fix",
                    "advisory": "https://example.invalid/fix",
                    "expires": (NOW + timedelta(days=5)).isoformat(),
                }
            ]
        }
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, policy, NOW)
        self.assertEqual([entry[1] for entry in calls], [None, "3.0.0", None])
        self.assertIn("version: '3.0.0'", (self.root / "pubspec.lock").read_text())

    def test_pub_disallowed_baseline_fails_without_a_native_compatible_replacement(
        self,
    ):
        _, releases, calls, write, resolver = self.pub_transitive_fixture()
        write(self.root, "2.0.0")
        policy = {
            "constraints": {
                "pub:sample": {
                    "range": ">=3.0.0",
                    "reason": "Required compatibility floor",
                }
            }
        }
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "No eligible Pub transitive graph"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, policy, NOW)
        self.assertEqual([entry[1] for entry in calls], [None, "3.0.0"])

    def test_pub_new_hash_cannot_inherit_an_old_young_baseline(self):
        _, releases, calls, write, resolver = self.pub_transitive_fixture()
        write(self.root, "2.0.0", digit="f")
        with (
            patch.object(registry, "releases", return_value=[releases[1]]),
            patch.object(native.chainman, "execute", side_effect=resolver),
            self.assertRaisesRegex(ValueError, "No eligible Pub transitive graph"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual(len(calls), 1)

    def test_pub_local_override_precedence_preserves_shadowed_ranges(self):
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                root = self.root / str(sibling)
                root.mkdir()
                manifest = self.put(
                    f"{sibling}/pubspec.yaml",
                    "# publishable\nname: root\ndependencies:\n  local_probe: ^99.0.0\n  sample: ^1.0.0\n",
                )
                self.put(
                    f"{sibling}/local/pubspec.yaml",
                    "name: local_probe\nversion: 1.0.0\n",
                )
                override = "dependency_overrides:\n  local_probe:\n    path: local\n"
                owner = (
                    self.put(f"{sibling}/pubspec_overrides.yaml", override)
                    if sibling
                    else manifest
                )
                if not sibling:
                    manifest.write_text(manifest.read_text() + override)
                for path in {manifest, owner}:
                    path.chmod(0o640)
                original = {
                    p: (p.read_bytes(), p.stat().st_mode) for p in {manifest, owner}
                }
                spec = {"adapter": "flutter", "directory": str(sibling)}
                pins = native.pins(
                    self.root, spec, native.specifications(self.root, spec)
                )
                self.assertEqual([p["name"] for p in pins], ["sample"])
                with native.pub_resolution_pins(
                    self.root, [(pins[0], registry.Release("2.0.0", NOW))]
                ):
                    current = native.manifests.document(manifest)[0]
                    self.assertEqual(current["dependencies"]["local_probe"], "^99.0.0")
                    self.assertEqual(current["dependencies"]["sample"], "2.0.0")
                self.assertEqual(
                    {p: (p.read_bytes(), p.stat().st_mode) for p in original}, original
                )

    def test_pub_sibling_attributes_replace_maps_and_do_not_merge_sources(self):
        self.put(
            "pubspec.yaml",
            "name: root\ndependencies:\n  sample: ^1.0.0\ndependency_overrides:\n  sample:\n    path: missing-shadowed\n",
        )
        self.put("local/pubspec.yaml", "name: sample\n")
        sibling = self.put(
            "pubspec_overrides.yaml",
            "dependency_overrides:\n  sample:\n    path: local\n",
        )
        group = native.manifests.pub_workspace(self.root, self.root)
        self.assertEqual(group["sources"]["sample"], {"kind": "path", "path": "local"})
        self.assertEqual(group["pins"], [])
        sibling.write_text("dependency_overrides: {}\n")
        group = native.manifests.pub_workspace(self.root, self.root)
        self.assertEqual([p["name"] for p in group["pins"]], ["sample"])
        self.assertEqual(group["sources"], {})
        sibling.write_text("resolution: null\n")
        with self.assertRaises(FileNotFoundError):
            native.manifests.pub_workspace(self.root, self.root)

    def test_pub_workspace_override_uses_actual_group_from_root_or_member(self):
        self.put("pubspec.yaml", "name: root\nworkspace: [members/a, members/b]\n")
        self.put(
            "members/a/pubspec.yaml",
            "name: a\nresolution: workspace\ndependencies:\n  sample: ^99.0.0\n  b: ^1.0.0\n",
        )
        self.put(
            "members/b/pubspec.yaml",
            "name: b\nversion: 1.0.0\nresolution: workspace\ndependency_overrides:\n  sample:\n    path: ../../local\n",
        )
        self.put("local/pubspec.yaml", "name: sample\nversion: 1.0.0\n")
        for directory in (".", "members/a"):
            spec = {"adapter": "flutter", "directory": directory}
            specs = native.specifications(self.root, spec)
            self.assertEqual(specs["flutter-0"]["directory"], ".")
            self.assertEqual(native.pins(self.root, spec, specs), [])
            self.assertEqual(
                specs["flutter-0"]["pub"]["sources"]["sample"]["path"], "local"
            )
            self.assertEqual(
                specs["flutter-0"]["pub"]["sources"]["b"]["kind"], "workspace"
            )
        partial = {"adapter": "flutter", "manifests": ["pubspec.yaml"]}
        with self.assertRaisesRegex(ValueError, "complete native resolution group"):
            native.pins(self.root, partial, native.specifications(self.root, partial))
        with self.assertRaisesRegex(ValueError, "distinct resolution groups"):
            native.specifications(
                self.root, {"adapter": "flutter", "directories": [".", "members/a"]}
            )
        self.put(
            "members/a/pubspec_overrides.yaml",
            "dependency_overrides:\n  sample:\n    path: ../../local\n",
        )
        with self.assertRaisesRegex(ValueError, "Duplicate effective"):
            native.specifications(self.root, {"adapter": "flutter"})

    def test_pub_effective_workspace_and_resolution_control_membership(self):
        self.put("pubspec.yaml", "name: root\nworkspace: [missing]\n")
        sibling = self.put("pubspec_overrides.yaml", "workspace: [member]\n")
        self.put(
            "member/pubspec.yaml",
            "name: member\nresolution: workspace\ndependencies:\n  sample: ^1.0.0\n",
        )
        self.assertEqual(
            native.members(self.root, self.root, "flutter"),
            ["pubspec.yaml", "member/pubspec.yaml"],
        )
        sibling.write_text("workspace: []\n")
        self.assertEqual(
            native.members(self.root, self.root, "flutter"), ["pubspec.yaml"]
        )
        with self.assertRaisesRegex(ValueError, "no declared containing"):
            native.manifests.pub_workspace(self.root, self.root / "member")
        self.put("member/pubspec_overrides.yaml", "resolution: null\n")
        group = native.manifests.pub_workspace(self.root, self.root / "member")
        self.assertEqual(group["directory"], "member")
        self.assertEqual([p["name"] for p in group["pins"]], ["sample"])

    def test_pub_dependent_package_overrides_and_independent_roots_do_not_leak(self):
        self.put(
            "pubspec.yaml",
            "name: root\ndependencies:\n  consumer:\n    path: consumer\n  sample: ^1.0.0\n",
        )
        self.put(
            "consumer/pubspec.yaml",
            "name: consumer\ndependencies:\n  sample: ^1.0.0\ndependency_overrides:\n  sample:\n    path: ../local\n",
        )
        self.put("local/pubspec.yaml", "name: sample\n")
        spec = {"adapter": "flutter", "directories": [".", "consumer"]}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        self.assertEqual(
            [(p["file"], p["name"]) for p in pins], [("pubspec.yaml", "sample")]
        )
        with patch.object(registry, "releases", side_effect=self.inventory) as queried:
            native.choose(self.root, pins[0], spec, {}, NOW)
        queried.assert_called_once_with("pub", "sample")

    def test_pub_effective_sources_reject_unsupported_or_uncontained_inputs(self):
        cases = [
            ("dependency_overrides: []\n", ValueError),
            ("dependency_overrides:\n  sample: {path: ''}\n", ValueError),
            ("dependency_overrides:\n  sample: {path: 2}\n", ValueError),
            (
                "dependency_overrides:\n  sample: {path: local, sdk: flutter}\n",
                ValueError,
            ),
            ("dependency_overrides:\n  sample: {sdk: unknown}\n", ValueError),
            (
                "dependency_overrides:\n  sample: {git: https://example.test/repo}\n",
                ValueError,
            ),
            (
                "dependency_overrides:\n  sample: {hosted: https://example.test, version: ^1.0.0}\n",
                ValueError,
            ),
            ("dependency_overrides:\n  sample: {path: ../outside}\n", ValueError),
            ("dependency_overrides:\n  sample: {path: missing}\n", FileNotFoundError),
            ("dependency_overrides:\n  sample: {path: wrong}\n", ValueError),
            ("workspace: ['packages/*']\n", ValueError),
            ("resolution: unknown\n", ValueError),
        ]
        self.put("pubspec.yaml", "name: root\ndependencies:\n  sample: ^1.0.0\n")
        self.put("wrong/pubspec.yaml", "name: another\n")
        for body, error in cases:
            with self.subTest(body=body):
                self.put("pubspec_overrides.yaml", body)
                with self.assertRaises(error):
                    native.specifications(self.root, {"adapter": "flutter"})

    def test_pub_override_and_target_symlinks_are_rejected_before_parent_walk(self):
        manifest = self.put("pubspec.yaml", "name: root\n")
        target = self.put("local/pubspec.yaml", "name: sample\n")
        override = self.put(
            "pubspec_overrides.yaml",
            "dependency_overrides:\n  sample: {path: alias/../local}\n",
        )
        (self.root / "alias").symlink_to(self.root / "local", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            native.manifests.pub_workspace(self.root, self.root)
        override.unlink()
        override.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            native.manifests.pub_workspace(self.root, self.root)
        override.unlink()
        manifest.unlink()
        manifest.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            native.manifests.pub_workspace(self.root, self.root)

    def test_pub_hosted_override_is_narrowed_only_temporarily_with_source_ownership(
        self,
    ):
        for sibling in (False, True):
            with self.subTest(sibling=sibling):
                path, releases, _, write, _ = self.pub_transitive_fixture()
                old_sibling = self.root / "pubspec_overrides.yaml"
                old_sibling.unlink(missing_ok=True)
                override = "dependency_overrides:\n  sample: '>=1.0.0 <3.0.0'\n"
                owner = (
                    self.put("pubspec_overrides.yaml", override) if sibling else path
                )
                if not sibling:
                    path.write_text(path.read_text() + override)
                owner.chmod(0o640)
                before = {p: (p.read_bytes(), p.stat().st_mode) for p in {owner, path}}
                calls = []

                def resolve(root, profile, argv, **kwargs):
                    effective = native.manifests.document(owner)[0][
                        "dependency_overrides"
                    ]["sample"]
                    calls.append(effective)
                    self.assertEqual(effective, "1.5.0")
                    self.assertNotIn(
                        "sample",
                        native.manifests.document(path)[0].get("dev_dependencies", {}),
                    )
                    write(self.root, effective)
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="native eligible override\n"
                    )

                with (
                    patch.object(registry, "releases", return_value=releases),
                    patch.object(native.chainman, "execute", side_effect=resolve),
                ):
                    result = native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
                self.assertEqual(calls, ["1.5.0"])
                self.assertEqual(result["changed_manifests"], [])
                self.assertEqual(
                    {p: (p.read_bytes(), p.stat().st_mode) for p in before}, before
                )
                self.assertEqual(
                    result["pub_overrides"], {"flutter-0": {"sample": "1.5.0"}}
                )

    def test_pub_fixed_override_selection_and_temporary_bound_are_authoritative(self):
        self.put("pubspec.yaml", "name: root\ndependencies:\n  sample: ^99.0.0\n")
        owner = self.put(
            "pubspec_overrides.yaml", "dependency_overrides:\n  sample: 1.2.0\n"
        )
        spec = {"adapter": "flutter"}
        (pin,) = native.pins(self.root, spec, native.specifications(self.root, spec))
        with patch.object(registry, "releases", side_effect=self.inventory):
            chosen = native.choose(self.root, pin, spec, {}, NOW)
        self.assertEqual(chosen.version, "1.2.0")
        original = owner.read_bytes()
        with native.pub_resolution_pins(self.root, [(pin, chosen)]):
            self.assertEqual(native.old_requirement(self.root, pin), "1.2.0")
        self.assertEqual(owner.read_bytes(), original)
        with (
            self.assertRaisesRegex(ValueError, "within unchanged declared overrides"),
            native.pub_resolution_pins(
                self.root, [(pin, registry.Release("2.0.0", NOW))]
            ),
        ):
            self.fail("An outside-bound version must not reach the native solve")
        self.assertEqual(owner.read_bytes(), original)

    def test_pub_temporarily_narrowed_override_preserves_unexpected_postimages(self):
        self.put("pubspec.yaml", "name: root\ndependencies:\n  sample: ^99.0.0\n")
        target = self.put("unrelated.yaml", "retained target\n")
        for change in ("bytes", "mode", "symlink"):
            with self.subTest(change=change):
                owner = self.put(
                    "pubspec_overrides.yaml",
                    "dependency_overrides:\n  sample: '>=1.0.0 <3.0.0'\n",
                )
                owner.chmod(0o640)
                spec = {"adapter": "flutter"}
                (pin,) = native.pins(
                    self.root, spec, native.specifications(self.root, spec)
                )
                with (
                    self.assertRaisesRegex(ValueError, "preserve and inspect"),
                    native.pub_resolution_pins(
                        self.root, [(pin, registry.Release("1.5.0", NOW))]
                    ),
                ):
                    if change == "bytes":
                        owner.write_text("concurrent override\n")
                    elif change == "mode":
                        owner.chmod(0o600)
                    else:
                        owner.unlink()
                        owner.symlink_to(target)
                if change == "bytes":
                    self.assertEqual(owner.read_text(), "concurrent override\n")
                elif change == "mode":
                    self.assertEqual(stat.S_IMODE(owner.stat().st_mode), 0o600)
                else:
                    self.assertTrue(owner.is_symlink())
                    self.assertEqual(target.read_text(), "retained target\n")
                    owner.unlink()

    def test_pub_inline_hosted_override_cannot_escape_age_by_rewrite(self):
        path, releases, _, _, _ = self.pub_transitive_fixture()
        path.write_text(path.read_text() + "dependency_overrides:\n  sample: 2.0.0\n")
        before = path.read_bytes()
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute") as execute,
            self.assertRaisesRegex(ValueError, "declared overrides"),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        execute.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_pub_hosted_override_cannot_materialize_a_different_eligible_version(self):
        path, releases, _, write, _ = self.pub_transitive_fixture()
        owner = self.put(
            "pubspec_overrides.yaml",
            "dependency_overrides:\n  sample: '>=1.0.0 <4.0.0'\n",
        )
        before = (path.read_bytes(), owner.read_bytes())

        def wrong(root, profile, argv, **kwargs):
            write(self.root, "1.5.0")
            return subprocess.CompletedProcess(argv, 0, stdout="wrong mature version\n")

        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(native.chainman, "execute", side_effect=wrong),
            self.assertRaisesRegex(
                ValueError, "selected version within declared overrides"
            ),
        ):
            native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
        self.assertEqual((path.read_bytes(), owner.read_bytes()), before)

    def test_pub_local_and_sdk_sources_have_no_hosted_exemption(self):
        self.put(
            "pubspec.yaml",
            "name: root\ndependencies:\n  sample: ^99.0.0\n  flutter: ^99.0.0\n",
        )
        self.put(
            "pubspec_overrides.yaml",
            "dependency_overrides:\n  sample: {path: local}\n  flutter: {sdk: flutter}\n",
        )
        self.put("local/pubspec.yaml", "name: sample\nversion: 1.0.0\n")
        spec = {"adapter": "flutter"}
        original = [
            p.read_bytes()
            for p in (self.root / "pubspec.yaml", self.root / "pubspec_overrides.yaml")
        ]
        for source in ("local", "wrong-local", "hosted", "wrong-sdk"):
            with self.subTest(source=source):

                def resolve(root, profile, argv, **kwargs):
                    local = "path" if source != "hosted" else "hosted"
                    target = "wrong" if source == "wrong-local" else "local"
                    sdk = "unknown" if source == "wrong-sdk" else "flutter"
                    self.put(
                        "pubspec.lock",
                        f"packages:\n  sample:\n    source: {local}\n    version: 1.0.0\n    description:\n      path: {target}\n  flutter:\n    source: sdk\n    version: 0.0.0\n    description: {sdk}\n",
                    )
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="source observation\n"
                    )

                (self.root / "pubspec.lock").unlink(missing_ok=True)
                with (
                    patch.object(registry, "releases") as queried,
                    patch.object(native.chainman, "execute", side_effect=resolve),
                ):
                    if source == "local":
                        native.resolve(self.root, spec, {}, NOW)
                    else:
                        with self.assertRaisesRegex(
                            ValueError, "resolved source disagrees"
                        ):
                            native.resolve(self.root, spec, {}, NOW)
                    queried.assert_not_called()
                self.assertEqual(
                    [
                        p.read_bytes()
                        for p in (
                            self.root / "pubspec.yaml",
                            self.root / "pubspec_overrides.yaml",
                        )
                    ],
                    original,
                )

    def test_pub_guarded_local_manifest_and_post_hook_override_changes_are_preserved(
        self,
    ):
        self.put("pubspec.yaml", "name: root\ndependencies:\n  sample: ^99.0.0\n")
        owner = self.put(
            "pubspec_overrides.yaml", "dependency_overrides:\n  sample: {path: local}\n"
        )
        target = self.put("local/pubspec.yaml", "name: sample\nversion: 1.0.0\n")
        spec = {"adapter": "flutter"}

        def resolve(root, profile, argv, **kwargs):
            self.put("pubspec.lock", "packages: {}\n")
            target.chmod(0o600)
            return subprocess.CompletedProcess(
                argv, 0, stdout="concurrent mode change\n"
            )

        target.chmod(0o640)
        with (
            patch.object(native.chainman, "execute", side_effect=resolve),
            self.assertRaisesRegex(ValueError, "preserve and inspect"),
        ):
            native.resolve(self.root, spec, {}, NOW)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        specs = native.specifications(self.root, spec)
        frozen = native.pub_input_state(
            self.root, specs["flutter-0"]["pub"]["guarded_inputs"]
        )
        owner.write_text("dependency_overrides: {}\n")
        with self.assertRaisesRegex(ValueError, "project hook changed a guarded Pub"):
            native.audit(
                self.root,
                spec,
                {"identities": [], "resolution": {"pub_inputs": frozen}},
                {},
                NOW,
            )
        self.assertEqual(owner.read_text(), "dependency_overrides: {}\n")

    def pub_lock_output(self, directory, packages):
        path = directory / "pubspec.lock"
        path.write_text(json.dumps({"packages": packages}) + "\n")
        return path.read_bytes(), path.stat().st_mode

    def test_pub_selected_override_source_and_presence_survive_native_and_hook_audits(
        self,
    ):
        for phase in ("native", "hook"):
            for source in ("hosted", "path", "sdk", "missing"):
                with self.subTest(phase=phase, source=source):
                    manifest, releases, _, write, _ = self.pub_transitive_fixture()
                    owner = self.put(
                        "pubspec_overrides.yaml",
                        "dependency_overrides:\n  sample: '>=1.0.0 <3.0.0'\n",
                    )
                    owner.chmod(0o640)
                    self.put("shadow/pubspec.yaml", "name: sample\nversion: 1.5.0\n")
                    public = {
                        p: (p.read_bytes(), p.stat().st_mode) for p in (manifest, owner)
                    }
                    lock = self.root / "pubspec.lock"
                    lock.unlink(missing_ok=True)
                    observed = {}

                    def replace_source():
                        packages = native.manifests.document(lock)[0]["packages"]
                        if source == "path":
                            packages["sample"] = {
                                "source": "path",
                                "version": "1.5.0",
                                "description": {"path": "shadow", "relative": True},
                            }
                        elif source == "sdk":
                            packages["sample"] = {
                                "source": "sdk",
                                "version": "1.5.0",
                                "description": "flutter",
                            }
                        elif source == "missing":
                            del packages["sample"]
                        observed["lock"] = self.pub_lock_output(self.root, packages)

                    def resolve(root, profile, argv, **kwargs):
                        self.assertEqual(
                            native.manifests.document(owner)[0]["dependency_overrides"][
                                "sample"
                            ],
                            "1.5.0",
                        )
                        write(self.root, "1.5.0")
                        if phase == "native":
                            replace_source()
                        return subprocess.CompletedProcess(
                            argv, 0, stdout="observed Pub output\n"
                        )

                    with (
                        patch.object(registry, "releases", return_value=releases),
                        patch.object(native.chainman, "execute", side_effect=resolve),
                    ):

                        def evaluate():
                            result = native.resolve(
                                self.root, {"adapter": "flutter"}, {}, NOW
                            )
                            self.assertEqual(
                                result["pub_overrides"],
                                {"flutter-0": {"sample": "1.5.0"}},
                            )
                            if phase == "hook":
                                replace_source()
                                native.audit(
                                    self.root,
                                    {"adapter": "flutter"},
                                    {"identities": [], "resolution": result},
                                    {},
                                    NOW,
                                )

                        if source == "hosted":
                            evaluate()
                        else:
                            with self.assertRaisesRegex(
                                ValueError, "Selected hosted Pub override"
                            ):
                                evaluate()
                    self.assertEqual(
                        {p: (p.read_bytes(), p.stat().st_mode) for p in public}, public
                    )
                    self.assertEqual(
                        (lock.read_bytes(), lock.stat().st_mode), observed["lock"]
                    )

    def test_pub_override_only_sources_must_materialize_in_their_group(self):
        for kind in ("hosted", "path", "sdk"):
            for present in (False, True):
                with self.subTest(kind=kind, present=present):
                    root = self.root / f"{kind}-{present}"
                    root.mkdir()
                    manifest = self.put(f"{root.name}/pubspec.yaml", "name: root\n")
                    declaration = {
                        "hosted": "'1.2.0'",
                        "path": "{path: local}",
                        "sdk": "{sdk: flutter}",
                    }[kind]
                    owner = self.put(
                        f"{root.name}/pubspec_overrides.yaml",
                        f"dependency_overrides:\n  sample: {declaration}\n",
                    )
                    self.put(
                        f"{root.name}/local/pubspec.yaml",
                        "name: sample\nversion: 1.2.0\n",
                    )
                    before = {
                        p: (p.read_bytes(), p.stat().st_mode) for p in (manifest, owner)
                    }
                    release = registry.Release(
                        "1.2.0",
                        NOW - timedelta(days=60),
                        artifacts=(
                            registry.Artifact(
                                "https://pub.dev/api/archives/sample-1.2.0.tar.gz",
                                "sha256:" + "a" * 64,
                                NOW - timedelta(days=60),
                            ),
                        ),
                    )
                    item = {
                        "source": kind,
                        "version": "1.2.0",
                        "description": {
                            "hosted": {
                                "name": "sample",
                                "url": "https://pub.dev",
                                "sha256": "a" * 64,
                            },
                            "path": {"path": "local", "relative": True},
                            "sdk": "flutter",
                        }[kind],
                    }

                    def resolve(root, profile, argv, **kwargs):
                        self.pub_lock_output(
                            kwargs["cwd"], {"sample": item} if present else {}
                        )
                        return subprocess.CompletedProcess(
                            argv, 0, stdout="override-only output\n"
                        )

                    spec = {"adapter": "flutter", "directory": root.name}
                    with (
                        patch.object(
                            registry, "releases", return_value=[release]
                        ) as queried,
                        patch.object(native.chainman, "execute", side_effect=resolve),
                    ):
                        if present:
                            result = native.resolve(self.root, spec, {}, NOW)
                            native.audit(
                                self.root,
                                spec,
                                {"identities": [], "resolution": result},
                                {},
                                NOW,
                            )
                            self.pub_lock_output(root, {})
                            with self.assertRaisesRegex(
                                ValueError, "missing from its lock"
                            ):
                                native.audit(
                                    self.root,
                                    spec,
                                    {"identities": [], "resolution": result},
                                    {},
                                    NOW,
                                )
                        else:
                            with self.assertRaisesRegex(
                                ValueError, "missing from its lock"
                            ):
                                native.resolve(self.root, spec, {}, NOW)
                        if kind != "hosted":
                            queried.assert_not_called()
                    self.assertEqual(
                        {p: (p.read_bytes(), p.stat().st_mode) for p in before}, before
                    )

    def test_pub_inactive_overrides_do_not_require_nodes(self):
        self.put(
            "pubspec.yaml",
            "name: root\ndependencies:\n  consumer: {path: consumer}\ndependency_overrides:\n  shadowed: 1.2.0\n",
        )
        self.put("pubspec_overrides.yaml", "dependency_overrides: {}\n")
        self.put(
            "consumer/pubspec.yaml",
            "name: consumer\nversion: 1.0.0\ndependency_overrides:\n  dependency_only: 1.2.0\n",
        )

        def resolve(root, profile, argv, **kwargs):
            self.pub_lock_output(
                self.root,
                {
                    "consumer": {
                        "source": "path",
                        "version": "1.0.0",
                        "description": {"path": "consumer", "relative": True},
                    },
                },
            )
            return subprocess.CompletedProcess(argv, 0, stdout="effective group only\n")

        with (
            patch.object(registry, "releases") as queried,
            patch.object(native.chainman, "execute", side_effect=resolve),
        ):
            result = native.resolve(self.root, {"adapter": "flutter"}, {}, NOW)
            self.assertEqual(result["pub_overrides"], {"flutter-0": {}})
            queried.assert_not_called()

    def test_pub_workspace_members_may_be_absent_from_the_shared_lock(self):
        self.put(
            "pubspec.yaml",
            "name: root\nworkspace: [member]\ndependencies:\n  member: ^1.0.0\n",
        )
        self.put(
            "member/pubspec.yaml",
            "name: member\nversion: 1.0.0\nresolution: workspace\n",
        )

        def resolve(root, profile, argv, **kwargs):
            self.assertEqual(kwargs["cwd"], self.root)
            self.pub_lock_output(self.root, {})
            return subprocess.CompletedProcess(
                argv, 0, stdout="workspace members omitted\n"
            )

        with (
            patch.object(registry, "releases") as queried,
            patch.object(native.chainman, "execute", side_effect=resolve),
        ):
            result = native.resolve(
                self.root, {"adapter": "flutter", "directory": "member"}, {}, NOW
            )
            self.assertEqual(result["pub_overrides"], {"flutter-0": {}})
            queried.assert_not_called()

    def test_pub_hosted_override_requirements_stay_with_their_independent_group(self):
        self.put("pubspec.yaml", "name: root\ndependency_overrides:\n  sample: 1.2.0\n")
        self.put("other/pubspec.yaml", "name: other\n")
        release = registry.Release(
            "1.2.0",
            NOW - timedelta(days=60),
            artifacts=(
                registry.Artifact(
                    "https://pub.dev/api/archives/sample-1.2.0.tar.gz",
                    "sha256:" + "a" * 64,
                    NOW - timedelta(days=60),
                ),
            ),
        )

        def resolve(root, profile, argv, **kwargs):
            directory = kwargs["cwd"]
            self.pub_lock_output(
                directory,
                {
                    "sample": {
                        "source": "hosted",
                        "version": "1.2.0",
                        "description": {
                            "name": "sample",
                            "url": "https://pub.dev",
                            "sha256": "a" * 64,
                        },
                    },
                }
                if directory == self.root
                else {},
            )
            return subprocess.CompletedProcess(
                argv, 0, stdout="independent group output\n"
            )

        spec = {"adapter": "flutter", "directories": [".", "other"]}
        with (
            patch.object(registry, "releases", return_value=[release]),
            patch.object(native.chainman, "execute", side_effect=resolve),
        ):
            result = native.resolve(self.root, spec, {}, NOW)
            self.assertEqual(
                result["pub_overrides"],
                {"flutter-0": {"sample": "1.2.0"}, "flutter-1": {}},
            )
            native.audit(
                self.root, spec, {"identities": [], "resolution": result}, {}, NOW
            )

    def test_pub_authoritative_upper_and_disjunctive_bounds_select_highest_eligible(
        self,
    ):
        cases = [("<3.0.0", "2.0.0"), (">=3.0.0 <4.0.0 || >=1.0.0 <2.0.0", "1.2.0")]
        for sibling in (False, True):
            for bound, expected in cases:
                with self.subTest(sibling=sibling, bound=bound):
                    root = self.root / f"range-{sibling}-{expected}"
                    root.mkdir()
                    manifest = self.put(
                        f"{root.name}/pubspec.yaml",
                        "name: root\ndependencies:\n  sample: ^99.0.0\n",
                    )
                    declaration = f"dependency_overrides:\n  sample: '{bound}'\n"
                    owner = (
                        self.put(f"{root.name}/pubspec_overrides.yaml", declaration)
                        if sibling
                        else manifest
                    )
                    if not sibling:
                        manifest.write_text(manifest.read_text() + declaration)
                    owner.chmod(0o640)
                    before = {
                        p: (p.read_bytes(), p.stat().st_mode) for p in (manifest, owner)
                    }
                    spec = {"adapter": "flutter", "directory": root.name}
                    (pin,) = native.pins(
                        self.root, spec, native.specifications(self.root, spec)
                    )
                    with patch.object(registry, "releases", side_effect=self.inventory):
                        selected = native.choose(self.root, pin, spec, {}, NOW)
                    self.assertIsNotNone(selected)
                    self.assertEqual(selected.version, expected)
                    with native.pub_resolution_pins(self.root, [(pin, selected)]):
                        self.assertEqual(
                            native.manifests.document(owner)[0]["dependency_overrides"][
                                "sample"
                            ],
                            expected,
                        )
                        self.assertEqual(
                            native.manifests.document(manifest)[0]["dependencies"][
                                "sample"
                            ],
                            "^99.0.0",
                        )
                    self.assertEqual(
                        {p: (p.read_bytes(), p.stat().st_mode) for p in before}, before
                    )


class SwiftResolutionTests(unittest.TestCase):
    setUp = NativeTests.setUp
    put = NativeTests.put

    def swift_plan(self):
        self.put("local/Package.swift", "// local package\n")
        path = self.put(
            "Package.swift",
            "// public contract\n"
            '.package(url: "https://github.com/example/package", from: "0.63.2"),\n'
            '.package(path: "local")\n',
        )
        path.chmod(0o640)
        spec = {"adapter": "swift", "profile": "swift"}
        specs = native.specifications(self.root, spec)
        pins = native.pins(self.root, spec, specs)
        return path, spec, specs, pins

    def test_swift_compatible_zero_major_keeps_the_native_interval(self):
        _, spec, _, pins = self.swift_plan()
        inventory = [
            registry.Release(value, NOW - timedelta(days=60))
            for value in ["0.63.2", "0.65.0", "1.0.0"]
        ]
        with patch.object(registry, "releases", return_value=inventory):
            self.assertEqual(
                native.choose(
                    self.root, pins[0], {**spec, "mode": "compatible"}, {}, NOW
                ).version,
                "0.65.0",
            )
            self.assertEqual(
                native.choose(self.root, pins[0], spec, {}, NOW).version, "1.0.0"
            )

    def test_swift_temporary_exact_restores_post_selection_bytes_and_modes(self):
        path, _, _, pins = self.swift_plan()
        chosen = registry.Release("0.65.0", NOW - timedelta(days=60))
        native.manifests.replace(pins[0], chosen, self.root)
        expected = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for failed in (False, True):
            with self.subTest(failed=failed):
                try:
                    with native.swift_resolution_pins(self.root, [(pins[0], chosen)]):
                        self.assertIn(b'exact: "0.65.0"', path.read_bytes())
                        self.assertIn(b'.package(path: "local")', path.read_bytes())
                        if failed:
                            raise subprocess.CalledProcessError(
                                41,
                                ["swift", "package", "update"],
                                stderr="native conflict",
                            )
                except subprocess.CalledProcessError as error:
                    self.assertEqual(
                        (error.returncode, error.stderr), (41, "native conflict")
                    )
                self.assertEqual(
                    (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), expected
                )
                self.assertIn(b'from: "0.65.0"', path.read_bytes())

    def test_swift_temporary_conflicting_bytes_modes_and_links_are_preserved(self):
        for change in ("bytes", "mode", "symlink"):
            with self.subTest(change=change):
                path, _, _, pins = self.swift_plan()
                chosen = registry.Release("0.63.2", NOW)
                outside = self.put("outside.txt", "outside preserved\n")
                with self.assertRaisesRegex(ValueError, "changes preserved"):
                    with native.swift_resolution_pins(self.root, [(pins[0], chosen)]):
                        if change == "bytes":
                            path.write_text("concurrent edit\n")
                        elif change == "mode":
                            path.chmod(0o600)
                        else:
                            path.unlink()
                            path.symlink_to(outside)
                if change == "bytes":
                    self.assertEqual(path.read_text(), "concurrent edit\n")
                elif change == "mode":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                else:
                    self.assertTrue(path.is_symlink())
                    path.unlink()
                self.assertEqual(outside.read_text(), "outside preserved\n")

    def test_swift_partial_pin_write_restores_earlier_manifest(self):
        path, _, _, pins = self.swift_plan()
        second = self.put(
            "other/Package.swift",
            '.package(url: "https://github.com/example/other", from: "0.63.2")',
        )
        spec = {"adapter": "swift", "directories": [".", "other"]}
        pins = native.pins(self.root, spec, native.specifications(self.root, spec))
        before = path.read_bytes(), second.read_bytes()
        atomic = native.tc.atomic_bytes

        def write(target, body, mode):
            if target == second:
                raise OSError("second write failed")
            atomic(target, body, mode)

        with (
            patch.object(native.tc, "atomic_bytes", side_effect=write),
            self.assertRaisesRegex(OSError, "second write failed"),
        ):
            with native.swift_resolution_pins(
                self.root, [(pin, registry.Release("0.63.2", NOW)) for pin in pins]
            ):
                self.fail("native command must not run")
        self.assertEqual((path.read_bytes(), second.read_bytes()), before)

    def test_swift_final_direct_version_revision_and_input_substitutions_fail(self):
        path, spec, specs, _ = self.swift_plan()
        identity = (
            "swift",
            "example/package",
            "0.63.2",
            "https://github.com/example/package",
            "git:" + "a" * 40,
        )
        before = {
            "identities": [],
            "resolution": {
                "swift_inputs": native.swift_input_state(self.root, specs),
                "swift_selected": {"swift-0": {"example/package": "0.63.2"}},
                "swift_identities": {"swift-0": [list(identity)]},
            },
        }
        for identities in (
            set(),
            {(*identity[:2], "0.65.0", *identity[3:])},
            {(*identity[:4], "git:" + "b" * 40)},
        ):
            with (
                patch.object(
                    native.lock_adapters, "identities", return_value=identities
                ),
                patch.object(native.updates, "audit_locks") as audit,
            ):
                with self.subTest(identities=identities), self.assertRaises(ValueError):
                    native.audit(self.root, spec, before, {}, NOW)
                audit.assert_not_called()
        with (
            patch.object(native.lock_adapters, "identities", return_value={identity}),
            patch.object(native.updates, "audit_locks") as audit,
        ):
            native.audit(self.root, spec, before, {}, NOW)
            audit.assert_called_once()
        path.write_text(path.read_text() + "// hook edit\n")
        with self.assertRaisesRegex(ValueError, "guarded Swift inputs"):
            native.audit(self.root, spec, before, {}, NOW)

    def test_swift_native_resolve_honors_selection_and_public_result_is_serializable(
        self,
    ):
        path, spec, _, _ = self.swift_plan()
        url = "https://github.com/example/package"
        published = NOW - timedelta(days=60)
        release = registry.Release(
            "0.65.0",
            published,
            artifacts=(registry.Artifact(url, "git:" + "a" * 40, published),),
        )

        def evaluate(*args, **kwargs):
            argv = args[2]
            if "show-dependencies" in argv:
                scratch = Path(argv[argv.index("--scratch-path") + 1])
                return swift_graph_node(
                    str(self.root),
                    dependencies=[
                        swift_graph_node(
                            url, "0.65.0", path=scratch / "checkouts/package"
                        ),
                        swift_graph_node(str(self.root / "local")),
                    ],
                )
            if argv[argv.index("--package-path") + 1] == str(self.root / "local"):
                return {"dependencies": []}
            text = path.read_text()
            value = "0.65.0" if "0.65.0" in text else "0.63.2"
            requirement = (
                {"exact": [value]}
                if "exact:" in text
                else {"range": [{"lowerBound": value, "upperBound": "1.0.0"}]}
            )
            return {
                "dependencies": [
                    {
                        "sourceControl": [
                            {
                                "location": {"remote": [{"urlString": url}]},
                                "requirement": requirement,
                            }
                        ]
                    },
                    {"fileSystem": [{"path": str(self.root / "local")}]},
                ]
            }

        def execute(*args, **kwargs):
            self.assertIn('exact: "0.65.0"', path.read_text())
            self.put(
                "Package.resolved",
                json.dumps(
                    {
                        "version": 3,
                        "pins": [
                            {
                                "kind": "remoteSourceControl",
                                "location": url,
                                "state": {"version": "0.65.0", "revision": "a" * 40},
                            }
                        ],
                    }
                ),
            )

        with (
            patch.object(native.lock_adapters, "native", side_effect=evaluate),
            patch.object(registry, "releases", return_value=[release]),
            patch.object(native.lock_adapters, "evidence", return_value=[release]),
            patch.object(native.chainman, "execute", side_effect=execute),
        ):
            result = native.resolve(self.root, spec, {}, NOW)
            json.dumps(result)
            self.assertIn('from: "0.65.0"', path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(
                result["swift_selected"], {"swift-0": {"example/package": "0.65.0"}}
            )
            before = {"identities": [], "resolution": result}
            lock = self.root / "Package.resolved"
            original = lock.read_text()
            for changed in (
                original.replace("0.65.0", "0.65.1"),
                original.replace("a" * 40, "b" * 40),
            ):
                lock.write_text(changed)
                with self.assertRaises(ValueError):
                    native.audit(self.root, spec, before, {}, NOW)

    def test_swift_computed_native_inventory_fails_before_release_queries(self):
        self.put("Package.swift", "let dependencies = computedDependencies()\n")
        with (
            patch.object(
                native.lock_adapters,
                "native",
                return_value={
                    "dependencies": [{"fileSystem": [{"path": str(self.root)}]}]
                },
            ),
            patch.object(registry, "releases") as releases,
            patch.object(native.chainman, "execute") as execute,
        ):
            with self.assertRaises(ValueError):
                native.resolve(self.root, {"adapter": "swift"}, {}, NOW)
            releases.assert_not_called()
            execute.assert_not_called()

    def test_swift_explicit_exact_variables_retain_existing_supported_route(self):
        path = self.put(
            "Package.swift",
            'let repoURL = "https://github.com/example/package"\n'
            'let release: Version = "0.63.2"\n.package(url: repoURL, exact: release)\n',
        )
        path.chmod(0o640)
        pin = {
            "provider": "swift",
            "name": "example/package",
            "file": "Package.swift",
            "format": "regex",
            "pattern": r'let release: Version = "(?P<value>[^"\n]+)"',
        }
        spec = {"adapter": "swift", "pins": [pin]}
        url = "https://github.com/example/package"
        published = NOW - timedelta(days=60)
        release = registry.Release(
            "0.65.0",
            published,
            artifacts=(registry.Artifact(url, "git:" + "a" * 40, published),),
        )

        def evaluate(*args, **kwargs):
            argv = args[2]
            if "show-dependencies" in argv:
                scratch = Path(argv[argv.index("--scratch-path") + 1])
                return swift_graph_node(
                    str(self.root),
                    dependencies=[
                        swift_graph_node(
                            url, "0.65.0", path=scratch / "checkouts/package"
                        ),
                    ],
                )
            value = native.old_requirement(self.root, pin)
            return {
                "dependencies": [
                    {
                        "sourceControl": [
                            {
                                "location": {"remote": [{"urlString": url}]},
                                "requirement": {"exact": [value]},
                            }
                        ]
                    }
                ]
            }

        def execute(*args, **kwargs):
            self.assertIn('let release: Version = "0.65.0"', path.read_text())
            self.assertIn(".package(url: repoURL, exact: release)", path.read_text())
            self.put(
                "Package.resolved",
                json.dumps(
                    {
                        "version": 3,
                        "pins": [
                            {
                                "kind": "remoteSourceControl",
                                "location": url,
                                "state": {"version": "0.65.0", "revision": "a" * 40},
                            }
                        ],
                    }
                ),
            )

        with (
            patch.object(native.lock_adapters, "native", side_effect=evaluate),
            patch.object(registry, "releases", return_value=[release]),
            patch.object(native.lock_adapters, "evidence", return_value=[release]),
            patch.object(native.chainman, "execute", side_effect=execute),
        ):
            result = native.resolve(self.root, spec, {}, NOW)
            json.dumps(result)
            self.assertEqual(
                result["swift_selected"], {"swift-0": {"example/package": "0.65.0"}}
            )
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            self.assertEqual(
                path.read_text(),
                'let repoURL = "https://github.com/example/package"\n'
                'let release: Version = "0.65.0"\n.package(url: repoURL, exact: release)\n',
            )
        for changed in (
            {**pin, "name": "example/wrong"},
            {**pin, "file": "other/Package.swift"},
            {**pin, "provider": "npm"},
            {**pin, "pattern": r"(?P<value>0\.65\.0)|(?P<other>release)"},
        ):
            with (
                self.subTest(pin=changed),
                patch.object(native.lock_adapters, "native", side_effect=evaluate),
                patch.object(registry, "releases") as query,
                self.assertRaises(ValueError),
            ):
                native.resolve(
                    self.root, {"adapter": "swift", "pins": [changed]}, {}, NOW
                )
            query.assert_not_called()

    def test_swift_explicit_ownership_rejects_unused_and_unowned_native_calls(self):
        body = (
            'let repoURL = "https://github.com/example/package"\n'
            'let release: Version = "0.63.2"\n'
            'let unused: Version = "0.63.2"\n'
            ".package(url: repoURL, exact: release)\n"
        )
        path = self.put("Package.swift", body)
        pin = {
            "provider": "swift",
            "name": "example/package",
            "file": "Package.swift",
            "format": "regex",
            "pattern": r'let release: Version = "(?P<value>[^"\n]+)"',
        }
        remote = {
            "sourceControl": [
                {
                    "location": {
                        "remote": [{"urlString": "https://github.com/example/package"}]
                    },
                    "requirement": {"exact": ["0.63.2"]},
                }
            ]
        }
        cases = [
            (
                body,
                [{**pin, "pattern": pin["pattern"].replace("release", "unused")}],
                [remote],
            ),
            (
                body + ".package(url: otherURL, exact: release)\n",
                [pin],
                [
                    remote,
                    {
                        "sourceControl": [
                            {
                                "location": {
                                    "remote": [
                                        {
                                            "urlString": "https://github.com/example/other"
                                        }
                                    ]
                                },
                                "requirement": {"exact": ["0.63.2"]},
                            }
                        ]
                    },
                ],
            ),
            (body.replace("exact: release", "from: release"), [pin], [remote]),
            (
                body.replace("url: repoURL, exact: release", "path: localPath"),
                [pin],
                [],
            ),
            (body, [pin], []),
            (body, [pin], [{"fileSystem": [{"path": str(self.root)}]}]),
            (
                body,
                [pin],
                [
                    {
                        "sourceControl": [
                            {
                                "location": {
                                    "remote": [
                                        {
                                            "urlString": "https://github.com/example/package"
                                        }
                                    ]
                                },
                                "requirement": {"exact": ["0.65.0"]},
                            }
                        ]
                    }
                ],
            ),
            (
                body.replace(
                    "url: repoURL, exact: release",
                    'url: "https://github.com/example/package", exact: "0.63.2"',
                ),
                [pin],
                [remote],
            ),
            (body, [pin, {**pin, "name": "example/other"}], [remote]),
        ]
        for manifest, pins, dependencies in cases:
            with (
                self.subTest(manifest=manifest, pins=pins, dependencies=dependencies),
                patch.object(
                    native.lock_adapters,
                    "native",
                    return_value={"dependencies": dependencies},
                ),
                patch.object(registry, "releases") as query,
                self.assertRaises(ValueError),
            ):
                path.write_text(manifest)
                native.resolve(self.root, {"adapter": "swift", "pins": pins}, {}, NOW)
            query.assert_not_called()

    def test_swift_local_closure_drift_fails_even_without_selected_pins(self):
        self.put("Package.swift", '.package(path: "local")\n')
        local = self.put("local/Package.swift", "// all local dependency\n")
        local.chmod(0o640)
        spec = {"adapter": "swift"}
        specs = native.specifications(self.root, spec)
        before = {
            "identities": [],
            "resolution": {
                "swift_inputs": native.swift_input_state(self.root, specs),
                "swift_selected": {"swift-0": {}},
                "swift_identities": {"swift-0": []},
            },
        }
        self.assertEqual(
            set(before["resolution"]["swift_inputs"]),
            {"Package.swift", "local/Package.swift"},
        )
        original = local.read_bytes()
        for change in ("bytes", "mode", "symlink", "closure"):
            with self.subTest(change=change):
                if change == "bytes":
                    local.write_bytes(original + b"// changed\n")
                elif change == "mode":
                    local.chmod(0o600)
                elif change == "symlink":
                    local.unlink()
                    local.symlink_to(self.root / "Package.swift")
                else:
                    self.put("other/Package.swift", "// newly reachable\n")
                    local.write_text('.package(path: "../other")\n')
                with (
                    patch.object(native.updates, "audit_locks") as final_audit,
                    self.assertRaises(ValueError),
                ):
                    native.audit(self.root, spec, before, {}, NOW)
                final_audit.assert_not_called()
                if local.is_symlink():
                    local.unlink()
                local.write_bytes(original)
                local.chmod(0o640)


if __name__ == "__main__":
    unittest.main()
