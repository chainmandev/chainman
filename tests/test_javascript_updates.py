"""Independent workspace, solver and immutable-lock contracts for JS updates."""

import base64
import gc
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tarfile
import unittest
import weakref
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, unquote

from semantic_version import NpmSpec, Version

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import javascript_updates as js
import registry


class JavaScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="javascript fixture ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.spec = {"directory": ".", "profile": "host"}
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        self.policy = {"minimum_age_days": 30}
        self.metadata = {}
        self.real_data = registry.data
        self.write("chainman.toml", 'schema=1\n[project]\ndefault_profile="host"\n')
        self.manifest("package.json", {})
        self.write("pnpm-workspace.yaml", "packages:\n  - packages/*\n")
        mocked = patch.object(registry, "data", side_effect=self.fetch)
        mocked.start()
        self.addCleanup(mocked.stop)

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def manifest(self, path, dependencies, **extra):
        return self.write(
            path,
            json.dumps(
                {
                    "name": "neutral-" + path.replace("/", "-"),
                    "private": True,
                    "dependencies": dependencies,
                    **extra,
                },
                indent=2,
            )
            + "\n",
        )

    def release(
        self, name, version, *, days=60, peers=None, children=None, optional=None
    ):
        body = self.metadata.setdefault(
            name, {"name": name, "versions": {}, "time": {}}
        )
        checksum = base64.b64encode(
            hashlib.sha512(f"{name}@{version}".encode()).digest()
        ).decode()
        body["versions"][version] = {
            "name": name,
            "version": version,
            "dist": {
                "integrity": "sha512-" + checksum,
                "tarball": f"https://registry.npmjs.org/{quote(name, safe='')}/-/{version}.tgz",
            },
            "peerDependencies": peers or {},
            "peerDependenciesMeta": optional or {},
            "dependencies": children or {},
        }
        body["time"][version] = (self.now - timedelta(days=days)).isoformat()

    def fetch(self, url):
        self.assertTrue(url.startswith("https://registry.npmjs.org/"), url)
        name = unquote(url.removeprefix("https://registry.npmjs.org/"))
        if name not in self.metadata:
            self.fail("Unexpected registry lookup for " + name)
        return self.metadata[name]

    def selected(self):
        workspace = js.Workspace(self.root, self.spec)
        _, selected = js.plan(workspace, self.policy, self.now)
        return workspace, {
            pin.alias: version
            for pin, version in zip(workspace.pins, selected, strict=True)
        }

    def test_malformed_peer_metadata_rejects_candidate_and_tries_valid_release(self):
        self.manifest("package.json", {"renderer": "^1.0.0"})
        self.release("renderer", "1.0.0")
        self.release("renderer", "1.5.0", peers={"runtime": "^1"})
        for invalid in (
            None,
            [],
            False,
            {"runtime": None},
            {"runtime": []},
            {"runtime": {"optional": "true"}},
            {"runtime": {"optional": 1}},
        ):
            with self.subTest(metadata=invalid):
                self.metadata["renderer"]["versions"]["1.5.0"][
                    "peerDependenciesMeta"
                ] = invalid
                original = (self.root / "package.json").read_bytes()
                _, selected = self.selected()
                self.assertEqual(selected, {"renderer": "1.0.0"})
                self.assertEqual((self.root / "package.json").read_bytes(), original)

    def test_unused_older_peer_metadata_does_not_poison_valid_selection(self):
        self.manifest("package.json", {"renderer": "^1.0.0"})
        self.release("renderer", "1.0.0", peers={"runtime": "^1"})
        self.metadata["renderer"]["versions"]["1.0.0"]["peerDependenciesMeta"] = None
        self.release(
            "renderer",
            "1.5.0",
            peers={"runtime": "^1"},
            optional={
                "runtime": {"optional": True, "futureMetadata": "retained upstream"}
            },
        )
        _, selected = self.selected()
        self.assertEqual(selected, {"renderer": "1.5.0"})

    def test_required_peer_is_not_made_optional_by_metadata_projection(self):
        self.manifest("package.json", {"renderer": "1.0.0"})
        self.release(
            "renderer",
            "1.0.0",
            peers={"runtime": "^1"},
            optional={"runtime": {"optional": False}},
        )
        self.release("runtime", "2.0.0")
        with self.assertRaisesRegex(ValueError, "peer"):
            self.selected()

    def test_many_peer_alternatives_preserve_the_conjunction_without_expansion(self):
        bounds = [
            ">=5.0.0 <6.0.0",
            "^1.2.1 || ^2.0.0 || ^3.0.0-beta.0 || ^3.0.0 || ^4.0.0 || ^5.0.0-beta.0 || ^5.0.0",
            "^3.0.0 || ^4.0.0 || ^5.0.0",
            "^5.0.0",
            "^5.0.0 || ^6.0.0 || ^7.0.0",
            "^5.0.0 || ^6.0.0-alpha || ^7.0.0",
        ]
        policy = js.scoped_policy({}, "framework", bounds)
        for value, expected in (
            ("1.2.1", False),
            ("3.0.0-beta.0", False),
            ("4.99.99", False),
            ("5.0.0-beta.0", False),
            ("5.0.0", True),
            ("5.18.2", True),
            ("5.99.99", True),
            ("5.18.2+build.7", True),
            ("6.0.0-alpha", False),
            ("6.0.0", False),
            ("7.0.0", False),
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    registry.compatible(
                        "npm", value, registry.constraint("npm", policy, "framework")
                    ),
                    expected,
                )
        nested = js.scoped_policy(policy, "framework", ["<5.20.0"])
        self.assertTrue(
            registry.compatible(
                "npm", "5.19.9", registry.constraint("npm", nested, "framework")
            )
        )
        self.assertFalse(
            registry.compatible(
                "npm", "5.20.0", registry.constraint("npm", nested, "framework")
            )
        )
        self.assertFalse(
            registry.compatible(
                "npm", "6.0.0", registry.constraint("npm", nested, "framework")
            )
        )
        with self.assertRaises(ValueError):
            js.scoped_policy(policy, "framework", [f">={i}.0.0" for i in range(129)])

    def fake_pnpm(self, root, profile, argv, *, cwd, **kwargs):
        self.assertEqual(root, self.root)
        self.assertEqual(profile, "host")
        self.assertEqual(
            argv[:4], ["pnpm", "install", "--lockfile-only", "--ignore-scripts"]
        )
        self.assertNotEqual(cwd, self.root)
        if "--frozen-lockfile" in argv:
            self.assertTrue((cwd / "pnpm-lock.yaml").is_file())
            return subprocess.CompletedProcess(argv, 0, "", "")
        workspace = js.Workspace(cwd, {**self.spec, "directory": "."})
        if "--no-frozen-lockfile" in argv:
            # Installation with an existing acceptable lock preserves its graph;
            # this fixture only refreshes declaration metadata during normalization.
            path = cwd / "pnpm-lock.yaml"
            lock = js.document(path, path.read_text())[0]
            for manifest in workspace.manifests:
                importer = lock["importers"][str(Path(manifest).parent)]
                for section in js.SECTIONS:
                    for alias, requirement in (
                        workspace.documents[manifest][0].get(section, {}).items()
                    ):
                        entry = importer.get(section, {}).get(alias)
                        if entry is not None:
                            entry["specifier"] = requirement
            for catalog, entries in lock.get("catalogs", {}).items():
                ranges = (
                    workspace.settings.get("catalog", {})
                    if catalog == "default"
                    else workspace.settings.get("catalogs", {}).get(catalog, {})
                )
                for alias, entry in entries.items():
                    if alias in ranges:
                        entry["specifier"] = ranges[alias]
            if "overrides" in lock:
                lock["overrides"] = dict(workspace.settings.get("overrides", {}))
            path.write_text(json.dumps(lock))
            return subprocess.CompletedProcess(argv, 0, "", "")
        packages, snapshots, importers = {}, {}, {}

        def add(name, version):
            key = name + "@" + version
            if key in packages:
                return
            info = self.metadata[name]["versions"][version]
            packages[key] = {"resolution": {"integrity": info["dist"]["integrity"]}}
            dependencies = dict(info["dependencies"])
            for peer, bound in info["peerDependencies"].items():
                available = [
                    v
                    for v in self.metadata.get(peer, {}).get("versions", {})
                    if Version(v) in NpmSpec(bound)
                ]
                if peer not in dependencies and available:
                    dependencies[peer] = str(max(map(Version, available)))
            snapshots[key] = {"dependencies": dependencies}
            for child, chosen in dependencies.items():
                add(child, chosen)

        for path in workspace.manifests:
            importer = str(Path(path).parent)
            importers[importer] = {}
            for section in js.SECTIONS:
                resolved = {}
                for alias, requirement in (
                    workspace.documents[path][0].get(section, {}).items()
                ):
                    original = requirement
                    if requirement.startswith("catalog:"):
                        key = requirement.removeprefix("catalog:") or "default"
                        requirement = (
                            workspace.settings.get("catalog", {})
                            if key == "default"
                            else workspace.settings["catalogs"][key]
                        )[alias]
                    parsed = js.parse_requirement(alias, requirement)
                    if parsed is None:
                        resolved[alias] = {
                            "specifier": original,
                            "version": "link:../local",
                        }
                        continue
                    name, _, bound, _ = parsed
                    version = str(
                        max(
                            Version(v)
                            for v in self.metadata[name]["versions"]
                            if Version(v) in NpmSpec(bound)
                        )
                    )
                    add(name, version)
                    resolved[alias] = {
                        "specifier": original,
                        "version": version if name == alias else name + "@" + version,
                    }
                if resolved:
                    importers[importer][section] = resolved
        (cwd / "pnpm-lock.yaml").write_text(
            json.dumps(
                {
                    "lockfileVersion": "9.0",
                    "importers": importers,
                    "packages": packages,
                    "snapshots": snapshots,
                }
            )
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def resolve(self, execute=None):
        with patch.object(
            js.chainman, "execute", side_effect=execute or self.fake_pnpm
        ):
            return js.resolve(self.root, self.spec, self.policy, self.now)

    def test_malformed_native_graph_stops_at_each_boundary_without_publication(self):
        self.manifest("package.json", {"library": "^1.0.0"})
        self.release("library", "1.0.0")
        original = {
            name: (self.root / name).read_bytes()
            for name in ("package.json", "pnpm-workspace.yaml")
        }
        for stage in ("resolve", "normalize", "frozen"):
            for malformed in (
                {"lockfileVersion": "9garbage"},
                {"importers": None},
                {
                    "importers": {
                        ".": {
                            "dependencies": {
                                "library": {"specifier": "^1", "version": []}
                            }
                        }
                    }
                },
                {"packages": {"library@1.0.0": {"resolution": False}}},
                {"snapshots": {"library@1.0.0": {"dependencies": {"child": False}}}},
            ):
                calls = []

                def execute(*args, **kwargs):
                    argv = args[2]
                    current = (
                        "frozen"
                        if "--frozen-lockfile" in argv
                        else "normalize"
                        if "--no-frozen-lockfile" in argv
                        else "resolve"
                    )
                    calls.append(current)
                    result = self.fake_pnpm(*args, **kwargs)
                    if current == stage:
                        path = kwargs["cwd"] / "pnpm-lock.yaml"
                        lock = js.document(path, path.read_text())[0]
                        path.write_text(json.dumps({**lock, **malformed}))
                    return result

                with (
                    self.subTest(stage=stage, malformed=malformed),
                    self.assertRaisesRegex(ValueError, "pnpm"),
                ):
                    self.resolve(execute)
                self.assertEqual(
                    calls,
                    ["resolve", "normalize", "frozen"][
                        : ["resolve", "normalize", "frozen"].index(stage) + 1
                    ],
                )
                self.assertFalse((self.root / "pnpm-lock.yaml").exists())
                self.assertEqual(
                    {name: (self.root / name).read_bytes() for name in original},
                    original,
                )

    def test_malformed_existing_graph_is_rejected_before_native_resolution(self):
        self.manifest("package.json", {"library": "^1.0.0"})
        self.release("library", "1.0.0")
        for field in ("importers", "packages", "snapshots"):
            self.write(
                "pnpm-lock.yaml", json.dumps({"lockfileVersion": "9.0", field: False})
            )
            original = {
                name: (self.root / name).read_bytes()
                for name in ("package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml")
            }
            with (
                self.subTest(field=field),
                patch.object(js.chainman, "execute") as execute,
                self.assertRaisesRegex(ValueError, "pnpm"),
            ):
                js.resolve(self.root, self.spec, self.policy, self.now)
            execute.assert_not_called()
            self.assertEqual(
                {name: (self.root / name).read_bytes() for name in original}, original
            )

    def test_unknown_native_metadata_survives_projection_and_publication(self):
        self.manifest("package.json", {"library": "^1.0.0"})
        self.release("library", "1.0.0")

        def execute(*args, **kwargs):
            result = self.fake_pnpm(*args, **kwargs)
            if (
                "--frozen-lockfile" not in args[2]
                and "--no-frozen-lockfile" not in args[2]
            ):
                path = kwargs["cwd"] / "pnpm-lock.yaml"
                lock = js.document(path, path.read_text())[0]
                for key, entry in (
                    ("importers", "."),
                    ("packages", "library@1.0.0"),
                    ("snapshots", "library@1.0.0"),
                ):
                    lock[key][entry]["future-metadata"] = {"keep": [key, entry]}
                path.write_text(json.dumps(lock))
            return result

        self.resolve(execute)
        path = self.root / "pnpm-lock.yaml"
        lock = js.document(path, path.read_text())[0]
        for key, entry in (
            ("importers", "."),
            ("packages", "library@1.0.0"),
            ("snapshots", "library@1.0.0"),
        ):
            self.assertEqual(
                lock[key][entry]["future-metadata"], {"keep": [key, entry]}
            )

    def test_normalization_cannot_hide_drift_in_unprojected_metadata(self):
        self.manifest("package.json", {"library": "^1.0.0"})
        self.release("library", "1.0.0")
        calls = []

        def execute(*args, **kwargs):
            calls.append(args[2])
            result = self.fake_pnpm(*args, **kwargs)
            if "--no-frozen-lockfile" in args[2]:
                path = kwargs["cwd"] / "pnpm-lock.yaml"
                lock = js.document(path, path.read_text())[0]
                lock["packages"]["library@1.0.0"]["future-metadata"] = {"changed": True}
                path.write_text(json.dumps(lock))
            return result

        with self.assertRaisesRegex(
            ValueError, "changed the selected dependency graph"
        ):
            self.resolve(execute)
        self.assertEqual(len(calls), 2)
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def fake_npm(self, root, profile, argv, *, cwd, **kwargs):
        self.assertEqual(argv[0], "npm")
        if argv[1] == "ci":
            self.assertIn("--dry-run", argv)
            self.assertIn("--offline", argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.assertIn("--package-lock-only", argv)
        self.assertIn("--ignore-scripts", argv)
        self.assertTrue(any(a.startswith("--before=") for a in argv))
        workspace = js.Workspace(cwd, {**self.spec, "directory": "."})
        packages = {}

        def add(location, name, version):
            info = self.metadata[name]["versions"][version]
            packages[location] = {
                "name": name,
                "version": version,
                "resolved": info["dist"]["tarball"],
                "integrity": info["dist"]["integrity"],
                "dependencies": info["dependencies"],
            }
            for child, chosen in info["dependencies"].items():
                add(location + "/node_modules/" + child, child, chosen)

        for file in workspace.manifests:
            parent = (
                "" if Path(file).parent == Path(".") else Path(file).parent.as_posix()
            )
            value = workspace.documents[file][0]
            packages[parent] = value.copy()
            for section in js.SECTIONS:
                for alias, requirement in value.get(section, {}).items():
                    parsed = js.parse_requirement(alias, requirement)
                    if parsed is None:
                        continue
                    name, _, bound, _ = parsed
                    version = str(
                        max(
                            Version(v)
                            for v in self.metadata[name]["versions"]
                            if Version(v) in NpmSpec(bound)
                        )
                    )
                    add(
                        (parent + "/" if parent else "") + "node_modules/" + alias,
                        name,
                        version,
                    )
        (cwd / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": packages})
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    def test_latest_mature_major_and_alias_keep_declared_operators(self):
        self.manifest("package.json", {"alias": "npm:@neutral/library@^1.0.0"})
        for version, days in (("1.0.0", 100), ("2.0.0", 40), ("3.0.0", 2)):
            self.release("@neutral/library", version, days=days)
        workspace, selected = self.selected()
        self.assertEqual(selected, {"alias": "2.0.0"})
        rendered = workspace.render(tuple(selected.values()))
        self.assertEqual(
            json.loads(rendered["package.json"])["dependencies"]["alias"],
            "npm:@neutral/library@^2.0.0",
        )

    def test_manifest_catalog_prefix_and_compatible_rules_all_apply(self):
        self.manifest("package.json", {"@neutral/library": "catalog:shared"})
        self.write(
            "pnpm-workspace.yaml",
            "# retained comment\npackages: []\ncatalogs:\n  shared:\n    '@neutral/library': ^1.0.0\n",
        )
        for version in ("1.0.0", "1.3.0", "1.4.0", "2.0.0"):
            self.release("@neutral/library", version)
        self.spec["mode"] = "compatible"
        self.policy["javascript"] = {
            "mode": "aggressive",
            "prefix_constraints": [
                {"prefix": "@neutral/", "range": "<2", "reason": "shared ABI"}
            ],
            "package_constraints": {
                "package.json": {
                    "@neutral/library": {"range": "<1.4", "reason": "native bridge"}
                }
            },
            "catalog_constraints": {
                "shared": {
                    "@neutral/library": {"range": ">=1.3", "reason": "fixed interface"}
                }
            },
        }
        workspace, selected = self.selected()
        self.assertEqual(selected["@neutral/library"], "1.3.0")
        self.assertIn(
            "# retained comment",
            workspace.render(tuple(selected.values()))["pnpm-workspace.yaml"].decode(),
        )

    def test_compatible_mode_preserves_original_nonbreaking_bounds(self):
        self.spec["mode"] = "compatible"
        cases = (
            ("^0.5.16", ["0.4.1", "0.5.16", "0.5.20", "0.6.0"], "0.5.20"),
            ("0.0.3", ["0.0.2", "0.0.3", "0.0.4", "0.1.0"], "0.0.3"),
            ("~0.0.3", ["0.0.3", "0.0.4", "0.1.0"], "0.0.4"),
            ("1.2.3", ["1.2.2", "1.2.3", "1.9.0", "2.0.0"], "1.9.0"),
            ("~1.2.3", ["1.2.3", "1.2.9", "1.3.0"], "1.2.9"),
            ("^1.2.3 || ^2.4.5", ["1.9.0", "2.0.0", "2.9.0", "3.0.0"], "2.9.0"),
            (">=1.2.3 <3", ["1.2.2", "1.2.3", "2.9.0", "3.0.0"], "2.9.0"),
        )
        for requirement, versions, expected in cases:
            with self.subTest(requirement=requirement):
                self.metadata = {}
                self.manifest("package.json", {"library": requirement})
                for version in versions:
                    self.release("library", version)
                self.assertEqual(self.selected()[1]["library"], expected)
        self.metadata = {}
        self.manifest("package.json", {"library": "^1.2.3"})
        self.release("library", "1.1.0")
        with self.assertRaisesRegex(ValueError, "No eligible release"):
            self.selected()

    def test_compatible_peer_fallback_cannot_cross_the_original_zero_major_floor(self):
        self.spec["mode"] = "compatible"
        self.manifest("package.json", {"renderer": "^1.0.0", "framework": "^0.5.0"})
        self.release("renderer", "1.0.0", peers={"framework": "^0.5.0"})
        self.release("renderer", "1.1.0", peers={"framework": "^0.4.0"})
        self.release("framework", "0.4.9")
        self.release("framework", "0.5.9")
        self.assertEqual(
            self.selected()[1], {"renderer": "1.0.0", "framework": "0.5.9"}
        )

    def test_compatible_final_audits_bind_original_versions_and_declarations(self):
        for manager in ("pnpm", "npm"):
            for replacement in ("^0.6.0", "*", "^0.4.0 || ^0.5.0"):
                with self.subTest(manager=manager, replacement=replacement):
                    self.spec["manager"] = manager
                    self.spec["mode"] = "aggressive"
                    for lock in ("pnpm-lock.yaml", "package-lock.json"):
                        (self.root / lock).unlink(missing_ok=True)
                    self.metadata = {}
                    self.manifest("package.json", {"library": "^0.5.0"})
                    for version in ("0.5.0", "0.5.9"):
                        self.release("library", version)
                    if replacement == "^0.6.0":
                        self.release("library", "0.6.0")
                    before = js.snapshot(self.root, self.spec)
                    self.manifest("package.json", {"library": replacement})
                    self.resolve(self.fake_npm if manager == "npm" else self.fake_pnpm)
                    self.spec["mode"] = "compatible"
                    with self.assertRaisesRegex(ValueError, "compatible"):
                        js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_compatible_update_and_unchanged_young_artifact_keep_their_bounds(self):
        self.spec["mode"] = "compatible"
        for manager in ("pnpm", "npm"):
            with self.subTest(manager=manager):
                self.spec["manager"] = manager
                self.metadata = {}
                self.manifest("package.json", {"library": "^0.5.0"})
                self.release("library", "0.5.0")
                self.release("library", "0.5.9")
                self.resolve(self.fake_npm if manager == "npm" else self.fake_pnpm)
                self.assertEqual(
                    json.loads((self.root / "package.json").read_text())[
                        "dependencies"
                    ],
                    {"library": "^0.5.9"},
                )
                self.release("library", "0.5.9", days=1)
                self.release("library", "0.6.0")
                self.assertEqual(self.selected()[1]["library"], "0.5.9")
                self.resolve(self.fake_npm if manager == "npm" else self.fake_pnpm)

    def test_compatible_legacy_override_migration_resolves_and_audits_original_range(
        self,
    ):
        self.spec.update(mode="compatible", reconcile_policy=True)
        self.manifest(
            "package.json",
            {"utility": "^1.0.0"},
            pnpm={"overrides": {"utility": "^1.0.0"}},
        )
        modes = {"package.json": 0o640, "pnpm-workspace.yaml": 0o600}
        for relative, mode in modes.items():
            (self.root / relative).chmod(mode)
        for version in ("1.0.0", "1.2.0", "2.0.0"):
            self.release("utility", version)
        before = js.snapshot(self.root, self.spec)
        self.assertEqual(
            [row for row in before["requirements"] if "overrides" in row["pointer"]],
            [
                {
                    "file": "package.json",
                    "pointer": ["pnpm", "overrides", "utility"],
                    "name": "utility",
                    "requirement": "^1.0.0",
                }
            ],
        )
        result = self.resolve()
        self.assertEqual(
            result["selected"],
            {
                "package.json:dependencies/utility": "1.2.0",
                "pnpm-workspace.yaml:overrides/utility": "1.2.0",
            },
        )
        workspace = js.Workspace(self.root, self.spec)
        self.assertNotIn("pnpm", workspace.documents["package.json"][0])
        self.assertEqual(workspace.settings["overrides"], {"utility": "^1.2.0"})
        self.assertEqual(
            {row[2] for row in js.snapshot(self.root, self.spec)["identities"]},
            {"1.2.0"},
        )
        js.audit(self.root, self.spec, before, self.policy, self.now)
        for relative, mode in modes.items():
            self.assertEqual((self.root / relative).stat().st_mode & 0o7777, mode)

    def test_compatible_migrated_workspace_audit_rejects_escaped_direct_declaration(
        self,
    ):
        self.spec.update(mode="compatible", reconcile_policy=True)
        self.manifest(
            "package.json",
            {"utility": "^1.0.0"},
            pnpm={"overrides": {"utility": "^1.0.0"}},
        )
        for version in ("1.0.0", "1.2.0", "2.0.0"):
            self.release("utility", version)
        before = js.snapshot(self.root, self.spec)
        self.resolve()
        manifest = json.loads((self.root / "package.json").read_text())
        self.assertNotIn("pnpm", manifest)
        for replacement in ("^2.0.0", "^1.0.0 || ^2.0.0"):
            with self.subTest(replacement=replacement):
                manifest["dependencies"]["utility"] = replacement
                self.write("package.json", json.dumps(manifest, indent=2) + "\n")
                inputs = {
                    path: (path.read_bytes(), path.stat().st_mode)
                    for path in self.root.iterdir()
                    if path.is_file()
                }
                with self.assertRaisesRegex(ValueError, "original compatible range"):
                    js.audit(self.root, self.spec, before, self.policy, self.now)
                self.assertEqual(
                    {path: (path.read_bytes(), path.stat().st_mode) for path in inputs},
                    inputs,
                )

    def test_compatible_migrated_override_audit_rejects_widened_declaration(self):
        self.spec.update(mode="compatible", reconcile_policy=True)
        self.manifest(
            "package.json",
            {"utility": "^1.0.0"},
            pnpm={"overrides": {"utility": "^1.0.0"}},
        )
        for version in ("1.0.0", "1.2.0", "2.0.0"):
            self.release("utility", version)
        before = js.snapshot(self.root, self.spec)
        self.resolve()
        fixed = {
            relative: (self.root / relative).read_bytes()
            for relative in ("package.json", "pnpm-lock.yaml")
        }
        for replacement in ("^1.0.0 || ^2.0.0", "*"):
            with self.subTest(replacement=replacement):
                workspace = js.Workspace(self.root, self.spec)
                workspace.settings["overrides"]["utility"] = replacement
                (self.root / "pnpm-workspace.yaml").write_bytes(
                    workspace.render([])["pnpm-workspace.yaml"]
                )
                self.assertEqual(
                    {
                        relative: (self.root / relative).read_bytes()
                        for relative in fixed
                    },
                    fixed,
                )
                inputs = {
                    path: (path.read_bytes(), path.stat().st_mode)
                    for path in self.root.iterdir()
                    if path.is_file()
                }
                with self.assertRaisesRegex(ValueError, "original compatible range"):
                    js.audit(self.root, self.spec, before, self.policy, self.now)
                self.assertEqual(
                    {path: (path.read_bytes(), path.stat().st_mode) for path in inputs},
                    inputs,
                )

    def test_compatible_catalog_and_alias_audits_require_one_original_identity(self):
        self.spec["mode"] = "compatible"
        for manager in ("pnpm", "npm"):
            with self.subTest(manager=manager):
                self.spec["manager"] = manager
                if manager == "pnpm":
                    self.manifest("package.json", {"library": "catalog:shared"})
                    self.write(
                        "pnpm-workspace.yaml",
                        "packages: []\ncatalogs:\n  shared:\n    library: ^0.5.0\n",
                    )
                else:
                    self.manifest("package.json", {"alias": "npm:library@^0.5.0"})
                for version in ("0.5.0", "0.5.9", "0.6.0"):
                    self.release("library", version)
                before = js.snapshot(self.root, self.spec)
                self.resolve(self.fake_npm if manager == "npm" else self.fake_pnpm)
                js.audit(self.root, self.spec, before, self.policy, self.now)
                original = before["requirements"][0]
                for requirements in (
                    [],
                    [original, original],
                    [{**original, "name": "other-library"}],
                ):
                    with (
                        self.subTest(requirements=requirements),
                        self.assertRaisesRegex(ValueError, "original compatible"),
                    ):
                        js.audit(
                            self.root,
                            self.spec,
                            {**before, "requirements": requirements},
                            self.policy,
                            self.now,
                        )

    def test_complex_ranges_and_override_selectors_are_preserved(self):
        self.manifest(
            "package.json",
            {"library": ">=1 <2 || >=3 <4"},
            pnpm={
                "overrides": {
                    "parent@^2>library@<4": "~3.0.0",
                    "library@>=1 <4": "3.0.0",
                    "removed": "-",
                    "same": "$library",
                }
            },
        )
        for version in ("1.0.0", "3.0.0", "3.1.0", "4.0.0"):
            self.release("library", version)
        workspace = js.Workspace(self.root, self.spec)
        _, selected = js.plan(workspace, self.policy, self.now)
        rendered = json.loads(workspace.render(selected)["package.json"])
        self.assertEqual(rendered["dependencies"]["library"], ">=1 <2 || >=3 <4")
        self.assertEqual(
            rendered["pnpm"]["overrides"],
            {
                "parent@^2>library@<4": "~3.0.0",
                "library@>=1 <4": "3.0.0",
                "removed": "-",
                "same": "$library",
            },
        )

    def test_peer_solver_retargets_source_without_relaxing_target_constraint(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        self.release("renderer", "1.0.0", peers={"framework": "^1"})
        self.release("renderer", "2.0.0", peers={"framework": "^2"})
        self.release("framework", "1.0.0")
        self.release("framework", "2.0.0")
        self.policy["constraints"] = {
            "npm:framework": {"range": "<2", "reason": "native API"}
        }
        _, selected = self.selected()
        self.assertEqual(selected, {"renderer": "1.0.0", "framework": "1.0.0"})

    def test_large_peer_domain_advances_repairs_within_the_original_state_bound(self):
        self.manifest(
            "package.json",
            {"held": "1.0.0", "renderer": "1.0.0", "framework": "1.0.0"},
        )
        self.release("held", "1.0.0", peers={"framework": "^1"})
        self.release("renderer", "3.0.0", peers={"framework": "^3"})
        self.release("renderer", "2.0.0", peers={"framework": "^2"})
        self.release("renderer", "1.0.0", peers={"framework": "^1"})
        # Never selected: search ordering must not eagerly validate its peers.
        self.release("renderer", "0.1.0", peers={"unused": "not-a-range"})
        self.release("framework", "3.0.0")
        for patch_version in range(300):
            self.release("framework", f"1.0.{patch_version}")
        self.assertEqual(
            self.selected()[1],
            {"held": "1.0.0", "renderer": "1.0.0", "framework": "1.0.299"},
        )
        self.policy["javascript"] = {"solver_states": 3}
        self.assertEqual(self.selected()[1]["framework"], "1.0.299")
        self.policy["javascript"] = {"solver_states": 2}
        with self.assertRaisesRegex(ValueError, "state bound"):
            self.selected()

    def test_missing_peer_queries_bound_baseline_scans_and_keep_provider_identity(self):
        class CountedBaseline(set):
            examined = 0

            def __iter__(self):
                for identity in super().__iter__():
                    self.examined += 1
                    yield identity

        self.manifest("package.json", {"first": "1.0.0", "second": "1.0.0"})
        for name in ("first", "second"):
            self.release(name, "1.0.0", peers={"runtime": "^1"})
        for patch_version in range(40):
            self.release("runtime", f"1.0.{patch_version}", days=1)
        workspace = js.Workspace(self.root, self.spec)
        for pin in workspace.pins:
            pin.candidates = ["1.0.0"]
        evidence = js.Evidence(self.policy, self.now)
        baseline = CountedBaseline(
            ("npm", f"unrelated-{index}", "1.0.0", "", "hash") for index in range(500)
        )
        baseline.add(("npm", "runtime", "1.0.0", "", "hash"))
        baseline.add(("pypi", "runtime", "1.0.1", "", "hash"))
        evidence.baseline = baseline
        self.assertEqual(js.solve(workspace, evidence, {}), ("1.0.0", "1.0.0"))
        self.assertLessEqual(baseline.examined, len(baseline))
        baseline.remove(("npm", "runtime", "1.0.0", "", "hash"))
        with self.assertRaisesRegex(ValueError, "peer"):
            js.solve(workspace, evidence, {})

    def test_evidence_releases_unrelated_manifest_payloads_but_keeps_peer_oracles(self):
        class Payload(dict):
            pass

        payloads = []

        def metadata(url):
            name = unquote(url.removeprefix("https://registry.npmjs.org/"))
            body = {"name": name, "versions": {}, "time": {}}
            for index in range(32):
                value = f"1.0.{index}"
                payload = Payload(text="x" * (256 * 1024))
                payloads.append(weakref.ref(payload))
                body["versions"][value] = {
                    "name": name,
                    "version": value,
                    "readme": payload,
                    "dependencies": {"unrelated": "^4"},
                    "peerDependencies": {"runtime": "^2"},
                    "peerDependenciesMeta": {"runtime": {"optional": True}},
                    "dist": {
                        "tarball": f"https://registry.npmjs.org/{name}/-/{value}.tgz",
                        "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode(),
                    },
                }
                body["time"][value] = (self.now - timedelta(days=60)).isoformat()
            return body

        evidence = js.Evidence(self.policy, self.now)
        with patch.object(registry, "data", side_effect=metadata) as fetch:
            for index in range(4):
                name = f"neutral-{index}"
                releases, versions = evidence.get(name)
                self.assertEqual(len(releases), 32)
                self.assertEqual(set(versions), {f"1.0.{i}" for i in range(32)})
                self.assertEqual(
                    evidence.peers(name, "1.0.31"),
                    ({"runtime": "^2"}, {"runtime": {"optional": True}}),
                )
                chosen = registry.select("npm", releases, self.policy, name, self.now)
                self.assertEqual(chosen.version, "1.0.31")
                self.assertEqual(
                    chosen.artifacts[0].digest, "sha512:" + (b"x" * 64).hex()
                )
                self.assertEqual(chosen.published, self.now - timedelta(days=60))
            requests = fetch.call_count
            evidence.get("neutral-0")
            self.assertEqual(fetch.call_count, requests)
        gc.collect()
        self.assertTrue(payloads)
        self.assertFalse(any(reference() is not None for reference in payloads))

    def test_post_anchor_metadata_keeps_frozen_selection_and_cached_peers(self):
        self.release("library", "1.0.0")
        for version in ("2.0.0", "3.0.0-beta.1"):
            self.release("library", version, peers={"runtime": "^1"})
            self.metadata["library"]["time"][version] = (
                self.now + timedelta(minutes=1)
            ).isoformat()
        evidence = js.Evidence(self.policy, self.now)
        with (
            patch.object(
                registry,
                "observation_time",
                return_value=self.now + timedelta(minutes=2),
                create=True,
            ) as clock,
            patch.object(registry, "data", side_effect=self.fetch) as fetch,
        ):
            cached = evidence.get("library")
            releases, versions = cached
            self.assertEqual(set(versions), {"1.0.0", "2.0.0", "3.0.0-beta.1"})
            self.assertEqual(
                evidence.peers("library", "3.0.0-beta.1"), ({"runtime": "^1"}, {})
            )
            original = [(r.version, r.published, r.artifacts) for r in releases]
            observed_calls, requests = clock.call_count, fetch.call_count
            clock.return_value += timedelta(days=90)
            self.assertIs(evidence.get("library"), cached)
            self.assertEqual(clock.call_count, observed_calls)
            self.assertEqual(fetch.call_count, requests)
            self.assertEqual(
                [(r.version, r.published, r.artifacts) for r in releases], original
            )
            self.assertEqual(evidence.now, self.now)
            for days in (0, 30):
                self.assertEqual(
                    registry.select(
                        "npm", releases, {"minimum_age_days": days}, "library", self.now
                    ).version,
                    "1.0.0",
                )

    def test_post_anchor_metadata_still_requires_dates_identity_and_digests(self):
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        self.metadata["library"]["time"]["2.0.0"] = (
            self.now + timedelta(minutes=1)
        ).isoformat()
        original = json.dumps(self.metadata["library"])
        cases = (
            ("missing_date", "Missing|age"),
            ("naive_date", "timezone"),
            ("wrong_package", "identity"),
            ("wrong_version", "identity"),
            ("missing_digest", "digest"),
            ("malformed_digest", "digest"),
        )
        with patch.object(
            registry,
            "observation_time",
            return_value=self.now + timedelta(minutes=2),
            create=True,
        ):
            for case, error in cases:
                with self.subTest(case=case):
                    body = self.metadata["library"] = json.loads(original)
                    info = body["versions"]["2.0.0"]
                    if case == "missing_date":
                        body["time"].pop("2.0.0")
                    elif case == "naive_date":
                        body["time"]["2.0.0"] = "2026-09-07T00:01:00"
                    elif case == "wrong_package":
                        info["name"] = "another-package"
                    elif case == "wrong_version":
                        info["version"] = "2.0.1"
                    elif case == "missing_digest":
                        info["dist"].pop("integrity")
                    else:
                        info["dist"]["integrity"] = "sha512-invalid"
                    with self.assertRaisesRegex(ValueError, error):
                        js.Evidence(self.policy, self.now).get("library")

    def test_actual_future_stable_and_prerelease_metadata_is_not_cached(self):
        for version in ("2.0.0", "3.0.0-beta.1"):
            with self.subTest(version=version):
                self.metadata = {}
                self.release("library", version)
                observed = self.now + timedelta(minutes=2)
                self.metadata["library"]["time"][version] = (
                    observed + timedelta(seconds=1)
                ).isoformat()
                evidence = js.Evidence(self.policy, self.now)
                with (
                    patch.object(
                        registry, "observation_time", return_value=observed, create=True
                    ),
                    self.assertRaisesRegex(ValueError, "Future"),
                ):
                    evidence.get("library")
                self.assertNotIn("library", evidence.cache)

    def test_peer_search_retains_coordinated_endpoint_changes(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        self.release("renderer", "2.0.0", peers={"framework": "^2"})
        self.release("renderer", "1.0.0", peers={"framework": "^3"})
        self.release("framework", "1.0.0")
        self.release("framework", "3.0.0")
        workspace = js.Workspace(self.root, self.spec)
        evidence, _ = js.plan(workspace, self.policy, self.now)
        # Neither endpoint alone can repair this state; both must change.
        initial = tuple(
            "2.0.0" if pin.name == "renderer" else "1.0.0" for pin in workspace.pins
        )
        selected = js.solve(workspace, evidence, {}, initial)
        self.assertEqual(
            {
                pin.name: value
                for pin, value in zip(workspace.pins, selected, strict=True)
            },
            {"renderer": "1.0.0", "framework": "3.0.0"},
        )

    def test_npm_peer_comparator_whitespace_preserves_membership(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        for version in ("1.0.0", "1.5.0", "2.0.0"):
            self.release("framework", version)
        for bound, expected in (
            (">= 1.0.0 < 2.0.0", "1.5.0"),
            ("^ 1.0.0", "1.5.0"),
            ("~ 1.0.0", "1.0.0"),
            ("> 1.5.0", "2.0.0"),
        ):
            with self.subTest(bound=bound):
                self.release("renderer", "1.0.0", peers={"framework": bound})
                self.assertEqual(self.selected()[1]["framework"], expected)
        self.release("renderer", "1.0.0", peers={"framework": "> = 1.0.0"})
        with self.assertRaisesRegex(ValueError, "Invalid NPM"):
            self.selected()

    def test_malformed_latest_peer_metadata_retargets_to_a_valid_release(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        self.release("framework", "1.0.0")
        self.release("renderer", "1.0.0", peers={"framework": "^1"})
        self.release("renderer", "2.0.0", peers={"framework": "^1 || insiders"})
        self.assertEqual(self.selected()[1]["renderer"], "1.0.0")
        self.policy["constraints"] = {
            "npm:renderer": {"range": "2.0.0", "reason": "Exact held release"}
        }
        with self.assertRaisesRegex(ValueError, r"renderer@2\.0\.0.*insiders"):
            self.selected()
        self.policy.pop("constraints")
        self.release("renderer", "1.0.0", peers={"framework": "not-a-range"})
        with self.assertRaisesRegex(ValueError, r"renderer@2\.0\.0.*insiders"):
            self.selected()

    def test_selected_malformed_peers_still_fail_validation(self):
        self.manifest("package.json", {"renderer": "1.0.0"})
        self.release("renderer", "1.0.0", peers={"framework": "not-a-range"})
        with self.assertRaisesRegex(ValueError, "Invalid NPM"):
            self.selected()

    def test_malformed_peer_shapes_reject_candidates_with_actionable_errors(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        self.release("framework", "1.0.0")
        self.release("renderer", "1.0.0", peers={"framework": "^1"})
        self.release("renderer", "2.0.0")
        for malformed in (
            [],
            "bad",
            {"framework": None},
            {"framework": []},
            {"framework": {}},
            {"framework": 1},
        ):
            with self.subTest(malformed=malformed):
                self.metadata["renderer"]["versions"]["2.0.0"]["peerDependencies"] = (
                    malformed
                )
                self.assertEqual(self.selected()[1]["renderer"], "1.0.0")
                self.policy["constraints"] = {
                    "npm:renderer": {"range": "2.0.0", "reason": "Exact held release"}
                }
                with self.assertRaisesRegex(ValueError, r"renderer@2\.0\.0.*must be"):
                    self.selected()
                self.policy.pop("constraints")

    def peer_syntax_exception(self, manifest="package.json"):
        return {
            "manifest": manifest,
            "source": "renderer",
            "peer": "framework",
            "reason": "The tested adapter supports the stable framework API; upstream also names a non-semver channel.",
        }

    def fake_pnpm_peer_syntax(self, *args, **kwargs):
        # Supply the known installed framework graph. The ordinary fake resolver
        # parses peers itself; restore the actual malformed registry metadata
        # before either production audit observes it.
        release = self.metadata["renderer"]["versions"]["1.0.0"]
        with patch.dict(release, {"peerDependencies": {"framework": ">=1"}}):
            return self.fake_pnpm(*args, **kwargs)

    def test_peer_syntax_exception_requires_the_exact_owning_edge(self):
        self.manifest("package.json", {"renderer": "2.0.0", "framework": "1.0.0"})
        self.release("renderer", "2.0.0", peers={"framework": ">=1 || nightly"})
        self.release("framework", "1.0.0")
        self.policy["constraints"] = {
            "npm:renderer": {"range": ">=2 <3", "reason": "Adapter API"}
        }
        self.policy["javascript"] = {"peer_exceptions": [self.peer_syntax_exception()]}
        self.assertEqual(self.selected()[1]["renderer"], "2.0.0")
        for field, wrong in (
            ("manifest", "packages/app/package.json"),
            ("source", "another-renderer"),
            ("peer", "another-framework"),
        ):
            with self.subTest(field=field):
                self.policy["javascript"]["peer_exceptions"] = [
                    {**self.peer_syntax_exception(), field: wrong}
                ]
                with self.assertRaisesRegex(ValueError, "No eligible JavaScript"):
                    self.selected()
        self.policy["javascript"]["peer_exceptions"] = []
        with self.assertRaisesRegex(ValueError, "No eligible JavaScript"):
            self.selected()

    def test_peer_evidence_cache_does_not_share_importer_exceptions(self):
        self.release("renderer", "1.0.0", peers={"framework": ">=1 || nightly"})
        self.policy["javascript"] = {"peer_exceptions": [self.peer_syntax_exception()]}
        for order in (
            ("package.json", "packages/app/package.json"),
            ("packages/app/package.json", "package.json"),
        ):
            evidence = js.Evidence(self.policy, self.now)
            for manifest in order:
                with self.subTest(order=order, manifest=manifest):
                    if manifest == "package.json":
                        self.assertEqual(
                            evidence.peers("renderer", "1.0.0", manifest=manifest)[0],
                            {"framework": ">=1 || nightly"},
                        )
                    else:
                        with self.assertRaises(ValueError):
                            evidence.peers("renderer", "1.0.0", manifest=manifest)
            with self.assertRaises(ValueError):
                evidence.peers("renderer", "1.0.0")

    def test_peer_syntax_exception_preserves_structure_and_sibling_validation(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        self.release("renderer", "1.0.0")
        self.release("framework", "1.0.0")
        self.policy["javascript"] = {"peer_exceptions": [self.peer_syntax_exception()]}
        for peers in (
            [],
            None,
            {"framework": None},
            {"framework": []},
            {"framework": 1},
            {"invalid name": "^1"},
            {"framework": ">=1 || nightly", "sibling": ">=1 || invalid"},
        ):
            with self.subTest(peers=peers):
                self.metadata["renderer"]["versions"]["1.0.0"]["peerDependencies"] = (
                    peers
                )
                with self.assertRaises(ValueError):
                    self.selected()

    def test_peer_syntax_exception_covers_both_audits_without_relaxing_age(self):
        for manager in ("pnpm", "npm"):
            with self.subTest(manager=manager):
                self.spec["manager"] = manager
                self.manifest(
                    "package.json", {"renderer": "1.0.0", "framework": "1.0.0"}
                )
                self.release(
                    "renderer",
                    "1.0.0",
                    peers={"framework": ">=1 || nightly"},
                    children={"framework": "1.0.0"},
                )
                self.release("framework", "1.0.0")
                self.policy["javascript"] = {
                    "peer_exceptions": [self.peer_syntax_exception()]
                }
                self.resolve(
                    self.fake_npm if manager == "npm" else self.fake_pnpm_peer_syntax
                )
                before = js.snapshot(self.root, self.spec)
                js.audit(self.root, self.spec, before, self.policy, self.now)
                self.policy["javascript"]["peer_exceptions"] = []
                with self.assertRaises(ValueError):
                    js.audit(self.root, self.spec, before, self.policy, self.now)
                self.policy["javascript"]["peer_exceptions"] = [
                    self.peer_syntax_exception()
                ]
                self.release("framework", "1.0.0", days=1)
                with self.assertRaisesRegex(ValueError, "not mature"):
                    js.audit(
                        self.root,
                        self.spec,
                        {**before, "identities": []},
                        self.policy,
                        self.now,
                    )
                self.release("framework", "1.0.0")
                self.metadata["renderer"]["versions"]["1.0.0"]["dist"]["integrity"] = (
                    "sha512-" + base64.b64encode(b"changed".ljust(64, b"x")).decode()
                )
                with self.assertRaisesRegex(
                    ValueError, "absent from registry evidence"
                ):
                    js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_transitive_peer_syntax_exception_is_audited_for_every_importer(self):
        for manager in ("pnpm", "npm"):
            with self.subTest(manager=manager):
                self.spec["manager"] = manager
                self.manifest(
                    "package.json", {"wrapper": "1.0.0"}, workspaces=["packages/*"]
                )
                self.manifest("packages/app/package.json", {"wrapper": "1.0.0"})
                self.release("wrapper", "1.0.0", children={"renderer": "1.0.0"})
                self.release(
                    "renderer",
                    "1.0.0",
                    peers={"framework": ">=1 || nightly"},
                    children={"framework": "1.0.0"},
                )
                self.release("framework", "1.0.0")
                self.policy["javascript"] = {
                    "peer_exceptions": [self.peer_syntax_exception()]
                }
                with self.assertRaises(ValueError):
                    self.resolve(
                        self.fake_npm
                        if manager == "npm"
                        else self.fake_pnpm_peer_syntax
                    )
                self.policy["javascript"]["peer_exceptions"].append(
                    self.peer_syntax_exception("packages/app/package.json")
                )
                self.resolve(
                    self.fake_npm if manager == "npm" else self.fake_pnpm_peer_syntax
                )

    def test_shared_catalog_checks_every_importer_and_scoped_peer_exception(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "catalog:"})
        self.manifest(
            "packages/app/package.json", {"renderer": "1.0.0", "framework": "catalog:"}
        )
        self.write(
            "pnpm-workspace.yaml",
            "packages: ['packages/*']\ncatalog:\n  framework: 1.0.0\n",
        )
        self.release("renderer", "1.0.0", peers={"framework": "^1"})
        self.release("renderer", "2.0.0", peers={"framework": "^2"})
        self.release("framework", "1.0.0")
        self.policy["javascript"] = {
            "peer_exceptions": [
                {
                    "manifest": "package.json",
                    "source": "renderer",
                    "peer": "framework",
                    "reason": "root tooling uses an isolated adapter",
                }
            ]
        }
        workspace = js.Workspace(self.root, self.spec)
        _, selected = js.plan(workspace, self.policy, self.now)
        versions = {
            (p.file, p.alias): v for p, v in zip(workspace.pins, selected, strict=True)
        }
        self.assertEqual(versions["package.json", "renderer"], "2.0.0")
        self.assertEqual(versions["packages/app/package.json", "renderer"], "1.0.0")

    def test_duplicate_dev_and_peer_entries_are_reconciled(self):
        self.manifest(
            "package.json",
            {},
            devDependencies={"framework": "1.0.0"},
            peerDependencies={"framework": ">=1 <2"},
        )
        self.release("framework", "1.0.0")
        self.release("framework", "2.0.0")
        workspace = js.Workspace(self.root, self.spec)
        _, selected = js.plan(workspace, self.policy, self.now)
        self.assertEqual(selected, ("1.0.0", "1.0.0"))

    def test_missing_auto_peer_maturity_retargets_its_source(self):
        self.manifest("package.json", {"renderer": "1.0.0"})
        self.release("renderer", "1.0.0", peers={"framework": "^1"})
        self.release("renderer", "2.0.0", peers={"framework": "^2"})
        self.release("framework", "1.0.0")
        self.release("framework", "2.0.0", days=2)
        self.assertEqual(self.selected()[1], {"renderer": "1.0.0"})

    def test_complex_manifest_range_is_restored_around_exact_lock_selection(self):
        self.manifest("package.json", {"library": ">=1 <4"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        self.release("library", "3.0.0")
        self.policy["constraints"] = {
            "npm:library": {"range": "<3", "reason": "compatible ABI"}
        }
        self.resolve()
        manifest = json.loads((self.root / "package.json").read_text())
        lock = js.document(
            Path("pnpm-lock.yaml"), (self.root / "pnpm-lock.yaml").read_text()
        )[0]
        self.assertEqual(manifest["dependencies"]["library"], ">=1 <4")
        self.assertEqual(
            lock["importers"]["."]["dependencies"]["library"],
            {"specifier": ">=1 <4", "version": "2.0.0"},
        )

    def test_peer_failure_and_solver_bound_are_visible(self):
        self.manifest("package.json", {"renderer": "1.0.0", "framework": "1.0.0"})
        self.release("renderer", "1.0.0", peers={"framework": "^2"})
        self.release("framework", "1.0.0")
        with self.assertRaisesRegex(ValueError, "scoped peer"):
            self.selected()
        self.release("renderer", "2.0.0", peers={"framework": "^3"})
        self.release("framework", "2.0.0")
        self.policy["javascript"] = {"solver_states": 1}
        with self.assertRaisesRegex(ValueError, "state bound"):
            self.selected()

    def test_version_bound_patch_is_retained_and_copied_without_upgrade(self):
        self.manifest(
            "package.json",
            {"library": "1.0.0"},
            pnpm={"patchedDependencies": {"library@1.0.0": "patches/fix.patch"}},
        )
        self.write("patches/fix.patch", "neutral patch bytes\n")
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        _, selected = self.selected()
        self.assertEqual(selected["library"], "1.0.0")
        before = js.snapshot(self.root, self.spec)
        self.assertEqual(
            before["patches"]["library@1.0.0"]["sha256"],
            hashlib.sha256(b"neutral patch bytes\n").hexdigest(),
        )

    def test_toolchain_owned_dependency_retains_an_eligible_exact_pin(self):
        self.manifest("package.json", {"browser-driver": "1.0.0", "library": "1.0.0"})
        self.spec["held_dependencies"] = [
            {
                "manifest": "package.json",
                "package": "browser-driver",
                "reason": "The selected SDK supplies matching browser binaries.",
            }
        ]
        for name in ("browser-driver", "library"):
            self.release(name, "1.0.0")
            self.release(name, "2.0.0")
        _, selected = self.selected()
        self.assertEqual(selected, {"browser-driver": "1.0.0", "library": "2.0.0"})
        self.release("browser-driver", "1.0.0", days=2)
        with self.assertRaisesRegex(ValueError, "No eligible"):
            self.selected()
        self.manifest("package.json", {"browser-driver": "^1.0.0"})
        with self.assertRaisesRegex(ValueError, "existing exact"):
            js.Workspace(self.root, self.spec)

    def test_missing_dates_and_misbound_registry_metadata_fail(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.metadata["library"]["time"].clear()
        with self.assertRaisesRegex(ValueError, "publication age"):
            self.selected()
        self.release("library", "1.0.0")
        self.metadata["library"]["versions"]["1.0.0"]["name"] = "other"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.selected()

    def test_policy_overrides_use_effective_workspace_settings(self):
        self.spec["reconcile_policy"] = True
        self.write(
            "pnpm-workspace.yaml",
            "overrides:\n  utility: ^1.0.0\n  unmanaged: 2.0.0\n"
            "allowBuilds:\n  esbuild: true\n",
        )
        self.policy["javascript"] = {
            "override_constraints": {
                "utility": {"range": "^1.2.0", "reason": "Supported interface."}
            }
        }
        workspace = js.Workspace(self.root, self.spec)
        js.reconcile_policy(workspace, self.policy)
        self.assertEqual(
            workspace.settings["overrides"],
            {"utility": "^1.2.0", "unmanaged": "2.0.0"},
        )
        self.assertNotIn("pnpm", workspace.documents["package.json"][0])
        self.assertEqual(workspace.settings["allowBuilds"], {"esbuild": True})
        self.assertEqual({pin.file for pin in workspace.pins}, {"pnpm-workspace.yaml"})

    def test_policy_migrates_all_legacy_overrides_without_new_rules(self):
        self.spec["reconcile_policy"] = True
        (self.root / "pnpm-workspace.yaml").unlink()
        legacy = {
            "utility": "^1.0.0",
            "parent>transitive": "1.0.0",
            "removed": "-",
            "shared": "$library",
        }
        self.manifest(
            "package.json",
            {"library": "1.0.0"},
            pnpm={"overrides": legacy, "onlyBuiltDependencies": ["esbuild"]},
        )
        workspace = js.Workspace(self.root, self.spec)
        js.reconcile_policy(workspace, self.policy)
        self.assertEqual(workspace.settings["overrides"], legacy)
        self.assertEqual(
            workspace.documents["package.json"][0]["pnpm"],
            {"onlyBuiltDependencies": ["esbuild"]},
        )

    def test_policy_coalesces_equal_override_duplicates(self):
        self.spec["reconcile_policy"] = True
        self.manifest("package.json", {}, pnpm={"overrides": {"utility": "^1"}})
        self.write("pnpm-workspace.yaml", "overrides:\n  utility: ^1\n")
        workspace = js.Workspace(self.root, self.spec)
        js.reconcile_policy(workspace, self.policy)
        self.assertEqual(workspace.settings["overrides"], {"utility": "^1"})
        self.assertNotIn("pnpm", workspace.documents["package.json"][0])
        self.assertEqual(len(workspace.pins), 1)

    def test_policy_rejects_conflicting_duplicate_overrides_before_assignment(self):
        self.spec["reconcile_policy"] = True
        self.manifest("package.json", {}, pnpm={"overrides": {"utility": "^1"}})
        self.write("pnpm-workspace.yaml", "overrides:\n  utility: ^2\n")
        self.policy["javascript"] = {
            "override_constraints": {
                "utility": {"range": "^3", "reason": "Explicit new policy."}
            }
        }
        workspace = js.Workspace(self.root, self.spec)
        with self.assertRaisesRegex(ValueError, "Conflicting.*override"):
            js.reconcile_policy(workspace, self.policy)
        self.assertEqual(workspace.settings["overrides"], {"utility": "^2"})
        self.assertEqual(
            workspace.documents["package.json"][0]["pnpm"]["overrides"],
            {"utility": "^1"},
        )

    def test_policy_check_rejects_legacy_migration_without_writing_files(self):
        self.spec["reconcile_policy"] = True
        self.manifest("package.json", {}, pnpm={"overrides": {"utility": "^1"}})
        workspace = js.Workspace(self.root, self.spec)
        original = dict(workspace.original)
        with self.assertRaisesRegex(ValueError, "policy ranges drifted"):
            js.reconcile_policy(workspace, self.policy, check=True)
        for name, body in original.items():
            self.assertEqual((self.root / name).read_bytes(), body)

    def test_policy_check_accepts_effective_workspace_without_legacy_recreation(self):
        self.spec["reconcile_policy"] = True
        self.write("pnpm-workspace.yaml", "overrides:\n  utility: ^1\n")
        self.policy["javascript"] = {
            "override_constraints": {
                "utility": {"range": "^1", "reason": "Supported interface."}
            }
        }
        workspace = js.Workspace(self.root, self.spec)
        original = dict(workspace.original)
        js.reconcile_policy(workspace, self.policy, check=True)
        self.assertNotIn("pnpm", workspace.documents["package.json"][0])
        for name, body in original.items():
            self.assertEqual((self.root / name).read_bytes(), body)

    def test_override_migration_requires_explicit_policy_reconciliation(self):
        self.manifest("package.json", {}, pnpm={"overrides": {"utility": "^1"}})
        workspace = js.Workspace(self.root, self.spec)
        before = workspace.render([])
        js.reconcile_policy(workspace, self.policy)
        self.assertEqual(workspace.render([]), before)
        self.assertNotIn("overrides", workspace.settings)

    def test_pnpm_override_migration_does_not_change_npm_projects(self):
        self.spec.update(manager="npm", reconcile_policy=True)
        self.manifest("package.json", {}, overrides={"utility": "^1"})
        workspace = js.Workspace(self.root, self.spec)
        before = workspace.render([])
        with self.assertRaisesRegex(ValueError, "requires pnpm"):
            js.reconcile_policy(workspace, self.policy)
        self.assertEqual(workspace.render([]), before)

    def test_declarative_catalog_reconciliation_preserves_exception_ranges(self):
        self.spec["reconcile_policy"] = True
        self.manifest("package.json", {"compiler": "1.0.0"})
        self.manifest("packages/bridge/package.json", {"compiler": "^1.0.0"})
        for version in ("1.0.0", "1.2.0", "2.0.0", "2.2.0"):
            self.release("compiler", version)
        self.release("utility", "1.1.0")
        self.policy["javascript"] = {
            "catalog_constraints": {
                "default": {
                    "compiler": {"range": "^2.0.0", "reason": "Shared compiler line."}
                }
            },
            "package_constraints": {
                "packages/bridge/package.json": {
                    "compiler": {
                        "range": "^1.0.0",
                        "reason": "Native interface stays on its validated major.",
                    }
                }
            },
            "override_constraints": {
                "utility": {
                    "range": "^1.0.0",
                    "reason": "Shared transitive compatibility.",
                }
            },
        }
        self.resolve()
        workspace = js.Workspace(self.root, self.spec)
        self.assertEqual(workspace.settings["catalog"]["compiler"], "^2.0.0")
        self.assertEqual(
            workspace.documents["package.json"][0]["dependencies"]["compiler"],
            "catalog:",
        )
        self.assertEqual(
            workspace.documents["packages/bridge/package.json"][0]["dependencies"][
                "compiler"
            ],
            "^1.0.0",
        )
        self.assertEqual(
            workspace.settings["overrides"]["utility"],
            "^1.0.0",
        )
        before = js.snapshot(self.root, self.spec)
        self.manifest("packages/bridge/package.json", {"compiler": "^2.0.0"})
        with self.assertRaisesRegex(ValueError, "policy ranges drifted"):
            js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_existing_young_identity_is_retained_without_persistent_age_bypass(self):
        self.manifest("package.json", {"library": "2.0.0"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        self.resolve()
        self.release("library", "2.0.0", days=3)
        self.release("library", "3.0.0", days=1)
        self.assertEqual(self.selected()[1]["library"], "2.0.0")
        self.resolve()
        settings = js.Workspace(self.root, self.spec).settings
        self.assertEqual(settings["minimumReleaseAgeExclude"], [])
        self.policy["constraints"] = {
            "npm:library": {
                "range": "<2",
                "reason": "Explicit application compatibility boundary.",
            }
        }
        self.assertEqual(self.selected()[1]["library"], "1.0.0")

    def test_only_unchanged_prerelease_artifacts_can_survive_a_baseline(self):
        import updates

        self.manifest("package.json", {"library": "1.0.0-beta.1"})
        self.release("library", "1.0.0-beta.1")
        # Seed a pre-existing lock directly; selection may not create it.
        temporary = self.root / "seed"
        temporary.mkdir()
        (temporary / "package.json").write_bytes(
            (self.root / "package.json").read_bytes()
        )
        self.fake_pnpm(
            self.root,
            "host",
            ["pnpm", "install", "--lockfile-only", "--ignore-scripts"],
            cwd=temporary,
        )
        (self.root / "pnpm-lock.yaml").write_bytes(
            (temporary / "pnpm-lock.yaml").read_bytes()
        )
        before = js.snapshot(self.root, self.spec)
        self.assertEqual(self.selected()[1]["library"], "1.0.0-beta.1")
        self.spec["mode"] = "compatible"
        self.assertEqual(self.selected()[1]["library"], "1.0.0-beta.1")
        self.resolve()
        identities = {tuple(i) for i in before["identities"]}
        updates.audit_identities(
            self.root, identities, identities, self.policy, self.now
        )
        with self.assertRaisesRegex(ValueError, "prerelease"):
            updates.audit_identities(
                self.root, identities, set(), self.policy, self.now
            )
        tampered = {(*i[:-1], "sha512:" + "0" * 128) for i in identities}
        with self.assertRaisesRegex(ValueError, "prerelease"):
            updates.audit_identities(
                self.root, tampered, identities, self.policy, self.now
            )
        self.assertEqual(registry.releases("npm", "library"), [])
        self.assertEqual(
            len(registry.releases("npm", "library", include_prerelease=True)), 1
        )

    def test_deprecated_baseline_evidence_is_retained_without_admitting_new_artifacts(
        self,
    ):
        import updates

        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0", children={"legacy-child": "1.0.0"})
        self.release("legacy-child", "1.0.0")
        self.resolve()
        before = js.snapshot(self.root, self.spec)
        for name in ("library", "legacy-child"):
            self.metadata[name]["versions"]["1.0.0"]["deprecated"] = "Superseded API"
        self.assertEqual(self.selected()[1]["library"], "1.0.0")
        self.resolve()
        identities = set(map(tuple, before["identities"]))
        updates.audit_identities(
            self.root, identities, identities, self.policy, self.now
        )
        for original in identities:
            for changed in (
                original,
                (*original[:-1], "sha512:" + "0" * 128),
                (*original[:3], "https://example.org/other.tgz", original[4]),
            ):
                with (
                    self.subTest(changed=changed),
                    self.assertRaisesRegex(ValueError, "deprecated"),
                ):
                    updates.audit_identities(
                        self.root,
                        {changed},
                        set() if changed == original else identities,
                        self.policy,
                        self.now,
                    )
        # Even a retained tuple needs its current registry's exact hash evidence.
        self.metadata["library"]["versions"]["1.0.0"]["dist"]["integrity"] = (
            "sha512-" + base64.b64encode(b"z" * 64).decode()
        )
        with self.assertRaisesRegex(ValueError, "absent from registry"):
            updates.audit_identities(
                self.root, identities, identities, self.policy, self.now
            )

    def test_deprecation_cannot_be_waived_by_security_age_exception_or_new_selection(
        self,
    ):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.metadata["library"]["versions"]["1.0.0"]["deprecated"] = "Superseded API"
        self.policy["exceptions"] = [
            {
                "package": "npm:library",
                "version": "1.0.0",
                "minimum_safe": "1.0.0",
                "reason": "Required fix",
                "advisory": "https://example.org/advisory",
                "expires": (self.now + timedelta(days=7)).isoformat(),
            }
        ]
        self.assertEqual(registry.releases("npm", "library"), [])
        records = registry.releases("npm", "library", include_deprecated=True)
        self.assertTrue(records[0].deprecated)
        with self.assertRaisesRegex(ValueError, "eligible"):
            registry.select("npm", records, self.policy, "library", self.now)
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.selected()
        self.release("library", "2.0.0")
        self.assertEqual(self.selected()[1]["library"], "2.0.0")

    def test_deprecated_retention_still_obeys_safe_floor_constraints_and_expiry(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.resolve()
        self.metadata["library"]["versions"]["1.0.0"]["deprecated"] = "Superseded API"
        self.policy["constraints"] = {
            "npm:library": {"range": ">=2", "reason": "Adopted API minimum"}
        }
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.selected()
        self.policy.pop("constraints")
        self.policy["exceptions"] = [
            {
                "package": "npm:library",
                "version": "2.0.0",
                "minimum_safe": "2.0.0",
                "reason": "Required fix",
                "advisory": "https://example.org/advisory",
                "expires": (self.now + timedelta(days=7)).isoformat(),
            }
        ]
        with self.assertRaisesRegex(ValueError, "eligible"):
            self.selected()
        self.policy["exceptions"][0].update(
            version="1.0.0", minimum_safe="1.0.0", expires=self.now.isoformat()
        )
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.selected()

    def test_exact_security_exception_retires_when_safe_release_matures(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0", days=1)
        self.policy["exceptions"] = [
            {
                "package": "npm:library",
                "version": "2.0.0",
                "minimum_safe": "2.0.0",
                "reason": "specific fix",
                "advisory": "https://example.invalid/advisory",
                "expires": (self.now + timedelta(days=10)).isoformat(),
            }
        ]
        self.assertEqual(self.selected()[1]["library"], "2.0.0")
        self.release("library", "3.0.0")
        self.assertEqual(self.selected()[1]["library"], "3.0.0")
        self.policy["exceptions"][0]["expires"] = self.now.isoformat()
        self.assertEqual(self.selected()[1]["library"], "3.0.0")
        self.release("library", "3.0.0", days=1)
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.selected()
        self.release("library", "3.0.0")
        self.policy["exceptions"][0].pop("advisory")
        with self.assertRaisesRegex(ValueError, "advisory"):
            self.selected()

    def test_exception_retirement_respects_catalog_package_mode_and_peer_scope(self):
        for scope in ("catalog", "package", "compatible", "peer"):
            with self.subTest(scope=scope):
                self.spec = {"directory": ".", "profile": "host"}
                (self.root / "pnpm-lock.yaml").unlink(missing_ok=True)
                self.write("pnpm-workspace.yaml", "packages: []\n")
                self.manifest("package.json", {"library": "1.8.0"})
                for version, days in (("1.8.0", 60), ("1.9.0", 1), ("2.0.0", 60)):
                    self.release("library", version, days=days)
                self.policy = {
                    "minimum_age_days": 30,
                    "javascript": {},
                    "exceptions": [
                        {
                            "package": "npm:library",
                            "version": "1.9.0",
                            "minimum_safe": "1.9.0",
                            "reason": "Exact supported-line security correction.",
                            "advisory": "https://example.invalid/advisory",
                            "expires": self.now.isoformat(),
                        }
                    ],
                }
                rule = {"range": "^1.8.0", "reason": "Supported interface line."}
                if scope == "catalog":
                    self.manifest("package.json", {"library": "catalog:"})
                    self.write(
                        "pnpm-workspace.yaml",
                        "packages: []\ncatalog:\n  library: ^1.8.0\n",
                    )
                    self.policy["javascript"]["catalog_constraints"] = {
                        "default": {"library": rule}
                    }
                elif scope == "package":
                    self.policy["javascript"]["package_constraints"] = {
                        "package.json": {"library": rule}
                    }
                elif scope == "compatible":
                    self.spec["mode"] = "compatible"
                else:
                    self.manifest(
                        "package.json", {"library": "1.8.0", "renderer": "1.0.0"}
                    )
                    self.release("renderer", "1.0.0", peers={"library": "^1.8.0"})
                with self.assertRaisesRegex(ValueError, "Expired"):
                    self.selected()
                self.policy["exceptions"][0]["expires"] = (
                    self.now + timedelta(days=10)
                ).isoformat()
                self.assertEqual(self.selected()[1]["library"], "1.9.0")
                self.resolve()
                self.assertEqual(
                    js.Workspace(self.root, self.spec).settings[
                        "minimumReleaseAgeExclude"
                    ],
                    ["library@1.9.0"],
                )

    def test_resolve_installs_only_audited_files_and_serializable_baseline(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        before = js.snapshot(self.root, self.spec)
        json.dumps(before)
        result = self.resolve()
        self.assertEqual(
            json.loads((self.root / "package.json").read_text())["dependencies"][
                "library"
            ],
            "2.0.0",
        )
        self.assertIn("pnpm-lock.yaml", result["changed_files"])
        js.audit(self.root, self.spec, before, self.policy, self.now)
        self.assertEqual(
            list((self.root / ".cache/toolchain/work").glob("javascript-update-*")), []
        )

    def native_override_normalization_fixture(self, fault=None):
        self.spec["reconcile_policy"] = True
        self.manifest(
            "package.json",
            {"utility": "^7.0.0", "unrelated": "1.0.0"},
            pnpm={"overrides": {"utility": "^6.0.0"}},
        )
        self.policy["javascript"] = {
            "override_constraints": {
                "utility": {"range": "^6.0.0", "reason": "Supported interface."}
            }
        }
        for version in ("6.0.0", "6.1.0", "7.0.0"):
            self.release("utility", version)
        self.release("unrelated", "1.0.0")
        calls = []
        original = {
            path: (path.read_bytes(), path.stat().st_mode)
            for path in self.root.iterdir()
            if path.is_file()
        }

        def package(name, version):
            return {
                "resolution": {
                    "integrity": self.metadata[name]["versions"][version]["dist"][
                        "integrity"
                    ]
                }
            }

        def execute(root, profile, argv, *, cwd, **kwargs):
            self.assertEqual(root, self.root)
            self.assertEqual(profile, "host")
            self.assertIn("--lockfile-only", argv)
            self.assertIn("--ignore-scripts", argv)
            self.assertNotEqual(cwd, self.root)
            path = cwd / "pnpm-lock.yaml"
            if "--frozen-lockfile" in argv:
                calls.append("frozen")
                lock = js.document(path, path.read_text())[0]
                accepted = (
                    lock["importers"]["."]["dependencies"]["utility"]["specifier"]
                    == "^6.0.0"
                )
                return subprocess.CompletedProcess(
                    argv,
                    0 if accepted else 1,
                    "",
                    ""
                    if accepted
                    else "ERR_PNPM_OUTDATED_LOCKFILE: effective override specifier differs",
                )
            if not calls:
                calls.append("resolve")
                lock = {
                    "lockfileVersion": "9.0",
                    "overrides": {"utility": "6.1.0"},
                    "importers": {
                        ".": {
                            "dependencies": {
                                "utility": {"specifier": "6.1.0", "version": "6.1.0"},
                                "unrelated": {"specifier": "1.0.0", "version": "1.0.0"},
                            }
                        }
                    },
                    "packages": {
                        "utility@6.1.0": package("utility", "6.1.0"),
                        "unrelated@1.0.0": package("unrelated", "1.0.0"),
                    },
                    "snapshots": {"utility@6.1.0": {}, "unrelated@1.0.0": {}},
                }
            else:
                self.assertEqual(calls, ["resolve"])
                self.assertIn("--no-frozen-lockfile", argv)
                calls.append("normalize")
                lock = js.document(path, path.read_text())[0]
                lock["overrides"] = {"utility": "^6.0.0"}
                lock["importers"]["."]["dependencies"]["utility"]["specifier"] = (
                    "^6.0.0"
                )
                if fault == "identity":
                    lock["importers"]["."]["dependencies"]["utility"]["version"] = (
                        "6.0.0"
                    )
                    del lock["packages"]["utility@6.1.0"]
                    lock["packages"]["utility@6.0.0"] = package("utility", "6.0.0")
                    del lock["snapshots"]["utility@6.1.0"]
                    lock["snapshots"]["utility@6.0.0"] = {}
                elif fault == "edge":
                    lock["snapshots"]["utility@6.1.0"]["dependencies"] = {
                        "unrelated": "1.0.0"
                    }
            path.write_text(json.dumps(lock))
            return subprocess.CompletedProcess(argv, 0, "", "")

        return execute, calls, original

    def test_native_override_normalization_preserves_selected_graph_before_freeze(self):
        execute, calls, original = self.native_override_normalization_fixture()
        before = js.snapshot(self.root, self.spec)
        result = self.resolve(execute)
        self.assertEqual(calls, ["resolve", "normalize", "frozen"])
        self.assertEqual(result["resolution_attempts"], 1)
        workspace = js.Workspace(self.root, self.spec)
        self.assertEqual(
            workspace.documents["package.json"][0]["dependencies"]["utility"],
            "^7.0.0",
        )
        self.assertEqual(workspace.settings["overrides"], {"utility": "^6.0.0"})
        self.assertEqual(
            {
                (row[1], row[2])
                for row in js.snapshot(self.root, self.spec)["identities"]
            },
            {("utility", "6.1.0"), ("unrelated", "1.0.0")},
        )
        js.audit(self.root, self.spec, before, self.policy, self.now)
        for path, (_, mode) in original.items():
            self.assertEqual(path.stat().st_mode, mode)

    def test_native_normalization_rejects_mature_identity_drift_before_freeze(self):
        execute, calls, original = self.native_override_normalization_fixture(
            "identity"
        )
        with self.assertRaises(ValueError):
            self.resolve(execute)
        self.assertEqual(calls, ["resolve", "normalize"])
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mode) for path in original},
            original,
        )
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def test_native_normalization_rejects_graph_only_drift_before_freeze(self):
        execute, calls, original = self.native_override_normalization_fixture("edge")
        with self.assertRaises(ValueError):
            self.resolve(execute)
        self.assertEqual(calls, ["resolve", "normalize"])
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mode) for path in original},
            original,
        )
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def test_native_normalization_failure_stops_before_freeze_and_publication(self):
        execute, calls, original = self.native_override_normalization_fixture()

        def fail(*args, **kwargs):
            result = execute(*args, **kwargs)
            if "--no-frozen-lockfile" in args[2]:
                return subprocess.CompletedProcess(
                    args[2], 41, "", "neutral normalization failure"
                )
            return result

        with self.assertRaisesRegex(ValueError, "normalization"):
            self.resolve(fail)
        self.assertEqual(calls, ["resolve", "normalize"])
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mode) for path in original},
            original,
        )
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def test_native_normalization_rejects_manifest_drift_before_freeze(self):
        execute, calls, original = self.native_override_normalization_fixture()

        def mutate(*args, **kwargs):
            result = execute(*args, **kwargs)
            if "--no-frozen-lockfile" in args[2]:
                (kwargs["cwd"] / "package.json").write_text("unexpected native edit\n")
            return result

        with self.assertRaisesRegex(ValueError, "declared resolver input"):
            self.resolve(mutate)
        self.assertEqual(calls, ["resolve", "normalize"])
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mode) for path in original},
            original,
        )
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def test_baseline_maturity_exclusions_survive_normalization_then_retire(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.resolve()
        self.release("library", "1.0.0", days=1)
        self.release("library", "2.0.0", days=1)
        before = js.snapshot(self.root, self.spec)
        phases = []

        def observe(*args, **kwargs):
            workspace = js.Workspace(kwargs["cwd"], self.spec)
            argv = args[2]
            phase = (
                "frozen"
                if "--frozen-lockfile" in argv
                else "normalize"
                if "--no-frozen-lockfile" in argv
                else "resolve"
            )
            phases.append(phase)
            self.assertEqual(
                workspace.settings["minimumReleaseAgeExclude"],
                [] if phase == "frozen" else ["library@1.0.0"],
            )
            return self.fake_pnpm(*args, **kwargs)

        self.resolve(observe)
        self.assertEqual(phases, ["resolve", "normalize", "frozen"])
        self.assertEqual(
            js.snapshot(self.root, self.spec)["identities"], before["identities"]
        )
        self.assertEqual(
            js.Workspace(self.root, self.spec).settings["minimumReleaseAgeExclude"], []
        )
        js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_failed_resolution_and_unexpected_source_edit_do_not_install_plan(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        original = (self.root / "package.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "without a narrowly"):
            self.resolve(
                lambda *a, **k: subprocess.CompletedProcess(
                    a, 1, "", "ERR_PNPM_FETCH_403"
                )
            )
        self.assertEqual((self.root / "package.json").read_bytes(), original)

        def concurrent(*args, **kwargs):
            result = self.fake_pnpm(*args, **kwargs)
            (self.root / "package.json").write_text("concurrent edit\n")
            return result

        with self.assertRaisesRegex(ValueError, "concurrently"):
            self.resolve(concurrent)
        self.assertEqual((self.root / "package.json").read_text(), "concurrent edit\n")
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def test_maturity_fallback_changes_only_the_attributed_parent(self):
        self.manifest("package.json", {"library": "1.0.0", "unrelated": "1.0.0"})
        for name in ("library", "unrelated"):
            self.release(name, "1.0.0")
            self.release(name, "2.0.0")
        calls = []

        def solve(*args, **kwargs):
            calls.append(kwargs["cwd"])
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    args,
                    1,
                    "",
                    "minimumReleaseAge constraint\nThis error happened while installing the dependencies of library@2.0.0\n",
                )
            return self.fake_pnpm(*args, **kwargs)

        result = self.resolve(solve)
        self.assertEqual(result["resolution_attempts"], 2)
        self.assertEqual(
            json.loads((self.root / "package.json").read_text())["dependencies"],
            {"library": "1.0.0", "unrelated": "2.0.0"},
        )

    def test_unattributed_and_outside_importer_failures_cannot_trigger_fallback(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        workspace = js.Workspace(self.root, self.spec)
        _, selected = js.plan(workspace, self.policy, self.now)
        for message in (
            "network failure library@2.0.0",
            "minimumReleaseAge constraint dependencies of different@2.0.0",
            "Version 2 of library does not meet the minimumReleaseAge constraint\nThis error happened while installing a direct dependency of /outside",
        ):
            self.assertIsNone(js.fallback(workspace, selected, message, self.root))

    def test_artifact_tampering_and_new_immature_transitives_fail_before_install(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0", children={"transitive": "1.0.0"})
        self.release("transitive", "1.0.0", days=2)
        with self.assertRaisesRegex(ValueError, "not mature"):
            self.resolve()
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())
        self.release("transitive", "1.0.0")

        def tamper(*args, **kwargs):
            result = self.fake_pnpm(*args, **kwargs)
            path = kwargs["cwd"] / "pnpm-lock.yaml"
            body = json.loads(path.read_text())
            body["packages"]["library@1.0.0"]["resolution"]["integrity"] = (
                "sha512-" + base64.b64encode(b"x" * 64).decode()
            )
            path.write_text(json.dumps(body))
            return result

        with self.assertRaisesRegex(ValueError, "absent from registry evidence"):
            self.resolve(tamper)
        self.assertFalse((self.root / "pnpm-lock.yaml").exists())

    def test_invalid_registry_edges_are_not_published_after_native_success(self):
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0", children={"transitive": "1.0.0"})
        self.release("transitive", "1.0.0")
        self.release("transitive", "2.0.0")
        original = (self.root / "package.json").read_bytes()
        for fault in ("wrong version", "missing edge"):
            with self.subTest(fault=fault):

                def malformed(*args, **kwargs):
                    result = self.fake_pnpm(*args, **kwargs)
                    path = kwargs["cwd"] / "pnpm-lock.yaml"
                    lock = json.loads(path.read_text())
                    dependencies = lock["snapshots"]["library@1.0.0"]["dependencies"]
                    if fault == "missing edge":
                        dependencies.pop("transitive", None)
                    else:
                        dependencies["transitive"] = "2.0.0"
                        lock["snapshots"]["transitive@2.0.0"] = {}
                        lock["packages"]["transitive@2.0.0"] = {
                            "resolution": self.metadata["transitive"]["versions"][
                                "2.0.0"
                            ]["dist"]
                        }
                    path.write_text(json.dumps(lock))
                    return result

                with self.assertRaisesRegex(ValueError, "library>transitive"):
                    self.resolve(malformed)
                self.assertFalse((self.root / "pnpm-lock.yaml").exists())
                self.assertEqual((self.root / "package.json").read_bytes(), original)

    def test_resolved_transitive_peer_is_checked_per_importer(self):
        self.manifest("package.json", {"renderer": "1.0.0"})
        self.release(
            "renderer",
            "1.0.0",
            peers={"framework": "^2"},
            children={"framework": "1.0.0"},
        )
        self.release("framework", "1.0.0")
        self.release("framework", "2.0.0")
        with self.assertRaisesRegex(ValueError, "Incompatible resolved peer"):
            self.resolve()
        self.policy["javascript"] = {
            "peer_exceptions": [
                {
                    "manifest": "package.json",
                    "source": "renderer",
                    "peer": "framework",
                    "reason": "reviewed adapter supplies compatibility",
                }
            ]
        }
        self.resolve()

    def test_paths_and_catalog_references_fail_closed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "packages").symlink_to(outside, target_is_directory=True)
        self.spec["manifests"] = ["packages/package.json"]
        (outside / "package.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "symlink"):
            js.snapshot(self.root, self.spec)
        self.spec["manifests"] = ["package.json"]
        self.manifest("package.json", {"library": "catalog:missing"})
        with self.assertRaisesRegex(ValueError, "missing catalog"):
            js.snapshot(self.root, self.spec)

    def test_npm_workspace_alias_updates_and_restores_declared_ranges(self):
        self.spec["manager"] = "npm"
        self.manifest("package.json", {}, workspaces=["packages/*"])
        self.manifest("packages/app/package.json", {"alias": "npm:library@>=1 <3"})
        self.release("library", "1.0.0")
        self.release("library", "2.0.0")
        self.policy["javascript"] = {
            "package_constraints": {
                "packages/app/package.json": {
                    "alias": {"range": "<2", "reason": "native ABI"}
                }
            }
        }
        result = self.resolve(self.fake_npm)
        self.assertIn("package-lock.json", result["changed_files"])
        lock = json.loads((self.root / "package-lock.json").read_text())
        self.assertEqual(
            lock["packages"]["packages/app"]["dependencies"]["alias"],
            "npm:library@>=1 <3",
        )
        self.assertEqual(
            lock["packages"]["packages/app/node_modules/alias"]["version"], "1.0.0"
        )
        self.assertNotIn("pnpm-workspace.yaml", result["changed_files"])

    def test_npm_invalid_resolver_entry_stops_before_ci_or_publication(self):
        self.spec["manager"] = "npm"
        manifest = self.manifest("package.json", {"library": "^1.0.0"})
        self.release("library", "2.0.0")
        original = manifest.read_bytes()
        calls = []

        def malformed(root, profile, argv, *, cwd, **kwargs):
            calls.append(argv[1])
            result = self.fake_npm(root, profile, argv, cwd=cwd, **kwargs)
            if argv[1] == "install":
                path = cwd / "package-lock.json"
                lock = json.loads(path.read_text())
                lock["packages"]["node_modules/library"] = []
                path.write_text(json.dumps(lock))
            return result

        with self.assertRaisesRegex(ValueError, "npm package.*must be an object"):
            self.resolve(malformed)
        self.assertEqual(calls, ["install"])
        self.assertEqual(manifest.read_bytes(), original)
        self.assertFalse((self.root / "package-lock.json").exists())

    def test_npm_publication_preserves_native_metadata_while_restoring_ranges(self):
        self.spec["manager"] = "npm"
        self.manifest("package.json", {"library": "^1.0.0"})
        self.release("library", "2.0.0")
        extension = {"future-native-field": ["keep", {"nested": True}]}

        def extended(root, profile, argv, *, cwd, **kwargs):
            result = self.fake_npm(root, profile, argv, cwd=cwd, **kwargs)
            if argv[1] == "install":
                path = cwd / "package-lock.json"
                lock = json.loads(path.read_text())
                lock["native-metadata"] = extension
                for entry in lock["packages"].values():
                    entry["native-metadata"] = extension
                path.write_text(json.dumps(lock))
            return result

        result = self.resolve(extended)
        self.assertIn("package-lock.json", result["changed_files"])
        lock = json.loads((self.root / "package-lock.json").read_text())
        self.assertEqual(lock["native-metadata"], extension)
        for entry in lock["packages"].values():
            self.assertEqual(entry["native-metadata"], extension)
        self.assertEqual(lock["packages"][""]["dependencies"], {"library": "^2.0.0"})
        self.assertEqual(lock["packages"]["node_modules/library"]["version"], "2.0.0")

    def test_npm_v2_and_v3_identity_and_unrecognized_sources(self):
        import javascript_npm

        self.spec["manager"] = "npm"
        self.manifest("package.json", {"library": "1.0.0"})
        self.release("library", "1.0.0")
        self.resolve(self.fake_npm)
        path = self.root / "package-lock.json"
        body = json.loads(path.read_text())
        for version in (2, 3):
            body["lockfileVersion"] = version
            path.write_text(json.dumps(body))
            identities = javascript_npm.identities(js.Workspace(self.root, self.spec))
            self.assertEqual(len(identities), 1)
        body["packages"]["node_modules/library"]["resolved"] = (
            "git+https://example.invalid/library"
        )
        path.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            js.snapshot(self.root, self.spec)

    def test_npm_immature_transitive_and_required_peer_fail_audit(self):
        self.spec["manager"] = "npm"
        self.manifest("package.json", {"renderer": "1.0.0"})
        self.release(
            "renderer",
            "1.0.0",
            children={"framework": "1.0.0"},
        )
        self.release("framework", "1.0.0", days=2)
        self.release("framework", "2.0.0")
        with self.assertRaisesRegex(ValueError, "not mature"):
            self.resolve(self.fake_npm)
        self.release("framework", "1.0.0")
        self.release(
            "renderer",
            "1.0.0",
            children={"framework": "1.0.0"},
            peers={"framework": "^2"},
        )
        with self.assertRaisesRegex(ValueError, "incompatible npm peer"):
            self.resolve(self.fake_npm)
        self.assertFalse((self.root / "package-lock.json").exists())

    def test_npm_local_alias_and_graph_bind_to_declared_manifests(self):
        self.spec["manager"] = "npm"
        self.manifest(
            "package.json", {"local-a": "file:packages/a"}, workspaces=["packages/*"]
        )
        self.manifest(
            "packages/a/package.json",
            {"local-b": "file:../b"},
            name="local-a",
            version="1.0.0",
        )
        self.manifest("packages/b/package.json", {}, name="local-b", version="1.0.0")
        lock = {
            "lockfileVersion": 3,
            "packages": {
                "": {"dependencies": {"local-a": "file:packages/a"}},
                "packages/a": {
                    "name": "local-a",
                    "version": "1.0.0",
                    "dependencies": {"local-b": "file:../b"},
                },
                "packages/b": {"name": "local-b", "version": "1.0.0"},
                "node_modules/local-a": {"link": True, "resolved": "packages/a"},
                "node_modules/local-b": {"link": True, "resolved": "packages/b"},
            },
        }
        path = self.write("package-lock.json", json.dumps(lock))
        before = js.snapshot(self.root, self.spec)
        js.audit(self.root, self.spec, before, self.policy, self.now)
        lock["packages"]["node_modules/local-a"]["resolved"] = "packages/b"
        path.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "local dependency|link alias"):
            js.audit(self.root, self.spec, before, self.policy, self.now)
        lock["packages"]["node_modules/local-a"]["resolved"] = "packages/a"
        lock["packages"]["packages/a"]["dependencies"] = {}
        path.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "local dependency declarations"):
            js.audit(self.root, self.spec, before, self.policy, self.now)
        lock["packages"]["packages/a"]["dependencies"] = {"local-b": "file:../b"}
        lock["packages"]["packages/b"]["peerDependencies"] = {"local-a": "^2.0.0"}
        self.manifest(
            "packages/b/package.json",
            {},
            name="local-b",
            version="1.0.0",
            peerDependencies={"local-a": "^2.0.0"},
        )
        path.write_text(json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "incompatible npm peer"):
            js.audit(self.root, self.spec, before, self.policy, self.now)

    def local_directory_fixture(self):
        self.manifest("package.json", {"local-provider": "file:packages/provider"})
        self.manifest(
            "packages/provider/package.json", {}, name="local-provider", version="1.0.0"
        )
        lock = {
            "lockfileVersion": "9.0",
            "importers": {
                ".": {
                    "dependencies": {
                        "local-provider": {
                            "specifier": "file:packages/provider",
                            "version": "file:packages/provider",
                        }
                    }
                },
                "packages/provider": {},
            },
            "packages": {
                "local-provider@file:packages/provider": {
                    "resolution": {
                        "directory": "packages/provider",
                        "type": "directory",
                    }
                }
            },
            "snapshots": {"local-provider@file:packages/provider": {}},
        }
        self.write("pnpm-lock.yaml", json.dumps(lock))
        return lock

    def test_local_directory_sources_bind_declared_workspace_manifests(self):
        lock = self.local_directory_fixture()
        before = js.snapshot(self.root, self.spec)
        self.assertEqual(before["identities"], [])
        js.audit(self.root, self.spec, before, self.policy, self.now)
        for fault in ("../outside", "packages/missing"):
            with self.subTest(fault=fault):
                self.manifest("package.json", {"local-provider": "file:" + fault})
                with self.assertRaisesRegex(ValueError, "escapes|declared workspace"):
                    js.snapshot(self.root, self.spec)
        self.local_directory_fixture()
        self.manifest(
            "packages/provider/package.json", {}, name="different-name", version="1.0.0"
        )
        with self.assertRaisesRegex(ValueError, "declared workspace"):
            js.snapshot(self.root, self.spec)
        self.local_directory_fixture()
        lock["packages"]["local-provider@file:packages/provider"]["resolution"][
            "directory"
        ] = "../outside"
        self.write("pnpm-lock.yaml", json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "package key"):
            js.snapshot(self.root, self.spec)
        self.local_directory_fixture()
        lock["snapshots"]["local-provider@file:packages/provider"] = {
            "dependencies": {"undeclared": "1.0.0"}
        }
        self.write("pnpm-lock.yaml", json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "graph differs"):
            js.audit(self.root, self.spec, before, self.policy, self.now)

    def test_local_peer_whitespace_survives_scoped_artifact_audit(self):
        lock = self.local_directory_fixture()
        bound = ">= 1.0.0 < 2.0.0"
        path = self.manifest(
            "packages/provider/package.json",
            {},
            name="local-provider",
            version="1.0.0",
            peerDependencies={"framework": bound},
        )
        original = path.read_bytes()
        self.release("framework", "1.5.0")
        lock["importers"]["packages/provider"] = {
            "peerDependencies": {"framework": {"specifier": bound, "version": "1.5.0"}}
        }
        lock["packages"]["framework@1.5.0"] = {
            "resolution": {
                "integrity": self.metadata["framework"]["versions"]["1.5.0"]["dist"][
                    "integrity"
                ]
            }
        }
        lock["snapshots"]["framework@1.5.0"] = {}
        lock["snapshots"]["local-provider@file:packages/provider"] = {
            "dependencies": {"framework": "1.5.0"}
        }
        self.write("pnpm-lock.yaml", json.dumps(lock))
        before = js.snapshot(self.root, self.spec)
        before["identities"] = []
        js.audit(self.root, self.spec, before, self.policy, self.now)
        workspace = js.Workspace(self.root, self.spec)
        self.assertEqual(
            workspace.render(("1.5.0",))["packages/provider/package.json"], original
        )
        self.assertEqual(path.read_bytes(), original)

    def retained_fixture(self, *, days=60, dependencies=None, integrity=True):
        import javascript_sources as sources

        archive_bytes = io.BytesIO()
        manifest = json.dumps(
            {
                "name": "native-leaf",
                "version": "1.0.0",
                "dependencies": dependencies or {},
            }
        ).encode()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            member = tarfile.TarInfo("source/package.json")
            member.size = len(manifest)
            archive.addfile(member, io.BytesIO(manifest))
        body = archive_bytes.getvalue()
        self.spec["retained_sources"] = [
            {
                "manifest": "package.json",
                "package": "native-leaf",
                "repository": "neutral/native-leaf",
                "commit": "a" * 40,
                "sha256": hashlib.sha256(body).hexdigest(),
                "reason": "Retained native compatibility source.",
            }
        ]
        item = sources.declarations(self.spec)[0]
        self.manifest("package.json", {"native-leaf": item["specifier"]})
        key = "native-leaf@" + item["url"]
        resolution = {"gitHosted": True, "tarball": item["url"]}
        if integrity:
            resolution["integrity"] = item["integrity"]
        lock = {
            "lockfileVersion": "9.0",
            "importers": {
                ".": {
                    "dependencies": {
                        "native-leaf": {
                            "specifier": item["specifier"],
                            "version": item["url"],
                        }
                    }
                }
            },
            "packages": {key: {"version": "1.0.0", "resolution": resolution}},
            "snapshots": {key: {}},
        }
        self.write("pnpm-lock.yaml", json.dumps(lock))
        return (
            item,
            body,
            lock,
            {
                "sha": item["commit"],
                "commit": {
                    "committer": {"date": (self.now - timedelta(days=days)).isoformat()}
                },
            },
        )

    def test_retained_source_is_explicit_and_generic_audit_checks_its_bytes(self):
        import updates

        item, body, lock, commit = self.retained_fixture()
        before = js.snapshot(self.root, self.spec)
        self.assertEqual(before["identities"][0][0], "github-source")
        with (
            patch.object(registry, "data", return_value=commit),
            patch.object(registry, "fetch", return_value=(body, {})),
        ):
            js.audit(self.root, self.spec, before, self.policy, self.now)
            updates.audit_identities(
                self.root,
                {tuple(i) for i in before["identities"]},
                set(),
                self.policy,
                self.now,
            )
        with (
            patch.object(registry, "data", return_value=commit),
            patch.object(registry, "fetch", return_value=(body + b"tampered", {})),
        ):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                updates.audit_identities(
                    self.root,
                    {tuple(i) for i in before["identities"]},
                    set(),
                    self.policy,
                    self.now,
                )
        self.spec["retained_sources"][0]["commit"] = "main"
        with self.assertRaisesRegex(ValueError, "immutable commit"):
            js.Workspace(self.root, self.spec)

    def test_retained_source_new_age_and_dependency_graph_cannot_be_bypassed(self):
        item, body, lock, commit = self.retained_fixture(days=1)
        before = js.snapshot(self.root, self.spec)
        with (
            patch.object(registry, "data", return_value=commit),
            patch.object(registry, "fetch", return_value=(body, {})),
        ):
            js.audit(self.root, self.spec, before, self.policy, self.now)
            with self.assertRaisesRegex(ValueError, "commit-age"):
                js.audit(
                    self.root,
                    self.spec,
                    {**before, "identities": []},
                    self.policy,
                    self.now,
                )
        item, body, lock, commit = self.retained_fixture(
            dependencies={"untracked": "1.0.0"}
        )
        before = js.snapshot(self.root, self.spec)
        with (
            patch.object(registry, "data", return_value=commit),
            patch.object(registry, "fetch", return_value=(body, {})),
        ):
            with self.assertRaisesRegex(ValueError, "dependency graphs"):
                js.audit(self.root, self.spec, before, self.policy, self.now)
        lock["importers"]["."]["dependencies"]["native-leaf"]["version"] = (
            "https://example.invalid/archive.tgz"
        )
        self.write("pnpm-lock.yaml", json.dumps(lock))
        with self.assertRaisesRegex(ValueError, "importer"):
            js.snapshot(self.root, self.spec)

    def test_retained_source_resolver_binds_archive_integrity_before_freeze(self):
        item, body, lock, commit = self.retained_fixture(integrity=False)
        next(iter(lock["packages"].values()))["resolution"]["integrity"] = (
            "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()
        )

        def execute(root, profile, argv, *, cwd, **kwargs):
            if "--frozen-lockfile" not in argv:
                (cwd / "pnpm-lock.yaml").write_text(json.dumps(lock))
            else:
                current = js.document(
                    Path("pnpm-lock.yaml"), (cwd / "pnpm-lock.yaml").read_text()
                )[0]
                self.assertEqual(
                    next(iter(current["packages"].values()))["resolution"]["integrity"],
                    item["integrity"],
                )
            return subprocess.CompletedProcess(argv, 0, "", "")

        with (
            patch.object(registry, "data", return_value=commit),
            patch.object(registry, "fetch", return_value=(body, {})),
        ):
            result = self.resolve(execute)
        self.assertIn("pnpm-lock.yaml", result["changed_files"])
        self.assertTrue(
            js.snapshot(self.root, self.spec)["identities"][0][-1].startswith("sha256:")
        )

    def transitive_source_fixture(self):
        import javascript_sources as sources

        item, body, lock, commit = self.retained_fixture(
            dependencies={"utility": "1.0.0"}
        )
        self.spec["retained_sources"][0].update(
            parent="renderer@1.0.0",
            parent_specifier="git+https://github.com/neutral/native-leaf.git",
        )
        item = sources.declarations(self.spec)[0]
        self.manifest("package.json", {"renderer": "^1.0.0"})
        self.write(
            "pnpm-workspace.yaml",
            "packages: []\noverrides:\n  'renderer@1.0.0>native-leaf': "
            + item["specifier"]
            + "\n  'utility@<1.1.0': 1.1.0\n",
        )
        self.release(
            "renderer", "1.0.0", children={"native-leaf": item["parent_specifier"]}
        )
        self.release("renderer", "2.0.0")
        self.release("utility", "1.0.0")
        self.release("utility", "1.1.0", children={"nested": "1.0.0"})
        self.release("nested", "1.0.0")
        lock["importers"]["."]["dependencies"] = {
            "renderer": {"specifier": "^1.0.0", "version": "1.0.0"}
        }
        lock["snapshots"]["native-leaf@" + item["url"]] = {
            "dependencies": {"utility": "1.1.0"}
        }
        lock["snapshots"]["renderer@1.0.0"] = {
            "dependencies": {"native-leaf": item["url"]}
        }
        lock["snapshots"]["utility@1.1.0"] = {"dependencies": {"nested": "1.0.0"}}
        lock["snapshots"]["nested@1.0.0"] = {}
        for name, version in (
            ("renderer", "1.0.0"),
            ("utility", "1.1.0"),
            ("nested", "1.0.0"),
        ):
            lock["packages"][name + "@" + version] = {
                "resolution": {
                    "integrity": self.metadata[name]["versions"][version]["dist"][
                        "integrity"
                    ]
                }
            }
        self.write("pnpm-lock.yaml", json.dumps(lock))

        def data(url):
            if "api.github.com" in url:
                return commit
            if url.endswith("renderer/1.0.0"):
                return self.metadata["renderer"]["versions"]["1.0.0"]
            return self.fetch(url)

        return item, body, lock, data

    def test_transitive_source_binds_parent_and_audits_registry_children(self):
        item, body, lock, data = self.transitive_source_fixture()
        before = js.snapshot(self.root, self.spec)
        with (
            patch.object(registry, "data", side_effect=data),
            patch.object(registry, "fetch", return_value=(body, {})),
        ):
            js.audit(self.root, self.spec, before, self.policy, self.now)
            self.assertEqual(self.selected()[1]["renderer"], "1.0.0")
            self.release("nested", "1.0.0", days=1)
            with self.assertRaisesRegex(ValueError, "not mature"):
                js.audit(
                    self.root,
                    self.spec,
                    {**before, "identities": []},
                    self.policy,
                    self.now,
                )

    def test_transitive_source_parent_graph_and_override_changes_fail(self):
        for fault in (
            "parent",
            "source",
            "missing-child",
            "range",
            "overridden-original",
            "metadata",
            "override",
        ):
            with self.subTest(fault=fault):
                item, body, lock, data = self.transitive_source_fixture()
                before = js.snapshot(self.root, self.spec)
                if fault == "parent":
                    lock["importers"]["."]["dependencies"]["renderer"]["version"] = (
                        "2.0.0"
                    )
                elif fault == "source":
                    lock["snapshots"]["renderer@1.0.0"]["dependencies"][
                        "native-leaf"
                    ] = item["url"].replace("a" * 40, "b" * 40)
                elif fault == "missing-child":
                    lock["snapshots"]["native-leaf@" + item["url"]] = {}
                elif fault == "range":
                    lock["snapshots"]["native-leaf@" + item["url"]]["dependencies"][
                        "utility"
                    ] = "4.0.0"
                elif fault == "overridden-original":
                    lock["snapshots"]["native-leaf@" + item["url"]]["dependencies"][
                        "utility"
                    ] = "1.0.0"
                elif fault == "metadata":
                    self.metadata["renderer"]["versions"]["1.0.0"]["dependencies"][
                        "native-leaf"
                    ] = "github:different/source#" + "a" * 40
                else:
                    path = self.root / "pnpm-workspace.yaml"
                    path.write_text(
                        path.read_text().replace(
                            item["specifier"], "github:neutral/native-leaf#main"
                        )
                    )
                self.write("pnpm-lock.yaml", json.dumps(lock))
                with (
                    patch.object(registry, "data", side_effect=data),
                    patch.object(registry, "fetch", return_value=(body, {})),
                    self.assertRaisesRegex(
                        ValueError, "parent|source|range|override|graph"
                    ),
                ):
                    js.audit(self.root, self.spec, before, self.policy, self.now)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_GITHUB_SOURCE") == "1",
        "explicit pinned JavaScript and public source integration lane",
    )
    def test_real_pnpm_binds_a_public_source_before_frozen_verification(self):
        source = {
            "manifest": "package.json",
            "package": "is-number",
            "repository": "jonschlinkert/is-number",
            "commit": "98e8ff1da1a89f93d1397a24d7413ed15421c139",
            "sha256": "4e169d1ea361d1b92907a6ab2dd6bcd781c85027858d99df1596a712aab49506",
            "reason": "Public dependency-free source fixture for immutable archive verification.",
        }
        self.spec["retained_sources"] = [source]
        self.manifest(
            "package.json",
            {"is-number": f"github:{source['repository']}#{source['commit']}"},
        )
        with patch.object(registry, "data", side_effect=self.real_data):
            result = js.resolve(self.root, self.spec, self.policy, self.now)
        self.assertIn("pnpm-lock.yaml", result["changed_files"])
        self.assertEqual(
            js.snapshot(self.root, self.spec)["identities"][0][-1],
            "sha256:" + source["sha256"],
        )

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_PNPM") == "1",
        "explicit pinned JavaScript profile integration lane",
    )
    def test_real_npm_resolves_and_checks_a_neutral_package_lock(self):
        self.spec["manager"] = "npm"
        self.manifest(
            "package.json",
            {"local-provider": "file:packages/provider"},
            workspaces=["packages/*"],
        )
        self.manifest(
            "packages/provider/package.json", {}, name="local-provider", version="1.0.0"
        )
        result = js.resolve(self.root, self.spec, self.policy, self.now)
        self.assertIn("package-lock.json", result["changed_files"])

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_PNPM") == "1",
        "explicit pinned JavaScript profile integration lane",
    )
    def test_real_pnpm_creates_a_neutral_lock_in_the_declared_profile(self):
        # No registry dependency or package installation is needed to exercise
        # the actual pnpm process and disposable-directory publication boundary.
        result = js.resolve(self.root, self.spec, self.policy, self.now)
        self.assertIn("pnpm-lock.yaml", result["changed_files"])
        self.assertEqual(result["resolution_attempts"], 1)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_PNPM") == "1",
        "explicit pinned JavaScript profile integration lane",
    )
    def test_real_pnpm_resolves_declared_local_directory_sources(self):
        self.local_directory_fixture()
        (self.root / "pnpm-lock.yaml").unlink()
        result = js.resolve(self.root, self.spec, self.policy, self.now)
        self.assertIn("pnpm-lock.yaml", result["changed_files"])
        self.assertEqual(js.snapshot(self.root, self.spec)["identities"], [])


if __name__ == "__main__":
    unittest.main()
