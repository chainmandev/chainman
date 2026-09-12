"""Composition contracts shared by real execution and public inspection."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configuration
import config_inspection
import toolchain as tc
import workflows
import chainman
import bootstrap_plan


class CompositionTests(unittest.TestCase):
    def test_inspection_validates_environment_without_resolving_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = 'schema=3\n[project]\ndefault_profile="host"\n[tasks.check]\ncommands=[["true"]]\n'
            path = root / "chainman.toml"
            path.write_text(source + '[environment.values]\nSECRET="{env:MISSING}"\n')
            doc = config_inspection.document(root, "explain", ["check"])
            self.assertEqual(
                doc["declarations"]["profiles"]["host"], {"execution": "host"}
            )
            for invalid in (
                '[environment.modes.typo.values]\nA="b"',
                '[environment]\nunset="A"',
                '[environment.values]\nCHAINMAN_MODE="host-nix"',
            ):
                path.write_text(source + invalid)
                with self.assertRaises(ValueError):
                    config_inspection.document(root, "config", ["validate"])

    def test_bootstrap_and_explain_include_inherited_services_and_watched_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[setup.compiler]
commands=[["true"]]
inputs=["chainman.toml"]
artifacts=["ready"]
[tasks.build]
commands=[["true"]]
setup=["compiler"]
[services.worker]
command=["true"]
[services.worker.watch]
task="build"
paths=["src"]
[templates.tasks.base]
services=["worker"]
wait_for_services=true
[templates.tasks.base.context_environment]
DEMO_PROVIDER="local"
[tasks.dev]
extends="base"
""")
            controller, options = bootstrap_plan.plan(root, "run", "dev")
            self.assertTrue(controller)
            self.assertEqual(
                options, ["--controller", "1", "--env-pattern", "DEMO_PROVIDER"]
            )
            self.assertFalse(bootstrap_plan.plan(root, "_update-prepare", "")[0])
            plan = config_inspection.document(root, "explain", ["dev"])
            self.assertEqual(plan["order"]["watch_tasks"], ["build"])
            self.assertEqual(plan["order"]["setup"], ["compiler"])
            self.assertEqual(
                plan["origins"]["tasks.dev"]["services"], "templates.tasks.base"
            )

    def test_recursive_tables_and_array_replacement_have_precise_origins(self):
        source = {
            "schema": 3,
            "templates": {
                "tasks": {
                    "base": {
                        "commands": [["true"]],
                        "environment": {"A": "a", "B": "b"},
                        "setup": ["one"],
                    },
                    "derived": {"extends": "base", "environment": {"B": "override"}},
                }
            },
            "tasks": {"build": {"extends": "derived", "setup": []}},
        }
        before = deepcopy(source)
        cfg, origins = configuration.compile(source)
        self.assertEqual(source, before)
        self.assertNotIn("templates", cfg)
        self.assertEqual(
            cfg["tasks"]["build"],
            {
                "commands": [["true"]],
                "environment": {"A": "a", "B": "override"},
                "setup": [],
            },
        )
        self.assertEqual(
            origins["tasks.build"]["environment.B"], "templates.tasks.derived"
        )
        self.assertEqual(origins["tasks.build"]["setup"], "tasks.build")

    def test_unused_invalid_templates_are_rejected(self):
        for spec in (
            {"extends": "missing"},
            {"extends": "bad"},
            {"typo": 1},
            {"cleanup_children": "false"},
            {"readiness": {"typo": 1}},
        ):
            kind = "services" if "readiness" in spec else "tasks"
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                configuration.compile({"schema": 3, "templates": {kind: {"bad": spec}}})

    def test_legacy_is_unchanged_and_cannot_silently_ignore_composition(self):
        for schema in (1, 2):
            source = {"schema": schema, "tasks": {"build": {"commands": [["true"]]}}}
            self.assertEqual(configuration.compile(source), (source, {}))
            with self.assertRaisesRegex(ValueError, "schema=3"):
                configuration.compile(dict(source, templates={}))
            source["tasks"]["build"]["extends"] = "base"
            with self.assertRaisesRegex(ValueError, "schema=3"):
                configuration.compile(source)

    def test_kind_boundaries_and_cycle_chain(self):
        for templates in (
            {"tasks": {"a": {"extends": "b"}, "b": {"extends": "a"}}},
            {"unknown": {}},
            {
                "tasks": {"a": {"extends": "service"}},
                "services": {"service": {"command": ["true"]}},
            },
        ):
            with self.assertRaises(ValueError):
                configuration.compile({"schema": 3, "templates": templates})

    def test_execution_and_inspection_use_the_same_config_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[templates.tasks.base]
commands=[["sh", "-c", "echo executed > result"]]
[templates.tasks.base.environment]
SECRET="never-print-this"
[tasks.build]
extends="base"
""")
            before = list(root.iterdir())
            with patch.object(
                subprocess,
                "run",
                side_effect=AssertionError("inspection ran a command"),
            ):
                self.assertTrue(
                    config_inspection.document(root, "config", ["validate"])["valid"]
                )
                shown = config_inspection.document(root, "config", ["show", "--json"])
                explained = config_inspection.document(
                    root, "explain", ["build", "--json"]
                )
            self.assertEqual(before, list(root.iterdir()))
            self.assertNotIn("never-print-this", json.dumps(shown))
            self.assertNotIn("never-print-this", json.dumps(explained))
            self.assertEqual(
                explained["declarations"]["tasks"]["build"]["commands"],
                tc.config(root)["tasks"]["build"]["commands"],
            )
            workflows.run(root, "build", [])
            self.assertEqual((root / "result").read_text(), "executed\n")

    def test_recovery_projection_does_not_compile_broken_declarations(self):
        import bootstrap_plan

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chainman.toml").write_text(
                'schema=3\n[templates.tasks.broken]\nextends="missing"\n'
            )
            for action in ("services-status", "services-stop"):
                self.assertEqual(
                    bootstrap_plan.plan(root, action, ""), (True, ["--controller", "1"])
                )
            self.assertEqual(
                bootstrap_plan.plan(root, "_control-export", ""), (False, [])
            )
            with self.assertRaises(ValueError):
                bootstrap_plan.plan(root, "run", "test")

    def test_schema_three_keeps_full_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for declaration in (
                '[tasks.bad]\nextends="base"\nsetup=["missing"]',
                '[services.bad]\nextends="base"',
            ):
                kind = "services" if declaration.startswith("[services") else "tasks"
                (root / "chainman.toml").write_text(
                    f'schema=3\n[project]\ndefault_profile="host"\n[templates.{kind}.base]\n{declaration}\n'
                )
                with self.assertRaises(ValueError):
                    config_inspection.validated(root)

    def test_all_kinds_expand_and_template_changes_invalidate_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = """schema=3
[project]
default_profile="host"
[templates.profiles.base]
runtime_profile="core"
compiler_cache=false
[profiles.tools]
extends="base"
[templates.setup.base]
commands=[["true"]]
inputs=["chainman.toml"]
artifacts=["ready"]
[setup.deps]
extends="base"
[templates.services.base]
command=["true"]
shutdown_seconds=10
[services.worker]
extends="base"
[templates.tasks.base]
services=["worker"]
setup=["deps"]
commands=[["true"]]
[tasks.test]
extends="base"
"""
            path = root / "chainman.toml"
            path.write_text(body)
            cfg = config_inspection.validated(root)
            self.assertEqual(cfg["profiles"]["tools"]["runtime_profile"], "core")
            before = workflows.fingerprint(root, workflows.group_spec(cfg, "deps"), {})
            profile_before = chainman.profile_fingerprint(root, "host", None)
            path.write_text(body.replace('commands=[["true"]]', 'commands=[["false"]]'))
            cfg = config_inspection.validated(root)
            self.assertNotEqual(
                before,
                workflows.fingerprint(root, workflows.group_spec(cfg, "deps"), {}),
            )
            self.assertNotEqual(
                profile_before, chainman.profile_fingerprint(root, "host", None)
            )
            plan = config_inspection.document(root, "explain", ["test"])
            self.assertEqual(
                plan["order"],
                {"tasks": ["test"], "services": ["worker"], "setup": ["deps"]},
            )


if __name__ == "__main__":
    unittest.main()
