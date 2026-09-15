"""Git is the only runtime distribution; exports retain exact committed bytes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_runtime


class DistributionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman Git source ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.origin / "VERSION").write_text("0.1.0\n")
        (self.origin / "executable").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.origin / "executable").chmod(0o755)
        (self.origin / "binary").write_bytes(b"\0binary\xff\n")
        self.commit()
        self.revision = self.git("rev-parse", "HEAD")
        repository = patch.object(git_runtime, "REPOSITORY", self.origin.as_uri())
        repository.start()
        self.addCleanup(repository.stop)
        environment = patch.dict(os.environ, XDG_CACHE_HOME=str(self.root / "cache"))
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.origin), *args], text=True
        ).strip()

    def commit(self):
        self.git("add", ".")
        self.git(
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "Runtime",
        )

    def test_default_branch_discovery_and_rename(self):
        self.assertEqual(git_runtime.default_revision(), self.revision)
        self.git("branch", "-m", "renamed-default-δ")
        self.assertEqual(git_runtime.default_revision(), self.revision)

    def test_rewritten_history_and_branch_advance_are_new_snapshots(self):
        selected = git_runtime.default_revision()
        self.git("checkout", "--orphan", "replacement")
        (self.origin / "binary").write_text("new history")
        self.commit()
        replacement = git_runtime.default_revision()
        self.assertNotEqual(selected, replacement)
        # Already-selected old commits still materialize after a branch rewrite.
        git_runtime.materialize(selected, self.root / "old")
        git_runtime.materialize(replacement, self.root / "new")
        self.assertEqual((self.root / "old/binary").read_bytes(), b"\0binary\xff\n")
        self.assertEqual((self.root / "new/binary").read_text(), "new history")

    def test_empty_missing_and_detached_default_branch_fail(self):
        self.git("symbolic-ref", "HEAD", "refs/heads/unborn")
        with self.assertRaisesRegex(ValueError, "default branch"):
            git_runtime.default_revision()
        self.git("update-ref", "--no-deref", "HEAD", self.revision)
        with self.assertRaisesRegex(ValueError, "default branch"):
            git_runtime.default_revision()
        self.origin.rename(self.root / "offline")
        with self.assertRaisesRegex(ValueError, "default branch"):
            git_runtime.default_revision()

    def test_inconsistent_or_malformed_advertisements_fail(self):
        for advertisement in (
            b"",
            b"ref: refs/heads/current\tHEAD\n",
            b"a" * 40 + b"\tHEAD\n",
            b"ref: refs/heads/current\tHEAD\n"
            + b"a" * 40
            + b"\tHEAD\n"
            + b"b" * 40
            + b"\trefs/heads/current\n",
            b"ref: refs/tags/current\tHEAD\n" + b"a" * 40 + b"\tHEAD\n",
            b"not a Git advertisement",
        ):
            with (
                self.subTest(advertisement=advertisement),
                patch.object(git_runtime, "git", return_value=advertisement),
                self.assertRaisesRegex(ValueError, "default branch"),
            ):
                git_runtime.default_revision()

    def test_exact_export_ignores_checkout_changes_and_attributes(self):
        (self.origin / ".gitattributes").write_text("binary export-ignore\n")
        self.commit()
        revision = self.git("rev-parse", "HEAD")
        (self.origin / "VERSION").write_text("uncommitted\n")
        (self.origin / "untracked").write_text("excluded")
        destination = self.root / "export"
        git_runtime.materialize(revision, destination)
        self.assertEqual((destination / "VERSION").read_text(), "0.1.0\n")
        self.assertEqual((destination / "binary").read_bytes(), b"\0binary\xff\n")
        self.assertTrue((destination / "executable").stat().st_mode & 0o100)
        self.assertFalse((destination / "untracked").exists())
        self.assertFalse((destination / ".git").exists())

    def test_warm_objects_work_without_origin(self):
        git_runtime.materialize(self.revision, self.root / "first")
        self.origin.rename(self.root / "offline")
        git_runtime.materialize(self.revision, self.root / "second")
        self.assertEqual(
            (self.root / "first/binary").read_bytes(),
            (self.root / "second/binary").read_bytes(),
        )

    def test_corrupt_git_blob_is_rejected(self):
        cache = git_runtime.objects(self.revision)
        oid = self.git("rev-parse", "HEAD:VERSION")
        path = cache / "objects" / oid[:2] / oid[2:]
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            path.chmod(0o600)
        path.write_bytes(zlib.compress(b"blob 6\0wrong\n"))
        with self.assertRaises(subprocess.CalledProcessError):
            git_runtime.materialize(self.revision, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_symlink_tree_is_rejected(self):
        (self.origin / "link").symlink_to("VERSION")
        self.commit()
        with self.assertRaisesRegex(ValueError, "ordinary contained files"):
            git_runtime.materialize(self.git("rev-parse", "HEAD"), self.root / "bad")

    def test_unavailable_revision_cannot_fall_back(self):
        with self.assertRaises(subprocess.CalledProcessError):
            git_runtime.materialize("0" * 40, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())

    def test_archive_lock_is_not_supported(self):
        for body in (
            b'{"revision":"' + b"a" * 40 + b'"}\n',
            b"a" * 40,
            b"A" * 40 + b"\n",
        ):
            with self.assertRaises(ValueError):
                git_runtime.pin(body)


if __name__ == "__main__":
    unittest.main()
