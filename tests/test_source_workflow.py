"""Source development must preserve original Git/source on failed acceptance."""

import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_workflow
import updates


class SourceWorkflows(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "source"
        self.root.mkdir()
        (self.root / ".gitignore").write_text(".cache/\n.chainman/\n")
        (self.root / "modules").mkdir()
        (self.root / "modules/core.toml").write_text(
            'name="core"\nprofile="core"\ndirectory="."\n'
        )
        (self.root / "toolchain.toml").write_text(
            'schema=1\nmodules=["core"]\n[updates]\noutputs=["dependency.lock"]\n'
        )
        (self.root / "dependencies.toml").write_text("minimum_age_days=30\n")
        (self.root / "dependency.lock").write_text("old\n")
        updates.git(self.root, "init", "-b", "main")
        updates.git(self.root, "config", "user.name", "Test")
        updates.git(self.root, "config", "user.email", "test@example.invalid")
        updates.git(self.root, "config", "commit.gpgsign", "false")
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-m", "Initial")
        self.before = updates.snapshot(self.root)
        self.environment = patch.dict(
            os.environ, XDG_CACHE_HOME=str(self.base / "cache")
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def resolve(self, root, *args):
        self.assertNotEqual(root, self.root)
        (root / "dependency.lock").write_text("new\n")

    def test_failed_source_update_preserves_original_and_candidate(self):
        with (
            patch.object(updates, "perform", side_effect=self.resolve),
            patch.object(updates, "verify", side_effect=ValueError("gate rejected")),
        ):
            with self.assertRaisesRegex(ValueError, "gate rejected"):
                source_workflow.run(self.root, "deps-update", [])
        self.assertEqual(updates.snapshot(self.root), self.before)
        self.assertEqual(updates.git(self.root, "status", "--porcelain"), "")
        candidates = list(
            (self.base / "cache/chainman/updates").glob("candidate.*/candidate")
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0] / "dependency.lock").read_text(), "new\n")

    def test_source_update_commits_only_after_candidate_verification(self):
        def verify(root, selected):
            self.assertNotEqual(root, self.root)
            self.assertEqual((root / "dependency.lock").read_text(), "new\n")
            self.assertEqual(updates.snapshot(self.root), self.before)

        with (
            patch.object(updates, "perform", side_effect=self.resolve),
            patch.object(updates, "verify", side_effect=verify),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            source_workflow.run(self.root, "deps-update", [])
        self.assertEqual((self.root / "dependency.lock").read_text(), "new\n")
        self.assertEqual(updates.git(self.root, "status", "--porcelain"), "")

    def test_source_format_failure_preserves_original(self):
        def formatter(root, *, check=False, staged=False):
            if check:
                raise ValueError("format rejected")
            self.resolve(root)

        with patch.object(source_workflow, "format_source", side_effect=formatter):
            with self.assertRaisesRegex(ValueError, "format rejected"):
                source_workflow.run(self.root, "format", [])
        self.assertEqual(updates.snapshot(self.root), self.before)


if __name__ == "__main__":
    unittest.main()
