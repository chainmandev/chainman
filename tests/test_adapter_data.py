"""Native input projections and their adapter decision boundaries."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import given, settings, strategies as st
from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import adapter_data as data
import lock_adapters

# Independent wire vocabulary: production omissions must remain testable.
PNPM_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)


class AdapterDataTests(unittest.TestCase):
    @settings(max_examples=80, derandomize=True, deadline=None)
    @given(
        st.dictionaries(
            st.sampled_from(PNPM_SECTIONS),
            st.dictionaries(
                st.sampled_from(("renamed", "@scope/library", "local")),
                st.tuples(
                    st.sampled_from(("^1", "npm:actual@~2", "workspace:*", "file:lib")),
                    st.sampled_from(
                        ("1.2.3(peer@4.0.0)", "actual@2.1.0", "link:lib", "file:lib")
                    ),
                ),
            ),
        )
    )
    def test_pnpm_projection_preserves_each_edge_coordinate_and_owns_graph(
        self, sections
    ):
        wire = {
            "lockfileVersion": "9.0",
            "importers": {
                "packages/app": {
                    section: {
                        alias: {"specifier": specifier, "version": version}
                        for alias, (specifier, version) in edges.items()
                    }
                    for section, edges in sections.items()
                }
            },
            "snapshots": {
                "parent@1.0.0(peer@4.0.0)": {
                    section: {alias: version for alias, (_, version) in edges.items()}
                    for section, edges in sections.items()
                }
            },
            "packages": {
                "parent@1.0.0": {
                    "resolution": {
                        "integrity": "sha512-fixture",
                        "future-source": True,
                    },
                    "future-metadata": {"keep": [1, 2]},
                }
            },
        }
        original = deepcopy(wire)
        parsed = data.pnpm_lock(wire)
        importer = parsed["importers"]["packages/app"]
        snapshot = parsed["snapshots"]["parent@1.0.0(peer@4.0.0)"]
        self.assertEqual(set(importer), set(sections))
        self.assertEqual(set(snapshot), set(sections))
        for section, edges in sections.items():
            self.assertEqual(set(importer[section]), set(edges))
            self.assertEqual(set(snapshot[section]), set(edges))
            for alias, (specifier, version) in edges.items():
                self.assertEqual(importer[section][alias]["specifier"], specifier)
                self.assertEqual(importer[section][alias]["version"], version)
                self.assertEqual(snapshot[section][alias], version)
                importer[section][alias]["version"] = "changed"
                snapshot[section][alias] = "changed"
        resolution = parsed["packages"]["parent@1.0.0"]["resolution"]
        self.assertEqual(
            resolution, {"integrity": "sha512-fixture", "future-source": True}
        )
        resolution["integrity"] = "changed"
        self.assertEqual(wire, original)

    def test_pnpm_rejects_malformed_nodes_instead_of_treating_them_as_empty(self):
        for field in ("importers", "packages", "snapshots"):
            for invalid in (
                None,
                [],
                False,
                0,
                "",
                {1: {}},
                {"entry": None},
                {"entry": []},
            ):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises(ValueError),
                ):
                    data.pnpm_lock({"lockfileVersion": "9.0", field: invalid})
        for kind in ("importers", "snapshots"):
            for section in PNPM_SECTIONS:
                invalid_entries = (
                    None,
                    [],
                    False,
                    {"alias": None},
                    {"alias": 1},
                    {"alias": False},
                )
                if kind == "importers":
                    invalid_entries += (
                        {"alias": "1.0.0"},
                        {"alias": {"version": "1.0.0"}},
                        {"alias": {"specifier": "^1", "version": []}},
                    )
                else:
                    invalid_entries += ({"alias": {"version": "1.0.0"}},)
                for invalid in invalid_entries:
                    with (
                        self.subTest(kind=kind, section=section, invalid=invalid),
                        self.assertRaises(ValueError),
                    ):
                        data.pnpm_lock(
                            {"lockfileVersion": 9, kind: {"entry": {section: invalid}}}
                        )
        for resolution in (
            None,
            [],
            False,
            {"integrity": []},
            {"tarball": False},
            {"directory": 1},
            {"type": None},
            {"gitHosted": "true"},
        ):
            with self.subTest(resolution=resolution), self.assertRaises(ValueError):
                data.pnpm_lock(
                    {
                        "lockfileVersion": 9,
                        "packages": {"a@1": {"resolution": resolution}},
                    }
                )
        for invalid in (None, True, "9garbage", "90", "9.1", 8, [], {}):
            with self.subTest(version=invalid), self.assertRaises(ValueError):
                data.pnpm_lock({"lockfileVersion": invalid})
        for version in (9, 9.0, "9", "9.0"):
            self.assertEqual(
                data.pnpm_lock({"lockfileVersion": version}),
                {"importers": {}, "packages": {}, "snapshots": {}},
            )
        for source in (
            "lockfileVersion: 9",
            "lockfileVersion: 9.0",
            "lockfileVersion: '9.0'",
        ):
            self.assertEqual(
                data.pnpm_lock(YAML().load(source)),
                {"importers": {}, "packages": {}, "snapshots": {}},
            )

    def test_peer_metadata_projection_preserves_boolean_meaning_and_owns_input(self):
        original = {
            "required": {"optional": False},
            "optional": {"optional": True},
            "default": {"unknown": "future field"},
        }
        parsed = data.peer_metadata(original)
        self.assertEqual(
            parsed,
            {
                "required": {"optional": False},
                "optional": {"optional": True},
                "default": {},
            },
        )
        original["required"]["optional"] = True
        self.assertIs(parsed["required"]["optional"], False)

    @settings(max_examples=100, derandomize=True, deadline=None)
    @given(
        st.lists(
            st.tuples(
                st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=20)
            ),
            max_size=8,
        )
    )
    def test_go_projection_preserves_each_coordinate_and_owns_its_input(
        self, references
    ):
        wire = {
            "Module": {"Path": "neutral.local/root"},
            "Require": [
                {"Path": path, "Version": version} for path, version in references
            ],
            "Replace": [
                {
                    "Old": {"Path": path, "Version": version},
                    "New": {"Path": "./replacement/" + str(index)},
                }
                for index, (path, version) in enumerate(references)
            ],
            "Use": None,
        }
        original = deepcopy(wire)
        parsed = data.GoFile.decode(wire)
        self.assertEqual(parsed.module, "neutral.local/root")
        self.assertEqual([(r.path, r.version) for r in parsed.requires], references)
        self.assertEqual(
            [(r.old.path, r.old.version) for r in parsed.replacements], references
        )
        self.assertEqual(
            [(r.new.path, r.new.version) for r in parsed.replacements],
            [("./replacement/" + str(index), "") for index in range(len(references))],
        )
        self.assertEqual(wire, original)
        wire["Require"].clear()
        wire["Replace"].clear()
        self.assertEqual([(r.path, r.version) for r in parsed.requires], references)
        self.assertEqual(len(parsed.replacements), len(references))

    def test_go_accepts_only_native_empty_list_forms(self):
        for field in ("Require", "Replace", "Use"):
            for valid in (None, []):
                parsed = data.GoFile.decode({field: valid}, workspace=True)
                self.assertEqual(
                    (parsed.requires, parsed.replacements, parsed.uses), ((), (), ())
                )
            for invalid in (False, 0, "", {}, "one", [None]):
                with (
                    self.subTest(field=field, invalid=invalid),
                    self.assertRaises(ValueError),
                ):
                    data.GoFile.decode({field: invalid}, workspace=True)

    def test_go_query_distinguishes_no_retractions_from_malformed_evidence(self):
        query = {"Path": "neutral.local/library", "Version": "v1.0.0"}
        for empty in (None, []):
            self.assertEqual(
                data.go_query({**query, "Retracted": empty}, query["Path"], "v1.0.0")[
                    "Retracted"
                ],
                [],
            )
        for changed in (
            {"Retracted": False},
            {"Retracted": {}},
            {"Retracted": [42]},
            {"Versions": None},
            {"Versions": [False]},
            {"Path": "different.local/module"},
            {"Version": "v2.0.0"},
            {"Error": {"Err": "incomplete response"}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                data.go_query({**query, **changed}, query["Path"], "v1.0.0")

    def test_go_source_validation_does_not_treat_invalid_evidence_as_no_dependencies(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="go-native-input-") as directory:
            root = Path(directory).resolve()
            (root / "go.mod").write_text("module neutral.local/root\n")
            for field in ("Require", "Replace"):

                def native(root, profile, argv, **kwargs):
                    if "env" in argv:
                        return {"GOWORK": "off"}
                    return {"Module": {"Path": "neutral.local/root"}, field: False}

                with (
                    self.subTest(field=field),
                    patch.object(lock_adapters, "native", side_effect=native),
                    self.assertRaises(ValueError),
                ):
                    lock_adapters.validate_go_sources(root, {"directory": "."}, set())

    def test_npm_projection_keeps_audited_fields_without_mutating_raw_extensions(self):
        wire = {
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "neutral-root",
                    "version": "1.0.0",
                    "dependencies": {"library": "^1"},
                    "license": "MIT",
                    "future-metadata": {"keep": [1, 2]},
                }
            },
        }
        original = deepcopy(wire)
        package = data.npm_lock(wire)["packages"][""]
        self.assertEqual(
            package,
            {
                "name": "neutral-root",
                "version": "1.0.0",
                "dependencies": {"library": "^1"},
            },
        )
        package["dependencies"]["library"] = "^2"
        self.assertEqual(wire, original)
        self.assertNotIn("integrity", package)

    def test_npm_rejects_malformed_entries_before_graph_traversal(self):
        for entry in (
            None,
            [],
            False,
            {"version": 1},
            {"name": None},
            {"resolved": []},
            {"integrity": False},
            {"dependencies": []},
            {"dependencies": {"a": 1}},
            {"optionalDependencies": None},
            {"peerDependencies": {"a": False}},
            {"link": "true"},
            {"link": True, "resolved": "local", "future": None},
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                data.npm_lock(
                    {"lockfileVersion": 3, "packages": {"node_modules/a": entry}}
                )
        for invalid in (
            None,
            [],
            {"lockfileVersion": True, "packages": {}},
            {"lockfileVersion": 3, "packages": {1: {}}},
        ):
            with self.subTest(lock=invalid), self.assertRaises(ValueError):
                data.npm_lock(invalid)

    def test_swift_graph_decodes_one_level_without_losing_child_obligations(self):
        child = {
            "identity": "child",
            "name": "child",
            "url": "/child",
            "version": "unspecified",
            "path": "/child",
            "dependencies": [],
        }
        root = {**child, "identity": "root", "dependencies": [child]}
        parsed = data.swift_node(root)
        self.assertEqual(parsed["dependencies"], [child])
        root["dependencies"].clear()
        self.assertEqual(parsed["dependencies"], [child])
        for field in ("identity", "name", "url", "version", "path"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                data.swift_node({**child, field: False})

    def test_maven_routing_rejects_nonstring_configuration(self):
        for config in (
            {"maven_repositories": [["central"]]},
            {"maven_plugin_packages": [None]},
            {"local_projects": {1: "library"}},
        ):
            with self.subTest(config=config), self.assertRaises(ValueError):
                if "local_projects" in config:
                    lock_adapters.local_gradle_projects(Path("."), config)
                else:
                    lock_adapters.maven_repository(config, "neutral:library")

    def test_gradle_graph_decodes_edges_without_losing_source_bindings(self):
        with tempfile.TemporaryDirectory(prefix="gradle-native-input-") as directory:
            root = Path(directory).resolve()
            library = root / "library"
            library.mkdir()
            (library / "build.gradle.kts").write_text("")
            spec = {"local_projects": {"neutral:library": "library"}}
            edge = {
                "selected": ":library",
                "coordinate": "neutral:library",
                "configuration": "runtimeClasspath",
            }
            report = {
                "schema": 1,
                "projects": [{"id": ":library", "directory": str(library)}],
                "edges": [edge],
            }
            original = deepcopy(report)
            self.assertEqual(
                lock_adapters.validate_gradle_projects(root, spec, [report]),
                {
                    "projects": {":library": "library"},
                    "edges": [edge],
                },
            )
            self.assertEqual(report, original)
            for changed in (
                {"schema": True},
                {"projects": [False]},
                {"edges": [None]},
                {"edges": [{**edge, "selected": []}]},
                {"edges": [{**edge, "coordinate": []}]},
                {"edges": [{**edge, "selected": ":missing"}]},
                {"edges": [{**edge, "coordinate": "neutral:different"}]},
            ):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    lock_adapters.validate_gradle_projects(
                        root, spec, [{**report, **changed}]
                    )


if __name__ == "__main__":
    unittest.main()
