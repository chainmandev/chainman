"""Source development must preserve original Git/source on failed acceptance."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
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
        self.base = Path(temporary.name).resolve()
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

        # Exercise the Python worker in process so resolver/verification mocks
        # remain effective. The native supervisor's lifecycle is qualified in
        # update_cache_test.go and through real bootstrap tests.
        def supervise(root, action, arguments):
            destination = self.base / "cache/chainman/updates/v1/candidate.fixture"
            destination.mkdir(parents=True, exist_ok=True)
            with patch.dict(os.environ, CHAINMAN_UPDATE_TRANSACTION=str(destination)):
                source_workflow.run(root, action, arguments)

        supervisor = patch.object(
            source_workflow.update_cache, "run", side_effect=supervise
        )
        supervisor.start()
        self.addCleanup(supervisor.stop)

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
            (self.base / "cache/chainman/updates/v1").glob("candidate.*/candidate")
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

    def test_force_runs_verification_and_reports_override_after_failure(self):
        with (
            patch.object(updates, "perform", side_effect=self.resolve),
            patch.object(
                updates,
                "verify",
                side_effect=subprocess.CalledProcessError(19, ["verify"]),
            ) as verify,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as diagnostics,
        ):
            source_workflow.run(self.root, "deps-update", ["--force"])
        verify.assert_called_once()
        record = json.loads(output.getvalue())
        self.assertEqual(record["verification"], "forced")
        self.assertEqual(record["verification_exit_code"], 19)
        self.assertEqual(record["commit"], updates.git(self.root, "rev-parse", "HEAD"))
        self.assertEqual((self.root / "dependency.lock").read_text(), "new\n")
        self.assertIn("--force", diagnostics.getvalue())

    def test_no_verify_skips_gate_but_still_resolves_and_reports(self):
        with (
            patch.object(updates, "perform", side_effect=self.resolve) as resolve,
            patch.object(
                updates, "verify", side_effect=AssertionError("gate ran")
            ) as verify,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as diagnostics,
        ):
            source_workflow.run(self.root, "deps-update", ["--no-verify"])
        resolve.assert_called_once()
        verify.assert_not_called()
        record = json.loads(output.getvalue())
        self.assertEqual(record["verification"], "skipped")
        self.assertNotIn("verification_exit_code", record)
        self.assertEqual(
            record["commit"], updates.git(self.root, "rev-parse", "HEAD").strip()
        )
        self.assertIn("--no-verify", diagnostics.getvalue())

    def test_no_verify_resume_reaudits_and_does_not_reuse_consent(self):
        with (
            patch.object(updates, "perform", side_effect=self.resolve) as resolve,
            patch.object(
                updates,
                "verify",
                side_effect=subprocess.CalledProcessError(1, ["verify"]),
            ) as verify,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                source_workflow.run(self.root, "deps-update", [])
            destination = self.base / "cache/chainman/updates/v1/candidate.fixture"
            with (
                patch.object(source_workflow.staging, "reaudit") as audit,
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                source_workflow.run(
                    self.root, "deps-update", [f"resume={destination}", "--no-verify"]
                )
            resolve.assert_called_once()
            verify.assert_called_once()
            audit.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())["verification"], "skipped")
            self.assertNotIn(
                "no_verify",
                json.loads((destination / "control/state.json").read_text())["options"],
            )

    def test_no_verify_does_not_bypass_resolution_or_inspection(self):
        for phase, error in (
            ("perform", subprocess.CalledProcessError(17, ["resolve"])),
            ("inspect", ValueError("rejected output scope")),
        ):
            with (
                self.subTest(phase=phase),
                patch.object(updates, "perform", side_effect=self.resolve),
                patch.object(updates, "verify") as verify,
                patch.object(
                    updates if phase == "perform" else source_workflow.staging,
                    phase,
                    side_effect=error,
                ),
                self.assertRaises(type(error)),
            ):
                destination = self.base / ("skip-failed-" + phase)
                destination.mkdir()
                with patch.dict(
                    os.environ, CHAINMAN_UPDATE_TRANSACTION=str(destination)
                ):
                    source_workflow.run(self.root, "deps-update", ["--no-verify"])
            verify.assert_not_called()
            self.assertEqual(updates.snapshot(self.root), self.before)

    def test_force_can_resume_without_reresolving_or_persisting_consent(self):
        with (
            patch.object(updates, "perform", side_effect=self.resolve) as resolve,
            patch.object(
                updates,
                "verify",
                side_effect=subprocess.CalledProcessError(19, ["verify"]),
            ),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                source_workflow.run(self.root, "deps-update", [])
            destination = self.base / "cache/chainman/updates/v1/candidate.fixture"
            with (
                patch.object(source_workflow.staging, "reaudit") as audit,
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                source_workflow.run(
                    self.root, "deps-update", ["--force", f"resume={destination}"]
                )
            resolve.assert_called_once()
            audit.assert_called_once()
            self.assertEqual(json.loads(output.getvalue())["verification"], "forced")
            checkpoint = json.loads((destination / "control/state.json").read_text())
            self.assertNotIn("force", checkpoint["options"])

    def test_force_preserves_resolver_validation_and_cancellation_failures(self):
        for phase, error in (
            ("perform", subprocess.CalledProcessError(17, ["resolve"])),
            ("verify", subprocess.CalledProcessError(130, ["verify"])),
            ("verify", ValueError("invalid verification inputs")),
        ):
            with (
                self.subTest(phase=phase, error=error),
                patch.object(updates, "perform", side_effect=self.resolve),
                patch.object(updates, "verify"),
                patch.object(updates, phase, side_effect=error),
                self.assertRaises(type(error)),
            ):
                # Each failed attempt owns its own disposable transaction.
                destination = self.base / ("failed-" + str(id(error)))
                destination.mkdir()
                with patch.dict(
                    os.environ, CHAINMAN_UPDATE_TRANSACTION=str(destination)
                ):
                    source_workflow.run(self.root, "deps-update", ["--force"])
            self.assertEqual(updates.snapshot(self.root), self.before)

    def test_force_cannot_accept_verifier_source_changes(self):
        def verify(root, selected):
            (root / "dependency.lock").write_text("changed during verification\n")
            raise subprocess.CalledProcessError(19, ["verify"])

        with (
            patch.object(updates, "perform", side_effect=self.resolve),
            patch.object(updates, "verify", side_effect=verify),
            self.assertRaisesRegex(ValueError, "Verification changed candidate"),
        ):
            source_workflow.run(self.root, "deps-update", ["--force"])
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_full_source_update_does_not_select_a_self_pin(self):
        import chainman_updates

        with (
            patch.object(updates, "perform", side_effect=self.resolve),
            patch.object(updates, "verify"),
            patch.object(
                chainman_updates,
                "runtime_candidate",
                side_effect=AssertionError("source self-update"),
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            source_workflow.run(self.root, "deps-update", ["targets=all"])
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
