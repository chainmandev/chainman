"""Canonical recipe behavior, isolated formatting, and source protection."""

import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import recipes
import chainman_updates
import dependency_api
import test_update_staging as staging
import updates
import update_staging


class RecipeTests(unittest.TestCase):
    def test_explicit_setup_validates_all_groups_before_project_tasks(self):
        cfg = {
            "tasks": {"hooks": {"commands": [["true"]]}},
            "recipes": {"setup": ["hooks"]},
        }
        # Raw setup owns extensions too; recipe dispatch must not run them twice.
        self.assertEqual(recipes.actions(cfg)["setup"], [["setup"]])

    def test_public_sdk_recipe_routes_each_platform_without_nested_shell_parsing(self):
        with tempfile.TemporaryDirectory(prefix="chainman SDK recipe ") as temporary:
            root = Path(temporary).resolve()
            shutil.copyfile(
                Path(__file__).resolve().parents[1] / "justfile", root / "justfile"
            )
            scripts = root / "scripts"
            scripts.mkdir()
            entry = scripts / "enter.sh"
            entry.write_text(
                f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            entry.chmod(0o755)
            for platform, profile in (("apple", "swift"), ("android", "flutter")):
                result = subprocess.run(
                    ["just", "sdk-doctor", platform],
                    cwd=root,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    [profile, "python3", "scripts/native_sdks.py", platform],
                )
            invalid = subprocess.run(
                ["just", "sdk-doctor", "unknown"], cwd=root, capture_output=True
            )
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(invalid.stdout, b"")

    def test_runtime_selection_uses_the_effective_public_target_arguments(self):
        cases = [
            ([], "include"),
            (["targets=all"], "include"),
            (["--targets=all"], "include"),
            (["--targets", "all"], "include"),
            (["targets=js", "targets=all"], "include"),
            (["targets=all", "--targets", "js"], "exclude"),
            (["targets=js,rust"], "exclude"),
            (["--policy", "compatible"], "include"),
            (["--skip-chainman"], "exclude"),
            (["--skip-chainman", "targets=all"], "exclude"),
            (["--only-chainman"], "only"),
            (["--format"], "exclude"),
            (["--message", "targets=js"], "include"),
            (["--", "2.0"], "include"),
            (["--skip-chainman", "--", "2.0"], "exclude"),
            (["--", "--custom", "value", "--targets", "js"], "exclude"),
        ]
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    chainman_updates.options(arguments).runtime.value, expected
                )

    def test_legacy_resolver_arguments_survive_runtime_selection_unchanged(self):
        extra = ["--custom", "some value", "--targets-extra", "all", "2.0"]
        for flags in ([], ["--skip-chainman"], ["--include-chainman"]):
            with self.subTest(flags=flags):
                opts = chainman_updates.options([*flags, "--", *extra])
                self.assertEqual(opts.extra, extra)
                self.assertEqual(opts.skip_chainman, flags == ["--skip-chainman"])
        # Tolerating legacy passthrough must not relax the actual adapter parser.
        with self.assertRaises(SystemExit):
            dependency_api.selection({"adapters": {"js": {}}}, extra)

    def test_builtin_module_update_accepts_explicit_all_without_new_selection(self):
        root = Path("/module-project")
        now = datetime.now(timezone.utc)
        with (
            patch.object(dependency_api, "policy", return_value={}),
            patch.object(chainman_updates.tc, "environment", return_value={}),
            patch.object(
                chainman_updates.tc, "config", return_value={"modules": ["go"]}
            ),
            patch.object(updates, "perform") as perform,
        ):
            chainman_updates.resolve_current(root, {}, now, ["--targets", "all"])
            perform.assert_called_once_with(root, now, ["go"])
            for extra in (["--targets", "go"], ["--policy", "compatible"]):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    chainman_updates.resolve_current(root, {}, now, extra)
            self.assertEqual(perform.call_count, 1)

    def test_values_are_opaque_and_last_public_choice_wins(self):
        self.assertEqual(
            recipes.options(
                [
                    "--message",
                    "a=b",
                    "commit=off",
                    "commit=auto",
                    "mode=dry-run",
                    "mode=apply",
                ]
            ),
            ["--message", "a=b"],
        )
        self.assertEqual(
            recipes.options(["message=--no-commit", "commit=auto"]),
            ["--message", "--no-commit"],
        )
        self.assertEqual(
            recipes.selection_options(["--targets", "go", "go_policy=compatible"]),
            ["--targets", "go", "--target-policy", "go=compatible"],
        )
        with self.assertRaises(ValueError):
            recipes.options(["--message"])

    def test_public_options_preserve_values_and_preview_does_not_commit(self):
        self.assertEqual(
            recipes.options(
                ["mode=dry-run", "commit=off", "targets=js,rust", "message=two words"]
            ),
            [
                "--preview",
                "--no-commit",
                "--message",
                "two words",
                "--",
                "--targets",
                "js,rust",
            ],
        )

    def test_runtime_plan_preserves_sequential_verification(self):
        cfg = {
            "tasks": {"pg": {}, "spanner": {}},
            "updates": {"verify_tasks": ["pg", "spanner"]},
            "recipes": {},
        }
        body = recipes.plan(cfg, "verify")
        self.assertIn(
            '"$entry" run pg\nexec "$entry" run spanner "$@"',
            body,
        )
        cfg["recipes"]["verify"] = ["pg"]
        with self.assertRaises(ValueError):
            recipes.plan(cfg, "verify")


class FormatTransactions(unittest.TestCase):
    prepare = staging.StagingTests.prepare
    finish = staging.StagingTests.finish

    def setUp(self):
        staging.StagingTests.setUp(self)
        path = self.root / "chainman.toml"
        path.write_text(
            path.read_text()
            + '\n[recipes]\nformat-write=["write"]\nformat-check=["verify"]\n[tasks.write]\ncommands=[["true"]]\n'
        )
        updates.git(self.root, "add", "chainman.toml")
        updates.git(self.root, "commit", "-m", "Configure format")
        self.before = updates.snapshot(self.root)

    def test_format_can_add_generated_files_but_does_not_run_commit_hooks(self):
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 99\n")
        hook.chmod(0o755)
        self.prepare("--format")
        (self.candidate / "generated.txt").write_text("generated\n")
        (self.candidate / "source.txt").unlink()
        update_staging.inspect(self.root, self.stage)
        result = self.finish()
        self.assertEqual(set(result["changed"]), {"generated.txt", "source.txt"})
        self.assertEqual(
            updates.git(self.root, "log", "-1", "--format=%s"), "chore: format"
        )

    def test_format_preserves_concurrent_user_edits(self):
        self.prepare("--format")
        (self.candidate / "source.txt").write_text("formatted\n")
        update_staging.inspect(self.root, self.stage)
        (self.root / "source.txt").write_text("new user work\n")
        with self.assertRaises(ValueError):
            self.finish()
        self.assertEqual((self.root / "source.txt").read_text(), "new user work\n")

    def test_resume_rechecks_original_identity_and_refreezes_candidate(self):
        self.prepare("--format")
        (self.candidate / "source.txt").write_text("first attempt\n")
        update_staging.inspect(self.root, self.stage)
        (self.candidate / "source.txt").write_text("reconciled\n")
        update_staging.resume(self.root, self.stage)
        with self.assertRaises(ValueError):
            self.finish()
        update_staging.inspect(self.root, self.stage)
        self.finish()
        self.assertEqual((self.root / "source.txt").read_text(), "reconciled\n")


if __name__ == "__main__":
    unittest.main()


class StagedDispatchTests(unittest.TestCase):
    def test_update_transaction_cannot_fall_back_to_repository_formatter(self):
        with self.assertRaisesRegex(ValueError, "format-staged"):
            chainman_updates.options(["--format", "--staged"])
