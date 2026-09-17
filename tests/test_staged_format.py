"""Real Git index/working-tree tests; no application setup or formatting gates."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import staged_format as subject


class StagedFormatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="staged format ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "chainman.lock").write_text("a" * 40 + "\n")
        (self.root / ".gitignore").write_text(".chainman/\n.cache/\n")
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[formatters.text]
paths=["*.txt"]
profile="host"
write=["unused"]
check=["unused"]
""")
        (self.root / "a.txt").write_text("first\nkeep\nkeep\nlast\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")

    def git(self, *args):
        return subject.git(self.root, *args).stdout

    def stage(self, path="a.txt", body="BAD\nkeep\nkeep\nlast\n"):
        (self.root / path).write_text(body)
        self.git("add", "--", path)

    def formatter(self, root, cfg, paths, **kwargs):
        for path in paths:
            file = root / path
            file.write_bytes(file.read_bytes().replace(b"BAD", b"GOOD"))

    def run_format(self, callback=None):
        with (
            patch.object(subject.formatters, "execute", callback or self.formatter),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return subject.run(self.root)

    def test_fully_staged_and_partial_changes(self):
        self.stage()
        (self.root / "a.txt").write_text("BAD\nkeep\nkeep\nunstaged\n")
        self.run_format()
        self.assertEqual(self.git("show", ":a.txt"), b"GOOD\nkeep\nkeep\nlast\n")
        self.assertEqual(
            (self.root / "a.txt").read_bytes(), b"GOOD\nkeep\nkeep\nunstaged\n"
        )

    def test_conflict_changes_neither_index_nor_working_tree(self):
        self.stage()
        (self.root / "a.txt").write_text("DIFFERENT\nkeep\nkeep\nlast\n")
        index = subject.active_index(self.root).read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.run_format()
        self.assertEqual(subject.active_index(self.root).read_bytes(), index)
        self.assertTrue((self.root / "a.txt").read_text().startswith("DIFFERENT"))

    def test_names_are_literal_and_modes_are_preserved(self):
        for path in ("space name.txt", "line\nbreak.txt", "[glob].txt", "-option.txt"):
            self.stage(path, "BAD\n")
        (self.root / "-option.txt").chmod(0o755)
        self.git("add", "--", "-option.txt")
        self.run_format()
        self.assertEqual((self.root / "-option.txt").stat().st_mode & 0o777, 0o755)
        for path in ("space name.txt", "line\nbreak.txt", "[glob].txt", "-option.txt"):
            self.assertEqual(self.git("show", ":" + path), b"GOOD\n")

    def test_no_match_does_not_enter_formatter_or_setup(self):
        (self.root / "other.md").write_text("BAD\n")
        self.git("add", ".")
        with patch.object(subject.formatters, "execute") as execute:
            self.assertEqual(subject.run(self.root), 0)
            execute.assert_not_called()

    def test_out_of_scope_or_concurrent_edit_is_not_applied(self):
        self.stage()
        index = subject.active_index(self.root).read_bytes()

        def outside(root, *args, **kwargs):
            (root / "new.txt").write_text("unrequested")

        with self.assertRaisesRegex(ValueError, "outside"):
            self.run_format(outside)

        def concurrent(root, cfg, paths, **kwargs):
            self.formatter(root, cfg, paths)
            (self.root / "a.txt").write_text("concurrent\n")

        with self.assertRaisesRegex(ValueError, "changed during"):
            self.run_format(concurrent)
        self.assertEqual(subject.active_index(self.root).read_bytes(), index)
        self.assertEqual((self.root / "a.txt").read_text(), "concurrent\n")

    def test_active_index_lock_is_not_git_owned_lock(self):
        self.stage()
        original = subject.active_index(self.root)
        alternate = original.with_name("index.lock")
        alternate.write_bytes(original.read_bytes())
        before = original.read_bytes()
        with patch.dict(os.environ, GIT_INDEX_FILE=str(alternate)):
            self.run_format()
        self.assertTrue(alternate.is_file())
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(
            subject.git(self.root, "show", ":a.txt", index=alternate).stdout,
            b"GOOD\nkeep\nkeep\nlast\n",
        )

    def test_real_lock_contention_preserves_lock(self):
        self.stage()
        lock = subject.active_index(self.root).with_name("index.lock")
        lock.write_bytes(b"owned by Git")
        with self.assertRaisesRegex(ValueError, "in use"):
            self.run_format()
        self.assertEqual(lock.read_bytes(), b"owned by Git")

    def test_clean_filters_are_not_executed_and_symlinks_are_preserved(self):
        (self.root / ".gitattributes").write_text("*.txt filter=host-command\n")
        self.git("config", "filter.host-command.clean", "touch leaked; cat")
        (self.root / "link").symlink_to("a.txt")
        self.git("add", ".gitattributes", "link")
        self.stage()
        (self.root / "leaked").unlink(missing_ok=True)
        self.run_format()
        self.assertFalse((self.root / "leaked").exists())
        self.assertTrue((self.root / "link").is_symlink())
        self.assertTrue(self.git("show", ":a.txt").startswith(b"GOOD"))

    def test_interrupted_application_has_original_index_and_files(self):
        self.stage()
        original = subject.active_index(self.root).read_bytes()
        atomic = subject.tc.atomic_bytes

        def interrupted(path, *args, **kwargs):
            if path == self.root / "a.txt":
                raise KeyboardInterrupt()
            return atomic(path, *args, **kwargs)

        with (
            patch.object(subject.tc, "atomic_bytes", interrupted),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.run_format()
        journals = list((self.root / ".chainman/staged-format").glob("*/apply.json"))
        self.assertEqual(len(journals), 1)
        recovery = journals[0].parent
        self.assertEqual((recovery / "original-index").read_bytes(), original)
        self.assertTrue((recovery / "original-0").read_bytes().startswith(b"BAD"))
        with self.assertRaisesRegex(ValueError, "Interrupted"):
            self.run_format()

    def test_concurrent_head_change_is_rejected(self):
        self.stage()
        original = subject.active_index(self.root).read_bytes()

        def formatter(root, cfg, paths, **kwargs):
            self.formatter(root, cfg, paths)
            self.git("update-ref", "-d", "HEAD")

        with self.assertRaisesRegex(ValueError, "changed during"):
            self.run_format(formatter)
        self.assertEqual(subject.active_index(self.root).read_bytes(), original)

    def test_initial_commit_and_alternate_index(self):
        self.git("update-ref", "-d", "HEAD")
        self.stage()
        self.run_format()
        self.assertTrue(self.git("show", ":a.txt").startswith(b"GOOD"))

    def test_real_commit_a_and_pre_staged_file(self):
        self.install_fixture_hook()
        (self.root / "b.txt").write_text("base\n")
        self.git("add", "b.txt")
        self.git("commit", "-qm", "Add b")
        self.stage()
        (self.root / "b.txt").write_text("BAD\n")
        self.real_commit("-am", "Format")
        self.assertEqual(self.git("show", "HEAD:a.txt"), b"GOOD\nkeep\nkeep\nlast\n")
        self.assertEqual(self.git("show", "HEAD:b.txt"), b"GOOD\n")
        self.assertEqual(self.git("diff", "--name-only"), b"")

    def install_fixture_hook(self):
        # A real pre-commit process sees Git's temporary active index. The test
        # formatter is neutral and intentionally avoids Nix/tool downloads.
        script = self.root / ".git/hooks/pre-commit"
        script.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(subject.__file__).parent)!r})
import staged_format
def formatter(root, cfg, paths, **kwargs):
    for path in paths:
        p = root / path
        p.write_bytes(p.read_bytes().replace(b"BAD", b"GOOD"))
staged_format.formatters.execute = formatter
raise SystemExit(staged_format.run(Path.cwd()))
""")
        script.chmod(0o755)

    def real_commit(self, *args):
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_amend_and_path_limited_commit_preserve_other_staging(self):
        self.install_fixture_hook()
        self.stage("other.txt", "BAD other\n")
        self.git("commit", "-qm", "Other")
        self.stage("other.txt", "unrelated staged\n")
        self.stage()
        self.real_commit("-m", "Only a", "--", "a.txt")
        self.assertEqual(self.git("show", "HEAD:other.txt"), b"BAD other\n")
        self.assertEqual(self.git("show", ":other.txt"), b"unrelated staged\n")
        self.stage()
        self.real_commit("--amend", "--no-edit", "--", "a.txt")
        self.assertTrue(self.git("show", "HEAD:a.txt").startswith(b"GOOD"))
        self.assertEqual(self.git("show", ":other.txt"), b"unrelated staged\n")


if __name__ == "__main__":
    unittest.main()
