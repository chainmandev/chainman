"""Application failures preserve exact Git/filesystem state for explicit recovery."""

import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

import test_update_staging as staging

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_staging as subject
import updates


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = staging.StagingTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root, self.stage = self.fixture.root, self.fixture.stage
        self.candidate = self.fixture.candidate
        config = self.root / "chainman.toml"
        config.write_text(
            config.read_text().replace(
                'outputs=["dependency.lock"]', 'outputs=["*.lock"]'
            )
        )
        self.old = {
            "b-update.lock": (b"before\n", 0o640),
            "c-mode.lock": (b"same bytes\n", 0o644),
            "d-delete.lock": (b"delete me\n", 0o600),
            "e-last.lock": (b"last before\n", 0o644),
        }
        self.new = {
            "a-create.lock": (b"created\n", 0o600),
            "b-update.lock": (b"after\n", 0o640),
            "c-mode.lock": (b"same bytes\n", 0o755),
            "d-delete.lock": None,
            "e-last.lock": (b"last after\n", 0o644),
        }
        for name, (body, mode) in self.old.items():
            (self.root / name).write_bytes(body)
            (self.root / name).chmod(mode)
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-m", "Application fixture")
        self.identity = updates.repository(self.root)
        self.original_index = subject.index(self.root)
        self.baseline = self.files(self.root)

    def files(self, root):
        # Independent oracle: actual bytes and full permission bits, including
        # untracked temporary files. No production snapshot/hash implementation.
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes(),
                path.stat().st_mode & 0o777,
            )
            for path in root.rglob("*")
            if path.is_file()
            and not set(path.relative_to(root).parts) & {".git", ".cache"}
        }

    def prepare(self, *args):
        self.fixture.prepare(*args)
        for name, output in self.new.items():
            path = self.candidate / name
            if output is None:
                path.unlink()
            else:
                path.write_bytes(output[0])
                path.chmod(output[1])
        subject.inspect(self.root, self.stage)
        self.candidate_files = self.files(self.candidate)
        self.checkpoint = (self.stage / "control/state.json").read_bytes()

    def expected(self, applied):
        files = dict(self.baseline)
        for name in applied:
            output = self.new[name]
            if output is None:
                files.pop(name, None)
            else:
                files[name] = output
        return files

    def finish(self):
        return self.fixture.finish()

    def assert_preserved(self, expected, *, staged=False):
        self.assertEqual(self.files(self.root), expected)
        self.assertEqual(updates.repository(self.root, clean=False), self.identity)
        if not staged:
            self.assertEqual(subject.index(self.root), self.original_index)
        self.assertEqual(self.files(self.candidate), self.candidate_files)
        self.assertEqual(
            (self.stage / "control/state.json").read_bytes(), self.checkpoint
        )

    def test_failed_replace_preserves_old_destination_and_prior_completed_write(self):
        self.prepare()
        real_replace = os.replace

        def fail(source, destination, *args, **kwargs):
            if Path(destination) == self.root / "b-update.lock":
                raise OSError(errno.ENOSPC, "injected full filesystem")
            return real_replace(source, destination, *args, **kwargs)

        with patch.object(os, "replace", side_effect=fail), self.assertRaises(OSError):
            self.finish()
        self.assert_preserved(self.expected(["a-create.lock"]))
        with self.assertRaisesRegex(ValueError, "Original checkout changed"):
            subject.resume(self.root, self.stage)
        self.assert_preserved(self.expected(["a-create.lock"]))

    def test_failed_delete_preserves_undeleted_file_and_completed_bytes_and_modes(self):
        self.prepare()
        unlink = Path.unlink

        def fail(path, *args, **kwargs):
            if path == self.root / "d-delete.lock":
                raise OSError(errno.EACCES, "injected deletion failure")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail), self.assertRaises(OSError):
            self.finish()
        self.assert_preserved(self.expected(list(self.new)[:3]))

    def test_interruption_after_delete_preserves_deletion_without_committing(self):
        self.prepare()
        unlink = Path.unlink

        def fail(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path == self.root / "d-delete.lock":
                raise KeyboardInterrupt("injected interruption")
            return result

        with patch.object(Path, "unlink", fail), self.assertRaises(KeyboardInterrupt):
            self.finish()
        self.assert_preserved(self.expected(list(self.new)[:4]))

    def test_index_lock_failure_preserves_applied_files_and_original_index(self):
        self.prepare()
        index_lock = self.root / ".git/index.lock"
        index_lock.write_bytes(b"another index writer")
        with self.assertRaises(subprocess.CalledProcessError):
            self.finish()
        self.assert_preserved(self.expected(self.new))
        self.assertEqual(index_lock.read_bytes(), b"another index writer")

    def test_commit_creation_failure_preserves_exact_staged_candidate(self):
        self.prepare()
        real_git = updates.git

        def fail(root, *args, **kwargs):
            if root == self.root and args[0] == "commit-tree":
                raise subprocess.CalledProcessError(1, ["git", *args])
            return real_git(root, *args, **kwargs)

        with (
            patch.object(updates, "git", side_effect=fail),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.finish()
        expected = self.expected(self.new)
        self.assert_preserved(expected, staged=True)
        self.assert_staged(expected)

    def assert_staged(self, expected):
        entries = subject.index(self.root)
        self.assertEqual(set(entries), set(expected))
        for name, (body, mode) in expected.items():
            self.assertEqual(entries[name][0], "100755" if mode & 0o100 else "100644")
            result = subprocess.run(
                ["git", "cat-file", "blob", entries[name][1]],
                cwd=self.root,
                check=True,
                capture_output=True,
            )
            self.assertEqual(result.stdout, body)

    def test_concurrent_edit_between_writes_is_preserved(self):
        self.prepare("--no-commit")
        write = subject.tc.atomic_bytes

        def edit(path, *args, **kwargs):
            result = write(path, *args, **kwargs)
            if path == self.root / "a-create.lock":
                (self.root / "b-update.lock").write_bytes(
                    b"user edit during application\n"
                )
            return result

        with (
            patch.object(subject.tc, "atomic_bytes", side_effect=edit),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.finish()
        expected = self.expected(["a-create.lock"])
        expected["b-update.lock"] = (b"user edit during application\n", 0o640)
        self.assert_preserved(expected)

    def test_concurrent_deletion_target_edit_is_preserved(self):
        self.prepare()
        write = subject.tc.atomic_bytes

        def edit(path, *args, **kwargs):
            result = write(path, *args, **kwargs)
            if path == self.root / "c-mode.lock":
                (self.root / "d-delete.lock").write_bytes(b"user kept this file\n")
            return result

        with (
            patch.object(subject.tc, "atomic_bytes", side_effect=edit),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.finish()
        expected = self.expected(list(self.new)[:3])
        expected["d-delete.lock"] = (b"user kept this file\n", 0o600)
        self.assert_preserved(expected)

    def test_concurrent_mode_change_is_preserved(self):
        self.prepare()
        write = subject.tc.atomic_bytes

        def edit(path, *args, **kwargs):
            result = write(path, *args, **kwargs)
            if path == self.root / "b-update.lock":
                (self.root / "c-mode.lock").chmod(0o600)
            return result

        with (
            patch.object(subject.tc, "atomic_bytes", side_effect=edit),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            self.finish()
        expected = self.expected(list(self.new)[:2])
        expected["c-mode.lock"] = (b"same bytes\n", 0o600)
        self.assert_preserved(expected)

    def kill_at(self, phase):
        self.prepare()
        # Kill only this disposable child, after synchronous filesystem/Git
        # boundaries. No production test hooks and no timed race against a write.
        script = r"""
import os
from pathlib import Path
import signal
import sys
sys.path.insert(0, sys.argv[1])
import update_staging as subject
import updates
root, destination, phase = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
write, git = subject.tc.atomic_bytes, updates.git
def stop():
    os.kill(os.getpid(), signal.SIGKILL)
def write_then_stop(path, *args, **kwargs):
    result = write(path, *args, **kwargs)
    if phase == "write" and path == root / "a-create.lock":
        stop()
    return result
def publish(path, *args, **kwargs):
    publishing = path == root and args[0] == "update-ref"
    if publishing and phase == "before-ref":
        stop()
    result = git(path, *args, **kwargs)
    if publishing and phase == "after-ref":
        stop()
    return result
subject.tc.atomic_bytes = write_then_stop
updates.git = publish
subject.finalize(root, destination)
"""
        result = subject.tc.managed_run(
            [
                sys.executable,
                "-B",
                "-c",
                script,
                str(Path(subject.__file__).parent),
                str(self.root),
                str(self.stage),
                phase,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            result.returncode, -signal.SIGKILL, result.stdout + result.stderr
        )
        self.assertEqual(result.stdout, "")

    @unittest.skipUnless(
        hasattr(signal, "SIGKILL"), "requires POSIX process termination"
    )
    def test_process_kill_after_first_write_releases_leases_and_preserves_partial_files(
        self,
    ):
        self.kill_at("write")
        self.assert_preserved(self.expected(["a-create.lock"]))
        with self.assertRaisesRegex(ValueError, "Original checkout changed"):
            subject.resume(self.root, self.stage)
        self.assert_preserved(self.expected(["a-create.lock"]))

    @unittest.skipUnless(
        hasattr(signal, "SIGKILL"), "requires POSIX process termination"
    )
    def test_process_kill_before_ref_update_leaves_exact_staged_files_on_old_head(self):
        self.kill_at("before-ref")
        expected = self.expected(self.new)
        self.assert_preserved(expected, staged=True)
        self.assert_staged(expected)
        with self.assertRaisesRegex(ValueError, "Original checkout changed"):
            subject.resume(self.root, self.stage)

    @unittest.skipUnless(
        hasattr(signal, "SIGKILL"), "requires POSIX process termination"
    )
    def test_process_kill_after_ref_update_keeps_one_complete_commit(self):
        self.kill_at("after-ref")
        branch, head = updates.repository(self.root)
        self.assertEqual(branch, self.identity[0])
        self.assertNotEqual(head, self.identity[1])
        self.assertEqual(updates.git(self.root, "rev-parse", "HEAD^"), self.identity[1])
        expected = self.expected(self.new)
        self.assertEqual(self.files(self.root), expected)
        self.assert_staged(expected)
        self.assertEqual(
            updates.git(self.root, "write-tree"),
            updates.git(self.root, "rev-parse", "HEAD^{tree}"),
        )
        self.assertEqual(self.files(self.candidate), self.candidate_files)
        with self.assertRaisesRegex(ValueError, "Original checkout changed"):
            subject.resume(self.root, self.stage)
        self.assertEqual(updates.repository(self.root), (branch, head))


class RecoveryMachine(RuleBasedStateMachine):
    """Model explicit operator recovery, reinspection and interrupted application."""

    def __init__(self):
        super().__init__()
        self.case = ApplicationTests()
        try:
            self.case.setUp()
            self.case.prepare("--no-commit")
        except BaseException:
            self.case.doCleanups()
            raise
        self.phase = "ready"
        self.model = dict(self.case.baseline)

    def teardown(self):
        self.case.doCleanups()

    @precondition(lambda self: self.phase == "ready")
    @rule(prefix=st.integers(min_value=0, max_value=4))
    def interrupt_application(self, prefix):
        write, unlink = subject.tc.atomic_bytes, Path.unlink
        stop = list(self.case.new)[prefix]

        def before(path):
            if path == self.case.root / stop:
                raise OSError(errno.ENOSPC, "generated application failure")

        def checked_write(path, *args, **kwargs):
            before(path)
            return write(path, *args, **kwargs)

        def checked_unlink(path, *args, **kwargs):
            before(path)
            return unlink(path, *args, **kwargs)

        with (
            patch.object(subject.tc, "atomic_bytes", checked_write),
            patch.object(Path, "unlink", checked_unlink),
            self.case.assertRaises(OSError),
        ):
            self.case.finish()
        self.model = self.case.expected(list(self.case.new)[:prefix])
        self.phase = "partial" if prefix else "ready"

    @precondition(lambda self: self.phase == "partial")
    @rule()
    def refuse_resume_over_partial_application(self):
        with self.case.assertRaisesRegex(ValueError, "Original checkout changed"):
            subject.resume(self.case.root, self.case.stage)

    @precondition(lambda self: self.phase in ("partial", "ready"))
    @rule()
    def operator_restores_original_and_resumes(self):
        # This is an explicit operator action, not automatic runtime rollback.
        # Reconstruct only the fixture's declared outputs from independent bytes.
        for name in self.case.new:
            path = self.case.root / name
            if name in self.case.baseline:
                body, mode = self.case.baseline[name]
                path.write_bytes(body)
                path.chmod(mode)
            else:
                path.unlink(missing_ok=True)
        self.model = dict(self.case.baseline)
        subject.resume(self.case.root, self.case.stage)
        self.phase = "uninspected"

    @precondition(lambda self: self.phase == "uninspected")
    @rule()
    def refuse_stale_inspection(self):
        with self.case.assertRaisesRegex(ValueError, "not been inspected"):
            self.case.finish()

    @precondition(lambda self: self.phase == "uninspected")
    @rule()
    def inspect_candidate(self):
        subject.inspect(self.case.root, self.case.stage)
        self.phase = "ready"

    @precondition(lambda self: self.phase == "ready")
    @rule()
    def apply_verified_candidate(self):
        result = self.case.finish()
        self.case.assertIsNone(result["commit"])
        self.case.assertEqual(result["changed"], list(self.case.new))
        self.model = self.case.expected(self.case.new)
        self.phase = "complete"

    @rule()
    def observe(self):
        pass

    @invariant()
    def exact_sources_index_head_and_candidate(self):
        self.case.assertEqual(self.case.files(self.case.root), self.model)
        self.case.assertEqual(subject.index(self.case.root), self.case.original_index)
        self.case.assertEqual(
            updates.repository(self.case.root, clean=False), self.case.identity
        )
        self.case.assertEqual(
            self.case.files(self.case.candidate), self.case.candidate_files
        )


TestRecoveryMachine = RecoveryMachine.TestCase
TestRecoveryMachine.settings = settings(
    max_examples=20, stateful_step_count=12, derandomize=True, deadline=None
)


if __name__ == "__main__":
    unittest.main()
