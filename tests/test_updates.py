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

    def submodule(self, initialized=True):
        path = self.root / "vendor source"
        path.mkdir()
        updates.git(path, "init", "-b", "main")
        updates.git(path, "config", "user.name", "Submodule Test")
        updates.git(path, "config", "user.email", "submodule@example.invalid")
        (path / "old.txt").write_text("old history excluded")
        updates.git(path, "add", ".")
        updates.git(path, "commit", "-m", "old input")
        old = updates.git(path, "rev-parse", "HEAD")
        (path / "old.txt").unlink()
        (path / "input.txt").write_text("current input")
        updates.git(path, "add", "--all")
        updates.git(path, "commit", "-m", "current input")
        identity = updates.git(path, "rev-parse", "HEAD")
        updates.git(path, "remote", "add", "origin", "https://example.invalid/private")
        (path / ".git/hooks/pre-commit").write_text("exit 99\n")
        self.write(
            ".gitmodules",
            '[submodule "vendor"]\npath = vendor source\nurl = https://example.invalid/private\n',
        )
        self.git("add", ".gitmodules")
        self.git(
            "update-index",
            "--add",
            "--cacheinfo",
            "160000," + identity + ",vendor source",
        )
        self.git("commit", "-m", "frozen source input")
        if not initialized:
            import shutil

            shutil.rmtree(path)
            path.mkdir()
        return path, identity, old

    def unchanged_submodule(self, initialized):
        _, identity, _ = self.submodule(initialized)
        result = updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertIsNotNone(result["commit"])
        self.assertEqual(
            updates.tree_entries(self.root, "HEAD")["vendor source"],
            ("160000", identity),
        )
        result = updates.transaction(self.root, ["deps.txt"], self.update, self.verify)
        self.assertIsNone(result["commit"])

    def test_initialized_submodule_commits_and_noop_with_no_fetch(self):
        self.unchanged_submodule(True)

    def test_uninitialized_submodule_commits_and_noop_with_no_fetch(self):
        self.unchanged_submodule(False)

    def test_uninitialized_submodule_preview_preserves_empty_input(self):
        _, identity, _ = self.submodule(False)

        def inspect(root, *_):
            self.assertEqual(list((root / "vendor source").iterdir()), [])
            self.assertEqual(
                updates.tree_entries(root, "HEAD")["vendor source"],
                ("160000", identity),
            )

        result = self.preview(
            lambda root, *_: (root / "deps.txt").write_text("candidate\n"), inspect
        )
        self.assertEqual(result["changed"], ["deps.txt"])

    def test_submodule_preview_copies_only_current_objects_and_preserves_pin(self):
        source, identity, old = self.submodule()
        before = updates.snapshot(self.root)

        def inspect(root, *_):
            child = root / "vendor source"
            self.assertEqual(updates.git(child, "rev-parse", "HEAD"), identity)
            self.assertEqual((child / "input.txt").read_text(), "current input")
            self.assertEqual(updates.git(child, "remote"), "")
            self.assertFalse((child / ".git/hooks/pre-commit").exists())
            probe = subprocess.run(
                ["git", "cat-file", "-e", old],
                cwd=child,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(probe.returncode, 0)
            self.assertEqual(updates.git(child, "rev-list", "--count", "HEAD"), "1")

        result = self.preview(
            lambda root, *_: (root / "deps.txt").write_text("candidate\n"), inspect
        )
        self.assertEqual(result["changed"], ["deps.txt"])
        self.assertEqual(updates.snapshot(self.root), before)
        self.assertTrue((source / ".git/hooks/pre-commit").exists())

    def test_submodule_dirty_bytes_and_hidden_flags_fail_without_repair(self):
        path, _, _ = self.submodule()
        self.git("config", "submodule.vendor.ignore", "all")
        updates.git(path, "config", "core.filemode", "false")
        (path / "input.txt").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "Submodule input changed"):
            updates.transaction(self.root, ["*"], self.update, self.verify)
        self.assertEqual((self.root / "deps.txt").read_text(), "before\n")
        (path / "input.txt").chmod(0o644)
        updates.git(path, "update-index", "--assume-unchanged", "input.txt")
        with self.assertRaisesRegex(ValueError, "hidden index"):
            updates.transaction(self.root, ["*"], self.update, self.verify)

    def test_gitfile_submodule_preview_is_independent(self):
        path, identity, _ = self.submodule()
        self.git("submodule", "absorbgitdirs")
        self.assertTrue((path / ".git").is_file())
        original = (path / ".git").read_bytes()

        def inspect(root, *_):
            copied = root / "vendor source"
            self.assertTrue((copied / ".git").is_dir())
            self.assertEqual(updates.git(copied, "rev-parse", "HEAD"), identity)

        self.preview(
            lambda root, *_: (root / "deps.txt").write_text("candidate\n"), inspect
        )
        self.assertEqual((path / ".git").read_bytes(), original)

    def test_submodule_metadata_is_read_only_with_commit_disabled(self):
        self.submodule(False)
        with self.assertRaisesRegex(ValueError, "separate transaction"):
            updates.transaction(
                self.root,
                ["*"],
                lambda: self.write(".gitmodules", "changed\n"),
                lambda: None,
                False,
            )

    def test_submodule_mutation_is_rejected_even_with_wildcard_outputs(self):
        path, _, _ = self.submodule()

        def mutate():
            self.update()
            (path / "input.txt").write_text("unexpected")

        head = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ValueError, "Submodule input changed"):
            updates.transaction(self.root, ["*"], mutate, self.verify)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((path / "input.txt").read_text(), "unexpected")

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

    def test_preview_rejects_links_to_original_or_external_files_before_hooks(self):
        with tempfile.TemporaryDirectory(prefix="preview external ") as external:
            canary = Path(external) / "canary.txt"
            canary.write_text("untouched")
            for target in (
                str(self.root / "deps.txt"),
                str(canary),
                os.path.relpath(canary, self.root),
                "cycle",
            ):
                with self.subTest(target=target):
                    link = self.root / "cycle"
                    link.symlink_to(target)
                    self.git("add", "cycle")
                    self.git("commit", "-m", "source link")
                    with self.assertRaisesRegex(ValueError, "Preview rejects"):
                        self.preview(
                            lambda root, *_: (root / "cycle").write_text("changed"),
                            lambda *_: self.fail("verification must not run"),
                        )
                    self.assertEqual(canary.read_text(), "untouched")
                    self.assertEqual((self.root / "deps.txt").read_text(), "before\n")
                    link.unlink()
                    self.git("add", "cycle")
                    self.git("commit", "-m", "remove source link")

    def test_preview_preserves_contained_relative_and_dangling_links(self):
        (self.root / "alias").symlink_to("deps.txt")
        (self.root / "dangling").symlink_to("build/not-created.txt")
        self.git("add", "alias", "dangling")
        self.git("commit", "-m", "portable source links")

        def inspect(root, *_):
            self.assertEqual(os.readlink(root / "alias"), "deps.txt")
            self.assertEqual(os.readlink(root / "dangling"), "build/not-created.txt")
            self.assertEqual((root / "alias").read_text(), "candidate\n")

        result = self.preview(
            lambda root, *_: (root / "alias").write_text("candidate\n"), inspect
        )
        self.assertEqual(result["changed"], ["deps.txt"])
        self.assertEqual((self.root / "alias").read_text(), "before\n")

    def test_submodule_preview_links_cannot_escape_copied_input(self):
        source, _, _ = self.submodule()
        link = source / "alias"
        for target in ("input.txt", "../deps.txt", str(source / "input.txt")):
            with self.subTest(target=target):
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                updates.git(source, "add", "alias")
                updates.git(
                    source,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-m",
                    "source link",
                )
                self.git("add", "vendor source")
                self.git("commit", "-m", "pin source link")
                if target == "input.txt":
                    self.preview(
                        lambda root, *_: (root / "deps.txt").write_text("candidate\n"),
                        lambda root, *_: self.assertEqual(
                            (root / "vendor source/alias").read_text(), "current input"
                        ),
                    )
                else:
                    with self.assertRaisesRegex(ValueError, "Preview rejects"):
                        self.preview(
                            lambda *_: self.fail("update must not run"),
                            lambda *_: self.fail("verification must not run"),
                        )
                self.assertEqual((source / "input.txt").read_text(), "current input")
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

    def test_preview_refresh_binds_actual_subprocess_to_disposable_project(self):
        # Keep the real perform/environment/subprocess/import path. Only the Nix
        # entry and registry resolver are neutral fixtures; no network is needed.
        self.write(
            "toolchain.toml",
            'schema=1\nmodules=["core"]\n[updates]\noutputs=["deps.txt"]\n[cache]\npreserve_environment=["CHAINMAN_ROOT"]\n',
        )
        self.write("dependencies.toml", "[nix]\nenabled=false\n")
        self.write("preview-fixture-marker", "neutral")
        self.write("scripts/enter.sh", '#!/bin/sh\nset -eu\nshift\nexec "$@"\n')
        (self.root / "scripts/enter.sh").chmod(0o755)
        self.write(
            "scripts/updates.py",
            "import runpy\nfrom pathlib import Path\n"
            + f"root = runpy.run_path({str(updates.RUNTIME / 'scripts/toolchain.py')!r})['ROOT']\n"
            + "assert (root / 'preview-fixture-marker').read_text() == 'neutral'\n"
            + "(root / 'deps.txt').write_text('candidate\\n')\n",
        )
        self.git("add", ".")
        self.git("commit", "-m", "neutral refresh boundary fixture")
        before = updates.snapshot(self.root)
        head = self.git("rev-parse", "HEAD")
        index = (self.root / ".git/index").read_bytes()
        verified = []

        def verify_copy(root, _):
            self.assertNotEqual(root, self.root)
            self.assertEqual((root / "deps.txt").read_text(), "candidate\n")
            verified.append(root)

        with (
            patch.object(updates, "RUNTIME", self.root),
            patch.object(updates, "verify", side_effect=verify_copy),
            patch.dict(
                os.environ,
                {
                    "CHAINMAN_ROOT": str(self.root),
                    "CHAINMAN_PROJECT_ROOT": str(self.root),
                },
            ),
        ):
            result = updates.preview(self.root, datetime.now(timezone.utc), [])
        self.assertEqual(
            result,
            {
                "preview": True,
                "changed": ["deps.txt"],
                "commit": None,
                "verification": "passed",
            },
        )
        self.assertEqual(len(verified), 1)
        self.assertFalse(verified[0].exists())
        self.assertEqual(updates.snapshot(self.root), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)

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
