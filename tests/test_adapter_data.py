"""Native input projections and their adapter decision boundaries."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import adapter_data as data
import lock_adapters


class AdapterDataTests(unittest.TestCase):
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
            root = Path(directory)
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
            root = Path(directory)
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
