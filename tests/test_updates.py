"""Exercise exact candidate commits and equivalent disposable previews in real Git."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import updates


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="toolchain git ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "0",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Example Test")
        self.git("config", "user.email", "example@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("deps.txt", "before\n")
        self.write("untouched.txt", "keep\n")
        self.write(".gitignore", ".cache/\nbuild/\n")
        self.git("add", ".")
        self.git("commit", "-m", "initial")
        self.initial = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return updates.git(self.root, *args)

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def update(self):
        self.write("deps.txt", "original\n")

    def verify(self):
        self.assertEqual((self.root / "deps.txt").read_text(), "original\n")

    def test_verified_commit_and_noop(self):
        result = updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("show", "HEAD:deps.txt"), "original")
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), "")
        with patch.object(
            updates, "commit_verified", side_effect=AssertionError("empty commit")
        ):
            result = updates.transaction(
                self.root, ["deps.txt"], self.update, self.verify
            )
        self.assertIsNone(result["commit"])

    def test_no_commit_preserves_candidate(self):
        result = updates.transaction(
            self.root, ["deps.txt"], self.update, self.verify, False
        )
        self.assertIsNone(result["commit"])
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.verify()

    def hidden_index_flag(self, flag):
        self.git("update-index", flag, "untouched.txt")
        self.write("untouched.txt", "hidden source mutation\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        before_flags = self.git("ls-files", "-v", "-z")
        with self.assertRaisesRegex(ValueError, "index|assume-unchanged|skip-worktree"):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("ls-files", "-v", "-z"), before_flags)
        self.assertEqual(
            (self.root / "untouched.txt").read_text(), "hidden source mutation\n"
        )
        self.assertEqual((self.root / "deps.txt").read_text(), "before\n")

    def test_assume_unchanged_cannot_hide_verified_source(self):
        self.hidden_index_flag("--assume-unchanged")

    def test_skip_worktree_cannot_hide_verified_source(self):
        self.hidden_index_flag("--skip-worktree")

    def test_clean_hidden_index_flag_is_rejected_without_clearing_it(self):
        self.git("update-index", "--assume-unchanged", "untouched.txt")
        before_flags = self.git("ls-files", "-v", "-z")
        with self.assertRaisesRegex(ValueError, "index|assume-unchanged"):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("ls-files", "-v", "-z"), before_flags)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_core_filemode_cannot_hide_baseline_mode_change(self):
        self.git("config", "core.filemode", "false")
        (self.root / "untouched.txt").chmod(0o755)
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "raw|mode|HEAD"):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual((self.root / "untouched.txt").stat().st_mode & 0o777, 0o755)

    def test_clean_filter_cannot_hide_different_baseline_source_bytes(self):
        self.write(".gitattributes", "untouched.txt filter=normalize\n")
        self.git("config", "filter.normalize.clean", "sed s/hidden/keep/g")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "declare normalizing source filter")
        head = self.git("rev-parse", "HEAD")
        self.write("untouched.txt", "hidden\n")
        self.git("add", "untouched.txt")
        self.assertEqual(self.git("status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "raw|filter|HEAD"):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / "untouched.txt").read_text(), "hidden\n")

    def test_identity_filter_preserves_exact_full_committed_tree(self):
        self.write(".gitattributes", "*.txt filter=identity\n")
        self.git("config", "filter.identity.clean", "cat")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "declare identity filter")
        result = updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("show", "HEAD:untouched.txt"), "keep")
        self.assertEqual(self.git("show", "HEAD:deps.txt"), "original")

    def test_verifier_cannot_install_hidden_index_flag(self):
        def verify():
            self.verify()
            self.git("update-index", "--skip-worktree", "untouched.txt")

        with self.assertRaisesRegex(ValueError, "index|skip-worktree"):
            updates.transaction(self.root, ["deps.txt"], self.update, verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertIn("S untouched.txt", self.git("ls-files", "-v"))

    def test_commit_preparation_cannot_install_hidden_index_flag(self):
        actual_git = updates.git

        def prepare(root, *args, **kwargs):
            result = actual_git(root, *args, **kwargs)
            if args[:1] == ("commit-tree",):
                actual_git(root, "update-index", "--assume-unchanged", "untouched.txt")
            return result

        with (
            patch.object(updates, "git", side_effect=prepare),
            self.assertRaisesRegex(ValueError, "index|assume-unchanged"),
        ):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertIn("h untouched.txt", self.git("ls-files", "-v"))

    def test_full_candidate_catches_tracked_change_after_baseline_read(self):
        self.write(".gitattributes", "untouched.txt filter=normalize\n")
        self.git("config", "filter.normalize.clean", "sed s/hidden/keep/g")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "declare normalizing source filter")
        head = self.git("rev-parse", "HEAD")
        actual_snapshot, injected = updates.snapshot, False

        def snapshot(root):
            nonlocal injected
            if not injected:
                injected = True
                self.write("untouched.txt", "hidden\n")
                self.git("add", "untouched.txt")
            return actual_snapshot(root)

        def verify():
            self.verify()
            self.assertEqual((self.root / "untouched.txt").read_text(), "hidden\n")

        with (
            patch.object(updates, "snapshot", side_effect=snapshot),
            self.assertRaisesRegex(ValueError, "staged|tree|filter"),
        ):
            updates.transaction(self.root, ["deps.txt"], self.update, verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / "untouched.txt").read_text(), "hidden\n")

    def test_clean_filter_cannot_commit_unverified_bytes(self):
        self.write(".gitattributes", "deps.txt filter=change\n")
        self.git("config", "filter.change.clean", "sed s/original/altered/g")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "declare filter")
        head = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "staged|tree|filter"):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.verify()

    def test_unrelated_index_insertion_during_staging_is_rejected(self):
        original_git = updates.git

        def concurrent(root, *args, **kwargs):
            result = original_git(root, *args, **kwargs)
            if args[:1] == ("add",):
                blob = subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=root,
                    input="unverified\n",
                    text=True,
                    check=True,
                    capture_output=True,
                ).stdout.strip()
                original_git(
                    root, "update-index", "--cacheinfo", f"100644,{blob},untouched.txt"
                )
            return result

        with patch.object(updates, "git", side_effect=concurrent):
            with self.assertRaisesRegex(ValueError, "staged|tree|index"):
                updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_invalid_signing_policy_is_not_unsigned_success(self):
        self.git("config", "commit.gpgsign", "not-a-boolean")
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_index_change_at_write_tree_cannot_advance_head(self):
        original_git = updates.git
        injected = False

        def concurrent(root, *args, **kwargs):
            nonlocal injected
            if args[:1] == ("write-tree",) and not injected:
                injected = True
                blob = subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=root,
                    input="unverified tree\n",
                    text=True,
                    check=True,
                    capture_output=True,
                ).stdout.strip()
                original_git(
                    root, "update-index", "--cacheinfo", f"100644,{blob},untouched.txt"
                )
            return original_git(root, *args, **kwargs)

        with patch.object(updates, "git", side_effect=concurrent):
            with self.assertRaises(ValueError):
                updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_no_commit_rejects_verifier_index_change(self):
        def mutate_index():
            self.verify()
            self.git("add", "--", "deps.txt")

        with self.assertRaisesRegex(ValueError, "Git|HEAD|index"):
            updates.transaction(
                self.root, ["deps.txt"], self.update, mutate_index, False
            )
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "deps.txt")

    def test_required_signing_failure_does_not_advance_branch(self):
        self.git("config", "commit.gpgsign", "true")
        self.git("config", "gpg.program", "unavailable-example-signer")
        with self.assertRaises(subprocess.CalledProcessError):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_dirty_parent_nested_and_failed_verification_refuse_commit(self):
        nested = self.root / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "enclosing"):
            updates.repository(nested)
        self.update()
        with self.assertRaisesRegex(ValueError, "clean"):
            updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.write("deps.txt", "before\n")

        def failure():
            raise ValueError("verification failure")

        with self.assertRaisesRegex(ValueError, "verification failure"):
            updates.transaction(self.root, ["deps.txt"], self.update, failure)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.verify()

    def test_leading_space_filename_preserves_identity(self):
        self.write(" leading.txt", "keep\n")
        self.git("add", " leading.txt")
        self.git("commit", "-m", "space path")
        self.assertIn(" leading.txt", updates.snapshot(self.root))

    def preview(self, update, verify):
        with (
            patch.object(
                updates,
                "settings",
                return_value={"outputs": ["deps.txt", "build/tracked.txt"]},
            ),
            patch.object(updates, "perform", side_effect=update),
            patch.object(updates, "verify", side_effect=verify),
        ):
            return updates.preview(self.root, datetime.now(timezone.utc), [])

    def preview_routing_case(self, overrides):
        before = updates.snapshot(self.root)
        index = (self.root / ".git/index").read_bytes()
        with patch.dict(os.environ, overrides):
            result = self.preview(
                lambda root, *_: (root / "deps.txt").write_text("candidate\n"),
                lambda *_: None,
            )
            self.assertEqual(result["changed"], ["deps.txt"])
            self.assertTrue(all(os.environ[k] == v for k, v in overrides.items()))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(updates.snapshot(self.root), before)

    def test_preview_ambient_git_routing_preserves_original_repository(self):
        self.preview_routing_case(
            {
                "GIT_DIR": str(self.root / ".git"),
                "GIT_WORK_TREE": str(self.root),
                "GIT_INDEX_FILE": str(self.root / ".git/index"),
            }
        )

    def test_preview_ambient_git_configuration_preserves_original_repository(self):
        self.preview_routing_case(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": str(self.root),
            }
        )

    def test_preview_failure_restores_git_environment(self):
        before = dict(os.environ)
        with self.assertRaisesRegex(ValueError, "controlled verification failure"):
            self.preview(
                lambda root, *_: (root / "deps.txt").write_text("candidate\n"),
                lambda *_: (_ for _ in ()).throw(
                    ValueError("controlled verification failure")
                ),
            )
        self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual((self.root / "deps.txt").read_text(), "before\n")

    def test_preview_is_disposable_and_reports_tracked_ignored_paths(self):
        self.write("build/tracked.txt", "old\n")
        self.git("add", "-f", "build/tracked.txt")
        self.git("commit", "-m", "tracked build input")
        before = updates.snapshot(self.root)
        result = self.preview(
            lambda root, *_: (root / "build/tracked.txt").write_text("new\n"),
            lambda *_: None,
        )
        self.assertEqual(result["changed"], ["build/tracked.txt"])
        self.assertEqual(updates.snapshot(self.root), before)

    def test_preview_rejects_verification_mutation_and_mode_changes(self):
        for mutate in (
            lambda path: path.write_text("verifier mutation\n"),
            lambda path: path.chmod(0o755),
        ):
            with self.subTest(mutate=mutate):
                with self.assertRaisesRegex(ValueError, "Verification changed"):
                    self.preview(
                        lambda root, *_: (root / "deps.txt").write_text("candidate\n"),
                        lambda root, *_: mutate(root / "deps.txt"),
                    )
                self.assertEqual((self.root / "deps.txt").read_text(), "before\n")


if __name__ == "__main__":
    unittest.main()
