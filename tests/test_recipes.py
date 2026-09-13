"""Canonical recipe behavior, isolated formatting, and source protection."""

import sys
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
            (["--format", "--staged"], "exclude"),
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

    def test_facade_uses_host_scripts_and_sequential_verification(self):
        cfg = {
            "tasks": {"pg": {}, "spanner": {}},
            "updates": {"verify_tasks": ["pg", "spanner"]},
            "recipes": {},
        }
        body = recipes.render(cfg).decode()
        self.assertIn("#!/bin/sh", body)
        self.assertIn(
            './scripts/chainman.sh run pg\n    exec ./scripts/chainman.sh run spanner "$@"',
            body,
        )
        cfg["recipes"]["verify"] = ["pg"]
        with self.assertRaises(ValueError):
            recipes.render(cfg)


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

    def test_staged_formatting_keeps_partial_and_unrelated_changes(self):
        (self.root / "source.txt").write_text("selected\n")
        updates.git(self.root, "add", "source.txt")
        (self.root / "dependency.lock").write_text("staged version\n")
        updates.git(self.root, "add", "dependency.lock")
        (self.root / "dependency.lock").write_text("unstaged version\n")
        self.prepare("--format", "--staged")
        (self.candidate / "source.txt").write_text("formatted selected\n")
        (self.candidate / "dependency.lock").write_text("formatted other\n")
        update_staging.inspect(self.root, self.stage)
        self.assertEqual(
            (self.candidate / "dependency.lock").read_text(), "unstaged version\n"
        )
        self.finish()
        self.assertEqual(
            updates.git(self.root, "show", ":source.txt"), "formatted selected"
        )
        self.assertEqual(
            updates.git(self.root, "show", ":dependency.lock"), "staged version"
        )
        self.assertEqual(
            (self.root / "dependency.lock").read_text(), "unstaged version\n"
        )

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

    def test_staged_formatting_rejects_clean_filter_changes(self):
        (self.root / ".gitattributes").write_text("source.txt filter=change\n")
        updates.git(
            self.root, "config", "filter.change.clean", "sed s/formatted/altered/g"
        )
        updates.git(self.root, "add", ".gitattributes")
        updates.git(self.root, "commit", "-m", "Declare clean filter")
        (self.root / "source.txt").write_text("selected\n")
        updates.git(self.root, "add", "source.txt")
        self.prepare("--format", "--staged")
        (self.candidate / "source.txt").write_text("formatted\n")
        update_staging.inspect(self.root, self.stage)
        with self.assertRaisesRegex(ValueError, "staged tree differs"):
            self.finish()
        self.assertEqual((self.root / "source.txt").read_text(), "formatted\n")
        self.assertEqual(updates.git(self.root, "show", ":source.txt"), "altered")

    def test_staged_formatting_rejects_ignored_executable_mode(self):
        updates.git(self.root, "config", "core.filemode", "false")
        (self.root / "source.txt").write_text("selected\n")
        updates.git(self.root, "add", "source.txt")
        self.prepare("--format", "--staged")
        (self.candidate / "source.txt").chmod(0o755)
        update_staging.inspect(self.root, self.stage)
        with self.assertRaisesRegex(ValueError, "staged tree differs"):
            self.finish()
        self.assertEqual((self.root / "source.txt").stat().st_mode & 0o777, 0o755)
        self.assertEqual(updates.staged_entries(self.root)["source.txt"][0], "100644")

    def test_staged_resume_keeps_selection(self):
        (self.root / "source.txt").write_text("selected\n")
        updates.git(self.root, "add", "source.txt")
        self.prepare("--format", "--staged")
        update_staging.resume(self.root, self.stage)
        self.assertIn(
            "--staged",
            (self.stage / "control/resume-arguments").read_text().splitlines(),
        )

    def test_staged_acceptance_checks_the_effective_tree(self):
        (self.root / "source.txt").write_text("old pair\n")
        updates.git(self.root, "add", "source.txt")
        (self.root / "dependency.lock").write_text("old pair\n")
        self.prepare("--format", "--staged")
        for name in ("source.txt", "dependency.lock"):
            (self.candidate / name).write_text("new pair\n")
        (self.candidate / "unselected-new.txt").write_text("exclude\n")
        update_staging.inspect(self.root, self.stage)

        # This cross-file gate would have passed on the formatter's full output.
        # It must reject the partial application, before anything is finalized.
        def verify_pair(root):
            return (root / "source.txt").read_bytes() == (
                root / "dependency.lock"
            ).read_bytes()

        self.assertFalse(verify_pair(self.candidate))
        self.assertTrue(verify_pair(self.root))
        self.assertFalse((self.candidate / "unselected-new.txt").exists())


if __name__ == "__main__":
    unittest.main()
