"""Failed runtime candidates preserve the usable pin and concurrent user edits."""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman
import chainman_updates as subject
import registry


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman candidate data ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "chainman.toml").write_text(
            'schema=1\n[updates]\nresolver=[["unused"]]\neligibility="resolver"\n'
        )
        self.previous = self.base / "previous"
        self.candidate = self.base / "candidate"
        for tree, value in ((self.previous, "1.0.0"), (self.candidate, "2.0.0")):
            for name in ("bootstrap", "scripts", "nix", "tests"):
                (tree / name).mkdir(parents=True)
            (tree / "VERSION").write_text(value + "\n")
            for name in ("chainman.sh", "fetch.nix"):
                (tree / "bootstrap" / name).write_text(value + " " + name)
            for name in ("chainman.py", "chainman_updates.py"):
                (tree / "scripts" / name).write_text(
                    "raise RuntimeError('fixture must never execute')\n"
                )
            (tree / "nix/flake.nix").write_text('throw "fixture must never evaluate"')
            (tree / "nix/flake.lock").write_text("{}")
        for source, target in (
            ("chainman.sh", "chainman.sh"),
            ("fetch.nix", "chainman-fetch.nix"),
        ):
            shutil.copy2(
                self.previous / "bootstrap" / source, self.root / "scripts" / target
            )
        (self.root / "scripts/chainman.sh").chmod(0o755)
        self.old_lock = {
            "schema": 1,
            "version": "1.0.0",
            "revision": "a" * 40,
            "url": "https://example.invalid/old.tar.gz",
            "narHash": "sha256-" + "A" * 43 + "=",
            "bundled_archive": "bundle.tar.gz",
        }
        (self.root / "chainman.lock").write_text(json.dumps(self.old_lock))
        (self.root / "bundle.tar.gz").write_bytes(b"old bundled bytes")
        self.body = b"new bundled bytes"
        self.metadata = {
            "schema": 1,
            "version": "2.0.0",
            "revision": "b" * 40,
            "url": "https://example.invalid/new.tar.gz",
            "narHash": "sha256-" + "B" * 43 + "=",
            "archive_sha256": hashlib.sha256(self.body).hexdigest(),
        }
        self.now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        release = registry.Release("v2.0.0", datetime(2026, 1, 1, tzinfo=timezone.utc))
        for target, kwargs in (
            ("chainman.RUNTIME", {"new": self.previous}),
            ("registry.github_releases", {"return_value": [release]}),
            ("registry.github_commit", {"return_value": "b" * 40}),
            ("registry.data", {"return_value": self.metadata}),
            ("registry.fetch", {"return_value": (self.body, {})}),
        ):
            context = patch(target, **kwargs)
            context.start()
            self.addCleanup(context.stop)
        self.opts = argparse.Namespace(
            extra=[], only_chainman=True, skip_chainman=False, no_commit=True
        )
        self.before = self.managed()

    def managed(self):
        return {
            name: (
                (self.root / name).read_bytes(),
                (self.root / name).stat().st_mode & 0o777,
            )
            for name in (
                "chainman.lock",
                "scripts/chainman.sh",
                "scripts/chainman-fetch.nix",
                "bundle.tar.gz",
            )
        }

    def transaction(self, root, patterns, update, verify, commit, *, message):
        # This callback exercises publication and verification without Git or
        # consumer execution; Git transaction integrity has its own real tests.
        self.assertEqual(
            message, getattr(self.opts, "message", "chore: update dependencies")
        )
        update()
        verify()
        return {"verification": "passed"}

    def run_apply(self, verifier):
        with (
            patch.object(subject, "fetch_runtime", return_value=self.candidate),
            patch.object(subject.updates, "transaction", side_effect=self.transaction),
            patch.object(subject, "verify", side_effect=verifier),
        ):
            return subject.apply(self.root, self.opts, self.now)

    def test_failed_verification_restores_exact_managed_bytes_and_modes(self):
        def reject(*_):
            self.assertEqual(
                json.loads((self.root / "chainman.lock").read_text())["version"],
                "2.0.0",
            )
            (self.root / "dependency.txt").write_text("resolved dependency remains")
            raise ValueError("candidate verification failed")

        with self.assertRaisesRegex(ValueError, "candidate verification failed"):
            self.run_apply(reject)
        self.assertEqual(self.managed(), self.before)
        self.assertEqual(
            (self.root / "dependency.txt").read_text(), "resolved dependency remains"
        )
        self.assertTrue(self.previous.is_dir())

    def test_concurrent_managed_bytes_and_mode_changes_are_preserved(self):
        def reject(*_):
            (self.root / "chainman.lock").write_text("concurrent pin")
            (self.root / "scripts/chainman.sh").chmod(0o700)
            raise ValueError("candidate verification failed")

        with self.assertRaisesRegex(ValueError, "candidate verification failed"):
            self.run_apply(reject)
        self.assertEqual((self.root / "chainman.lock").read_text(), "concurrent pin")
        self.assertEqual(
            (self.root / "scripts/chainman.sh").stat().st_mode & 0o777, 0o700
        )
        self.assertEqual(
            (self.root / "bundle.tar.gz").read_bytes(), self.before["bundle.tar.gz"][0]
        )

    def test_concurrent_symlink_is_not_followed_or_replaced(self):
        outside = self.base / "outside"
        outside.write_text("keep")

        def reject(*_):
            target = self.root / "scripts/chainman.sh"
            target.unlink()
            target.symlink_to(outside)
            raise ValueError("candidate verification failed")

        with self.assertRaisesRegex(ValueError, "candidate verification failed"):
            self.run_apply(reject)
        self.assertTrue((self.root / "scripts/chainman.sh").is_symlink())
        self.assertEqual(outside.read_text(), "keep")

    def test_success_keeps_candidate_and_previous_runtime(self):
        self.assertEqual(self.run_apply(lambda *_: None), {"verification": "passed"})
        self.assertEqual(
            json.loads((self.root / "chainman.lock").read_text())["version"], "2.0.0"
        )
        self.assertEqual((self.root / "bundle.tar.gz").read_bytes(), self.body)
        self.assertTrue(self.previous.is_dir())

    def test_bad_candidate_tree_is_rejected_before_publication_or_execution(self):
        for bad in ("missing", "symlink", "fifo", "version", "directory", "tests"):
            with self.subTest(bad=bad):
                tree = self.base / ("bad-" + bad)
                shutil.copytree(self.candidate, tree)
                if bad == "missing":
                    (tree / "nix/flake.lock").unlink()
                elif bad == "symlink":
                    (tree / "escape").symlink_to(self.base)
                elif bad == "fifo":
                    os.mkfifo(tree / "pipe")
                elif bad == "version":
                    (tree / "VERSION").write_text("9.9.9\n")
                elif bad == "tests":
                    (tree / "tests").rmdir()
                else:
                    (tree / "scripts/chainman.py").unlink()
                    (tree / "scripts/chainman.py").mkdir()
                with (
                    patch.object(subject, "fetch_runtime", return_value=tree),
                    patch.object(
                        subject.updates, "transaction", side_effect=self.transaction
                    ),
                    patch.object(subject, "verify") as execute,
                ):
                    with self.assertRaises(ValueError):
                        subject.apply(self.root, self.opts, self.now)
                    execute.assert_not_called()
                self.assertEqual(self.managed(), self.before)

    def test_download_or_fetch_failure_does_not_publish_candidate(self):
        for failure in (ValueError("bad archive"), OSError("fetch unavailable")):
            with self.subTest(failure=failure):
                with (
                    patch.object(subject, "fetch_runtime", side_effect=failure),
                    patch.object(
                        subject.updates, "transaction", side_effect=self.transaction
                    ),
                    self.assertRaises(type(failure)),
                ):
                    subject.apply(self.root, self.opts, self.now)
                self.assertEqual(self.managed(), self.before)

    def test_concurrent_change_during_fetch_is_not_overwritten(self):
        def fetched(*_):
            (self.root / "bundle.tar.gz").write_bytes(b"concurrent archive")
            return self.candidate

        with (
            patch.object(subject, "fetch_runtime", side_effect=fetched),
            patch.object(subject.updates, "transaction", side_effect=self.transaction),
            patch.object(subject, "verify") as execute,
        ):
            with self.assertRaisesRegex(ValueError, "changed during preparation"):
                subject.apply(self.root, self.opts, self.now)
            execute.assert_not_called()
        self.assertEqual(
            (self.root / "bundle.tar.gz").read_bytes(), b"concurrent archive"
        )
        for name in (
            "chainman.lock",
            "scripts/chainman.sh",
            "scripts/chainman-fetch.nix",
        ):
            self.assertEqual(self.managed()[name], self.before[name])

    def test_partial_publication_failure_restores_already_written_files(self):
        original = subject.tc.atomic_bytes

        def interrupted(path, body, mode=0o600):
            original(path, body, mode)
            if path == self.root / "bundle.tar.gz" and body == self.body:
                raise OSError("publication interrupted")

        with (
            patch.object(subject.tc, "atomic_bytes", side_effect=interrupted),
            self.assertRaisesRegex(OSError, "publication interrupted"),
        ):
            self.run_apply(
                lambda *_: self.fail("verification started after publication failed")
            )
        self.assertEqual(self.managed(), self.before)

    def test_resolver_failure_restores_managed_pin(self):
        self.opts.only_chainman = False
        with (
            patch.object(
                subject.tc, "managed_run", side_effect=ValueError("resolver failed")
            ),
            self.assertRaisesRegex(ValueError, "resolver failed"),
        ):
            self.run_apply(
                lambda *_: self.fail("verification started after resolver failed")
            )
        self.assertEqual(self.managed(), self.before)

    @unittest.skipUnless(
        shutil.which("nix"), "real Nix is required for NAR verification"
    )
    def test_real_nix_fetch_verifies_downloaded_bytes_without_evaluating_candidate(
        self,
    ):
        nar_hash = subprocess.check_output(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "path",
                str(self.candidate),
            ],
            text=True,
        ).strip()
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            archive.add(self.candidate, arcname="runtime")
        lock = {
            key: self.metadata[key]
            for key in ("schema", "version", "revision", "url", "narHash")
        }
        lock["narHash"] = nar_hash
        actual = Path(__file__).resolve().parents[1]
        with patch.object(chainman, "RUNTIME", actual):
            runtime = subject.fetch_runtime(lock, output.getvalue())
            self.assertEqual((runtime / "VERSION").read_text().strip(), "2.0.0")
            lock["narHash"] = "sha256-" + "A" * 43 + "="
            with self.assertRaises(subprocess.CalledProcessError):
                subject.fetch_runtime(lock, output.getvalue())
        self.assertEqual(self.managed(), self.before)


if __name__ == "__main__":
    unittest.main()
