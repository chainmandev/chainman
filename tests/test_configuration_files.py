"""Configuration modules preserve execution, diagnostics and frozen authority."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap_plan
import chainman
import config_inspection
import configuration
import configuration_files
import exception_retirement
import toolchain as tc
import update_staging


class ConfigurationFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman modules ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.root.mkdir()
        self.put("chainman.lock", "a" * 40 + "\n")

    def put(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def test_nested_modules_compile_like_one_file_and_report_physical_origins(self):
        self.put(
            "chainman.toml",
            'schema=3\ninclude=["config/tasks.toml","config/profiles.toml"]\n[project]\ndefault_profile="host"\n',
        )
        self.put(
            "config/tasks.toml",
            'include=["config/templates.toml"]\n[tasks.check]\nextends="base"\n',
        )
        self.put(
            "config/templates.toml", '[templates.tasks.base]\ncommands=[["true"]]\n'
        )
        self.put("config/profiles.toml", '[profiles.tools]\nruntime_profile="core"\n')
        source = configuration_files.read(self.root)
        before = {
            "schema": 3,
            "project": {"default_profile": "host"},
            "profiles": {"tools": {"runtime_profile": "core"}},
            "templates": {"tasks": {"base": {"commands": [["true"]]}}},
            "tasks": {"check": {"extends": "base"}},
        }
        self.assertEqual(
            configuration.compile(source.data), configuration.compile(before)
        )
        doc = config_inspection.document(self.root, "explain", ["check"])
        validation = config_inspection.document(self.root, "config", ["validate"])
        self.assertTrue(validation["valid"])
        self.assertNotIn("field_sources", validation)
        self.assertEqual(
            doc["field_sources"]["templates.tasks.base.commands"],
            "config/templates.toml",
        )
        self.assertEqual(
            doc["field_sources"]["tasks.check.extends"], "config/tasks.toml"
        )
        self.assertEqual(
            doc["origins"]["tasks.check"]["commands"], "templates.tasks.base"
        )
        self.assertEqual(
            set(doc["files"]),
            {
                "chainman.toml",
                "config/tasks.toml",
                "config/templates.toml",
                "config/profiles.toml",
            },
        )

    def test_duplicates_never_override_or_concatenate_even_when_equal(self):
        for right in (
            '[tasks.check]\ncommands=[["true"]]',
            '[tasks.check]\ncommands=[["false"]]',
            '[tasks]\ncheck="bad"',
        ):
            self.put("chainman.toml", 'schema=3\ninclude=["a.toml","b.toml"]\n')
            self.put("a.toml", '[tasks.check]\ncommands=[["true"]]\n')
            self.put("b.toml", right)
            with (
                self.subTest(right=right),
                self.assertRaisesRegex(
                    ValueError,
                    r"Duplicate configuration setting tasks.check.*a.toml and b.toml",
                ),
            ):
                tc.config(self.root)

    def test_include_limits_bound_depth_count_and_combined_bytes(self):
        self.put("chainman.toml", 'schema=3\ninclude=["part0.toml"]\n')
        for i in range(16):
            self.put(f"part{i}.toml", f'include=["part{i + 1}.toml"]\n')
        with self.assertRaisesRegex(ValueError, "16 levels"):
            configuration_files.read(self.root)
        self.put(
            "chainman.toml",
            "schema=3\ninclude=["
            + ",".join(f'"p{i}.toml"' for i in range(128))
            + "]\n",
        )
        for i in range(128):
            self.put(f"p{i}.toml", "# empty module\n")
        with self.assertRaisesRegex(ValueError, "128 files"):
            configuration_files.read(self.root)
        self.put("chainman.toml", 'schema=3\ninclude=["p0.toml","p1.toml"]\n')
        for i in range(2):
            self.put(f"p{i}.toml", "#" + " " * (2 * 1024 * 1024))
        with self.assertRaisesRegex(ValueError, "4 MiB in total"):
            configuration_files.read(self.root)

    def test_paths_cycles_repeated_includes_types_and_module_schema_fail(self):
        self.put("a.toml", '[tasks.check]\ncommands=[["true"]]\n')
        for include in (
            '"a.toml"',
            "[1]",
            '["../outside.toml"]',
            '["/absolute.toml"]',
            '["./a.toml"]',
            '["a/*.toml"]',
            '["a.toml","a.toml"]',
            '["chainman.toml"]',
        ):
            self.put("chainman.toml", f"schema=3\ninclude={include}\n")
            with self.subTest(include=include), self.assertRaises(ValueError):
                tc.config(self.root)
        self.put("chainman.toml", 'schema=3\ninclude=["a.toml"]\n')
        self.put("a.toml", 'include=["b.toml"]\n')
        self.put("b.toml", 'include=["a.toml"]\n')
        with self.assertRaisesRegex(ValueError, "cycle"):
            tc.config(self.root)
        self.put("a.toml", "schema=3\n")
        with self.assertRaisesRegex(ValueError, "Only chainman.toml"):
            tc.config(self.root)
        for schema in (1, 2):
            self.put("chainman.toml", f"schema={schema}\ninclude=[]\n")
            with self.assertRaisesRegex(ValueError, "schema=3"):
                tc.config(self.root)

    def test_missing_symlink_directory_malformed_and_oversize_inputs_fail(self):
        self.put("chainman.toml", 'schema=3\ninclude=["module.toml"]\n')
        with self.assertRaises(FileNotFoundError):
            tc.config(self.root)
        target = self.root / "module.toml"
        target.symlink_to(self.root / "chainman.toml")
        with self.assertRaisesRegex(ValueError, "symlink"):
            tc.config(self.root)
        target.unlink()
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            tc.config(self.root)
        target.rmdir()
        self.put("module.toml", "invalid [")
        with self.assertRaisesRegex(ValueError, "module.toml"):
            tc.config(self.root)
        self.put("module.toml", "#" * (4 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            tc.config(self.root)

    def test_module_content_additions_removals_change_profile_identity(self):
        self.put(
            "chainman.toml",
            'schema=3\ninclude=["module.toml"]\n[project]\ndefault_profile="host"\n',
        )
        self.put("module.toml", '[tasks.check]\ncommands=[["true"]]\n')
        first = chainman.profile_inputs(self.root, "host", None)
        self.assertEqual(
            first["module.toml"],
            hashlib.sha256((self.root / "module.toml").read_bytes()).hexdigest(),
        )
        self.put("module.toml", '[tasks.check]\ncommands=[["true"]]\n# edit\n')
        self.assertNotEqual(first, chainman.profile_inputs(self.root, "host", None))
        (self.root / "module.toml").unlink()
        with self.assertRaises(FileNotFoundError):
            chainman.profile_inputs(self.root, "host", None)

    def test_bootstrap_projects_included_services_and_context(self):
        self.put(
            "chainman.toml",
            'schema=3\ninclude=["config/stack.toml"]\n[project]\ndefault_profile="host"\n',
        )
        self.put(
            "config/stack.toml",
            '[tasks.dev]\ncommands=[["true"]]\nservices=["server"]\ncontext_environment={APP_MODE="test"}\n[services.server]\ncommand=["true"]\n',
        )
        controller, options = bootstrap_plan.plan(self.root, "run", "dev")
        self.assertTrue(controller)
        self.assertIn("APP_MODE", options)

    def test_frozen_authority_copies_complete_closure_and_ignores_candidate_edits(self):
        self.put("chainman.toml", 'schema=3\ninclude=["config/tasks.toml"]\n')
        self.put(
            "config/tasks.toml",
            'include=["config/updates.toml"]\n[tasks.check]\ncommands=[["true"]]\n',
        )
        self.put("config/updates.toml", '[updates]\nverify_task="check"\n')
        candidate = self.root.parent / "candidate"
        shutil.copytree(self.root, candidate)
        authority = self.root.parent / "authority"
        update_staging.export_authority(self.root, candidate, authority)
        self.assertEqual(
            (authority / "config/tasks.toml").read_bytes(),
            (self.root / "config/tasks.toml").read_bytes(),
        )
        (candidate / "config/tasks.toml").write_text(
            '[tasks.check]\ncommands=[["evil"]]\n'
        )
        with patch.dict(os.environ, CHAINMAN_ENTRY_AUTHORITY=str(authority)):
            self.assertEqual(
                tc.config(candidate)["tasks"]["check"]["commands"], [["true"]]
            )
            self.assertEqual(tc.config(candidate)["updates"]["verify_task"], "check")

    def test_exception_retirement_finds_and_edits_only_the_owning_module(self):
        self.put("chainman.toml", 'schema=3\ninclude=["config/policy.toml"]\n')
        self.put(
            "config/policy.toml",
            '[updates]\nexceptions=[{package="demo",version="1.0.0",reason="fixture"}]\n',
        )
        self.assertEqual(
            exception_retirement.output_paths(self.root), ["config/policy.toml"]
        )
        documents = exception_retirement.documents(self.root)
        policy = next(doc for doc in documents if doc.path == "config/policy.toml")
        self.assertTrue(
            exception_retirement.permitted(
                policy, b"[updates]\nexceptions=[]\n", {("updates", "exceptions"): {0}}
            )
        )

    def test_real_host_command_runs_from_module_with_project_relative_directory(self):
        self.put(
            "chainman.toml",
            'schema=3\ninclude=["config/tasks.toml"]\n[project]\ndefault_profile="host"\n',
        )
        self.put(
            "config/tasks.toml",
            '[tasks.check]\ndirectory="app"\ncommands=[["sh","-c","printf reached > marker"]]\n',
        )
        (self.root / "app").mkdir()
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(chainman.RUNTIME / "scripts/chainman.py"),
                "--root",
                str(self.root),
                "run",
                "check",
            ],
            env=dict(env, CHAINMAN_MODE="host"),
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "app/marker").read_text(), "reached")


if __name__ == "__main__":
    unittest.main()
