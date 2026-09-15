"""Git runtime selection preserves pins, frozen identity and concurrent edits."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman
import chainman_updates as subject
import registry


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman candidate data ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "chainman.toml").write_text(
            'schema=3\n[project]\ndefault_profile="host"\n'
        )
        self.old = "a" * 40
        self.new = "b" * 40
        (self.root / "chainman.lock").write_text(self.old + "\n")
        (self.root / "justfile").write_text("# project-owned bootstrap and workflows\n")
        self.previous = self.base / "previous"
        self.candidate = self.base / "candidate"
        for tree, value in ((self.previous, "1.0.0"), (self.candidate, "2.0.0")):
            for name in (
                "bootstrap/chainman.sh",
                "bootstrap/fetch.nix",
                "nix/flake.nix",
                "nix/flake.lock",
                "scripts/chainman.py",
                "scripts/chainman_updates.py",
            ):
                path = tree / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture: must never execute")
            (tree / "VERSION").write_text(value + "\n")
        self.now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.published = self.now - timedelta(days=40)
        self.release = registry.Release("v2.0.0", self.published)
        self.metadata = {
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
            "immutable": False,
            "published_at": self.published.isoformat(),
        }
        patches = [
            patch.object(chainman, "RUNTIME", self.previous),
            patch.object(registry, "github_releases", return_value=[self.release]),
            patch.object(registry, "github_commit", return_value=self.new),
            patch.object(
                subject.source_updates, "commit_time", return_value=self.published
            ),
            patch.object(registry, "data", side_effect=lambda _: dict(self.metadata)),
            patch.object(subject.git_runtime, "store", return_value=self.candidate),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.before = self.state()

    def state(self):
        return {
            name: subject.managed_state(self.root, name)
            for name in subject.managed_paths(self.root)
        }

    def select(self, managed=None):
        return subject.runtime_candidate(
            self.root, {}, self.now, managed, gc_root=self.base / "runtime-root"
        )

    def add_copy(self):
        path = self.root / "templates/common"
        path.mkdir(parents=True)
        shutil.copy2(self.root / "chainman.lock", path / "chainman.lock")
        with (self.root / "chainman.toml").open("a") as stream:
            stream.write('\n[runtime]\ncopies=["templates/common"]\n')
        return path

    def test_mutable_published_release_updates_only_the_pin(self):
        before_recipe = (self.root / "justfile").read_bytes()
        self.assertEqual(self.select(), self.candidate)
        self.assertEqual((self.root / "chainman.lock").read_text(), self.new + "\n")
        self.assertEqual((self.root / "justfile").read_bytes(), before_recipe)
        self.assertEqual(set(self.state()), {"chainman.lock"})
        self.assertFalse(list(self.root.rglob("*.tar.gz")))
        self.assertTrue(self.previous.exists())

    def test_newest_eligible_major_skips_a_young_release(self):
        self.mocks[1].return_value = [
            self.release,
            registry.Release("v3.0.0", self.now - timedelta(days=5)),
        ]
        self.assertEqual(self.select(), self.candidate)
        self.mocks[5].assert_called_once_with(
            self.new, gc_root=self.base / "runtime-root"
        )

    def test_candidate_runtime_drives_resolution_and_verification(self):
        with (
            patch.object(subject, "runtime_candidate", return_value=self.candidate),
            patch.object(subject.tc, "managed_run") as execute,
        ):
            runtime = subject.perform(
                self.root, {}, self.now, ["two words"], skip_runtime=False
            )
            subject.verify(self.root, {}, runtime)
        commands = [call.args[0] for call in execute.call_args_list]
        self.assertTrue(
            any(
                str(self.candidate / "scripts/chainman_updates.py") in command
                and "--resolve-root" in command
                for command in commands
            )
        )
        self.assertTrue(
            any(
                str(self.candidate / "scripts/chainman_updates.py") in command
                and "--verify-root" in command
                for command in commands
            )
        )
        self.assertFalse(
            any(str(self.candidate / "tests") in command for command in commands)
        )
        for call in execute.call_args_list:
            self.assertEqual(call.kwargs["env"]["TOOLCHAIN_FRESH"], "1")

    def test_unchanged_version_does_not_fetch_or_rewrite(self):
        (self.previous / "VERSION").write_text("2.0.0\n")
        self.assertEqual(self.select(), self.previous)
        self.mocks[5].assert_not_called()
        self.assertEqual(self.state(), self.before)

    def test_drafts_prereleases_and_changed_publication_fail(self):
        for key, value in (
            ("draft", True),
            ("prerelease", True),
            ("published_at", self.now.isoformat()),
        ):
            with (
                self.subTest(key=key),
                patch.dict(self.metadata, {key: value}),
                self.assertRaises(ValueError),
            ):
                self.select()
        self.mocks[5].assert_not_called()
        self.assertEqual(self.state(), self.before)

    def test_commit_age_is_checked_before_candidate_fetch(self):
        self.mocks[3].return_value = self.now
        with self.assertRaisesRegex(ValueError, "No eligible"):
            self.select()
        self.mocks[5].assert_not_called()
        self.assertEqual(self.state(), self.before)

    def test_age_boundary_is_inclusive(self):
        self.mocks[3].return_value = self.now - timedelta(days=30)
        self.assertEqual(self.select(), self.candidate)

    def test_moved_tag_is_rejected_before_pin_publication(self):
        self.mocks[2].side_effect = [self.new, self.new, "c" * 40]
        with self.assertRaisesRegex(ValueError, "tag changed"):
            self.select()
        self.assertEqual(self.state(), self.before)

    def test_wrong_version_and_invalid_tree_fail_before_publication(self):
        (self.candidate / "VERSION").write_text("9.0.0\n")
        with self.assertRaisesRegex(ValueError, "VERSION"):
            self.select()
        (self.candidate / "VERSION").write_text("2.0.0\n")
        (self.candidate / "unsafe").symlink_to(self.previous / "VERSION")
        with self.assertRaisesRegex(ValueError, "regular files"):
            self.select()
        self.assertEqual(self.state(), self.before)

    def test_fetch_failure_preserves_pin(self):
        self.mocks[5].side_effect = ValueError("Git object corruption")
        with self.assertRaisesRegex(ValueError, "corruption"):
            self.select()
        self.assertEqual(self.state(), self.before)

    def test_declared_copies_update_and_restore_exactly(self):
        copy = self.add_copy()
        (self.root / "chainman.lock").chmod(0o664)
        (copy / "chainman.lock").chmod(0o444)
        before = self.state()
        managed = subject.ManagedFiles(self.root)
        self.select(managed)
        self.assertEqual((copy / "chainman.lock").read_text(), self.new + "\n")
        managed.restore()
        self.assertEqual(self.state(), before)

    def test_modified_copy_blocks_all_publication(self):
        copy = self.add_copy()
        (copy / "chainman.lock").write_text("c" * 40 + "\n")
        before = self.state()
        with self.assertRaisesRegex(ValueError, "locally modified"):
            self.select()
        self.mocks[5].assert_not_called()
        self.assertEqual(self.state(), before)

    def test_copy_execution_bit_change_is_not_a_permission_normalization(self):
        copy = self.add_copy()
        (copy / "chainman.lock").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "locally modified"):
            self.select()

    def test_copy_symlink_or_escape_is_not_followed(self):
        copy = self.add_copy()
        (copy / "chainman.lock").unlink()
        (copy / "chainman.lock").symlink_to(self.root / "chainman.lock")
        with self.assertRaises(ValueError):
            self.select()
        self.assertEqual((self.root / "chainman.lock").read_text(), self.old + "\n")

    def test_concurrent_change_during_fetch_is_preserved(self):
        def changed(*args, **kwargs):
            (self.root / "chainman.lock").write_text("c" * 40 + "\n")
            return self.candidate

        self.mocks[5].side_effect = changed
        with self.assertRaisesRegex(ValueError, "changed during preparation"):
            self.select()
        self.assertEqual((self.root / "chainman.lock").read_text(), "c" * 40 + "\n")

    def test_partial_publication_restores_only_our_changes(self):
        self.add_copy()
        before = self.state()
        real = subject.tc.atomic_bytes

        def fail_copy(path, *args, **kwargs):
            if str(path).endswith("common/chainman.lock"):
                raise OSError("fixture write failure")
            return real(path, *args, **kwargs)

        with (
            patch.object(subject.tc, "atomic_bytes", side_effect=fail_copy),
            self.assertRaises(OSError),
        ):
            self.select()
        self.assertEqual(self.state(), before)

    def test_rollback_preserves_concurrent_mode_and_byte_edits(self):
        managed = subject.ManagedFiles(self.root)
        self.select(managed)
        path = self.root / "chainman.lock"
        path.write_text("c" * 40 + "\n")
        path.chmod(0o600)
        managed.restore()
        self.assertEqual(path.read_text(), "c" * 40 + "\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_runtime_root_lasts_through_resolution(self):
        roots = []

        def selected(*args, gc_root, **kwargs):
            gc_root.symlink_to(self.candidate)
            roots.append(gc_root)
            return self.candidate

        def resolved(*args, **kwargs):
            self.assertTrue(roots[0].is_symlink())
            raise ValueError("resolver failure")

        with (
            patch.object(subject, "runtime_candidate", side_effect=selected),
            patch.object(subject.tc, "managed_run", side_effect=resolved),
            self.assertRaisesRegex(ValueError, "resolver failure"),
        ):
            subject.perform(self.root, {}, self.now, [], skip_runtime=False)
        self.assertFalse(roots[0].parent.exists())


if __name__ == "__main__":
    unittest.main()
