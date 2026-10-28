"""Exact-source and content checks for the same-run CI closure transfer."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_runtime_closure as closure


SHA = "a" * 40
STORE_PATH = "/nix/store/" + "b" * 32 + "-runtime"


class RuntimeClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / closure.ARCHIVE
        self.archive.write_bytes(b"reviewed runtime closure")
        self.digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_wrong_candidate_and_dirty_checkout_fail_before_import(self):
        for answers in (["b" * 40], [SHA, " M nix/flake.nix"]):
            with self.subTest(answers=answers):
                with patch.object(closure, "output", side_effect=answers):
                    with patch.object(closure.subprocess, "run") as run:
                        with self.assertRaisesRegex(ValueError, "checkout"):
                            closure.import_closure(
                                self.root, self.root, SHA, self.digest
                            )
                        run.assert_not_called()

    def test_malformed_candidate_never_invokes_git_or_nix(self):
        with patch.object(closure, "output") as output:
            with self.assertRaisesRegex(ValueError, "exact candidate"):
                closure.import_closure(self.root, self.root, "master", self.digest)
            output.assert_not_called()

    def test_missing_or_wrong_digest_never_imports(self):
        for digest in ("", "sha256:" + self.digest, "0" * 64):
            with self.subTest(digest=digest):
                with patch.object(closure, "check_source"):
                    with patch.object(closure.subprocess, "run") as run:
                        with self.assertRaisesRegex(ValueError, "SHA-256"):
                            closure.import_closure(self.root, self.root, SHA, digest)
                        run.assert_not_called()

    def test_modified_archive_cannot_supply_its_own_digest(self):
        self.archive.write_bytes(b"changed download")
        (self.root / "sha256").write_text(
            hashlib.sha256(self.archive.read_bytes()).hexdigest()
        )
        with patch.object(closure, "check_source"):
            with patch.object(closure.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "preparation job"):
                    closure.import_closure(self.root, self.root, SHA, self.digest)
                run.assert_not_called()

    def test_symlink_empty_and_oversize_archives_are_rejected(self):
        self.archive.unlink()
        self.archive.symlink_to(self.root / "other")
        with self.assertRaisesRegex(ValueError, "regular file"):
            closure.archive_digest(self.archive)
        self.archive.unlink()
        self.archive.touch()
        with self.assertRaisesRegex(ValueError, "regular file"):
            closure.archive_digest(self.archive)
        self.archive.write_bytes(b"too large")
        with patch.object(closure, "MAX_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "size limit"):
                closure.archive_digest(self.archive)

    def test_different_runtime_is_rejected_without_building(self):
        with patch.object(closure, "check_source"):
            with patch.object(
                closure, "runtime_path", return_value=STORE_PATH
            ) as runtime:
                with patch.object(
                    closure, "output", return_value="/nix/store/" + "c" * 32 + "-other"
                ):
                    with patch.object(closure.subprocess, "run") as run:
                        with self.assertRaisesRegex(
                            ValueError, "does not contain this runtime"
                        ):
                            closure.import_closure(
                                self.root, self.root, SHA, self.digest
                            )
                        runtime.assert_called_once_with(self.root, build=False)
                        self.assertEqual(run.call_count, 1)

    def test_failed_nix_import_cannot_report_success(self):
        with patch.object(closure, "check_source"):
            with patch.object(
                closure.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "nix-store"),
            ):
                with patch.object(closure, "runtime_path") as runtime:
                    with self.assertRaises(subprocess.CalledProcessError):
                        closure.import_closure(self.root, self.root, SHA, self.digest)
                    runtime.assert_not_called()


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_CI_RUNTIME") == "1",
    "run just ci-runtime-test for isolated Nix stores",
)
class NativeRuntimeClosureTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_real_export_import_into_empty_store_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory(prefix="chainman closure ") as temporary:
            root = Path(temporary)
            repo = root / "source"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "tracked").write_text("source identity\n")
            subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=repo,
                check=True,
            )
            candidate = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            payload = root / "payload"
            payload.write_text("runtime content\n")
            archive = root / "transfer"
            source_store = "local?root=" + str(root / "source store")
            target_store = "local?root=" + str(root / "target store")
            with patch.dict(os.environ, {"NIX_REMOTE": source_store}):
                runtime = subprocess.check_output(
                    ["nix-store", "--add", str(payload)], text=True
                ).strip()
                with patch.object(closure, "runtime_path", return_value=runtime):
                    digest = closure.export_closure(repo, archive, candidate)
            with patch.dict(os.environ, {"NIX_REMOTE": target_store}):
                with patch.object(closure, "runtime_path", return_value=runtime):
                    closure.import_closure(repo, archive, candidate, digest)
                subprocess.run(["nix-store", "--verify-path", runtime], check=True)
            physical = root / "target store" / runtime.lstrip("/")
            self.assertEqual(physical.read_text(), payload.read_text())
            with (archive / closure.ARCHIVE).open("ab") as stream:
                stream.write(b"tampered")
            with patch.object(closure.subprocess, "run") as run:
                with patch.object(closure, "check_source"):
                    with self.assertRaisesRegex(ValueError, "SHA-256"):
                        closure.import_closure(repo, archive, candidate, digest)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
