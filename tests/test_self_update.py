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
from urllib.error import HTTPError, URLError

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
        self.release_date = "2026-01-01T00:00:00Z"
        self.asset_changes = {}
        for target, kwargs in (
            ("chainman.RUNTIME", {"new": self.previous}),
            ("registry.github_releases", {"return_value": [release]}),
            ("registry.github_commit", {"return_value": "b" * 40}),
            ("source_updates.commit_time", {"return_value": release.published}),
            ("registry.data", {"side_effect": self.release_response}),
            ("registry.fetch", {"side_effect": self.fetch_asset}),
        ):
            context = patch(target, **kwargs)
            context.start()
            self.addCleanup(context.stop)
        self.opts = argparse.Namespace(
            extra=[], only_chainman=True, skip_chainman=False, no_commit=True
        )
        self.before = self.managed()

    def release_response(self, url):
        self.assertEqual(
            url,
            "https://api.github.com/repos/chainmandev/chainman/releases/tags/v2.0.0",
        )
        assets = []
        for number, name, body in (
            (1, "chainman-release.json", json.dumps(self.metadata).encode()),
            (2, "chainman-2.0.0.tar.gz", self.body),
        ):
            assets.append(
                {
                    "id": number,
                    "name": name,
                    "state": "uploaded",
                    "size": len(body),
                    "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "created_at": self.release_date,
                    "updated_at": self.release_date,
                    **self.asset_changes.get(number, {}),
                }
            )
        return {
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": self.release_date,
            "assets": assets,
        }

    def fetch_asset(self, url, **kwargs):
        self.assertEqual(kwargs, {"accept": "application/octet-stream"})
        if url.endswith("/releases/assets/1"):
            return json.dumps(self.metadata).encode(), {}
        self.assertTrue(url.endswith("/releases/assets/2"))
        return self.body, {}

    def test_old_release_cannot_admit_young_or_undated_replacement_assets(self):
        for number in (1, 2):
            for changes in (
                {"updated_at": "2026-09-06T00:00:00Z"},
                {"created_at": "2026-09-06T00:00:00Z"},
                {"updated_at": None},
                {"digest": None},
                {"digest": "sha256:" + "0" * 64},
                {"size": 1},
            ):
                with self.subTest(asset=number, changes=changes):
                    self.asset_changes = {number: changes}
                    with (
                        patch.object(
                            subject,
                            "fetch_runtime",
                            side_effect=AssertionError("candidate evaluated"),
                        ) as evaluate,
                        patch.object(
                            subject.updates, "transaction", side_effect=self.transaction
                        ),
                        patch.object(
                            subject,
                            "verify",
                            side_effect=AssertionError("candidate executed"),
                        ),
                    ):
                        with self.assertRaises(ValueError):
                            subject.apply(self.root, self.opts, self.now)
                        evaluate.assert_not_called()
                    self.assertEqual(self.managed(), self.before)

    def test_old_release_cannot_admit_young_commit_or_moving_tag(self):
        with patch("source_updates.commit_time", return_value=self.now):
            with self.assertRaisesRegex(ValueError, "No eligible"):
                self.run_apply(lambda *_: self.fail("candidate executed"))
        with patch("registry.github_commit", side_effect=["b" * 40, "c" * 40]):
            with self.assertRaisesRegex(ValueError, "tag changed"):
                self.run_apply(lambda *_: self.fail("candidate executed"))
        self.assertEqual(self.managed(), self.before)

    def test_release_asset_age_boundary_is_inclusive(self):
        self.asset_changes = {2: {"updated_at": "2026-08-08T00:00:00Z"}}
        self.assertEqual(self.run_apply(lambda *_: None), {"verification": "passed"})

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


class FreshReleaseTagTests(unittest.TestCase):
    """Exercise the real registry cache; replace only the HTTP transport."""

    def setUp(self):
        registry.fetch.cache_clear()
        self.addCleanup(registry.fetch.cache_clear)
        temporary = tempfile.TemporaryDirectory(prefix="fresh tag control ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        self.lock = self.root / "chainman.lock"
        self.lock.write_text('{"schema":1,"version":"1.0.0"}\n')
        self.before = self.lock.read_bytes()
        self.api = "https://api.github.com/repos/chainmandev/chainman"
        self.ref = self.api + "/git/ref/tags/v2.0.0"
        self.original = "a" * 40
        self.changed = "b" * 40
        self.date = "2026-01-01T00:00:00Z"
        self.selected = registry.Release("v2.0.0", registry.timestamp(self.date))
        self.now = datetime(2026, 9, 8, tzinfo=timezone.utc)
        self.archive = b"exact dated fixture asset; never execute"
        self.metadata = json.dumps(
            {
                "schema": 1,
                "version": "2.0.0",
                "revision": self.original,
                "url": "https://github.com/chainmandev/chainman/releases/download/v2.0.0/chainman-2.0.0.tar.gz",
                "narHash": "sha256-" + "A" * 43 + "=",
                "archive_sha256": hashlib.sha256(self.archive).hexdigest(),
            }
        ).encode()
        self.release = {
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": self.date,
            "assets": [
                {
                    "id": number,
                    "name": name,
                    "state": "uploaded",
                    "size": len(body),
                    "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "created_at": self.date,
                    "updated_at": self.date,
                }
                for number, name, body in (
                    (1, "chainman-release.json", self.metadata),
                    (2, "chainman-2.0.0.tar.gz", self.archive),
                )
            ],
        }
        self.requests = []
        self.downloaded = False
        self.annotated = False
        self.move = False
        self.failure = None

    def transport(self, request, timeout):
        self.assertEqual(timeout, 30)
        url = request.full_url
        self.requests.append((url, request.get_header("Cache-control")))
        moved = self.move and self.downloaded
        if url == self.ref:
            if self.downloaded and self.failure == "http":
                error = HTTPError(url, 403, "denied", {}, io.BytesIO(b"denied"))
                self.addCleanup(error.close)
                raise error
            if self.downloaded and self.failure == "transport":
                raise URLError("unavailable")
            value = {
                "object": {
                    "type": "tag" if self.annotated else "commit",
                    "sha": ("e" if moved else "c") * 40
                    if self.annotated
                    else self.changed
                    if moved
                    else self.original,
                }
            }
        elif url in {self.api + "/git/tags/" + digit * 40 for digit in "ce"}:
            value = {
                "object": {
                    "type": "tag",
                    "sha": ("f" if url.endswith("e" * 40) else "d") * 40,
                }
            }
        elif url in {self.api + "/git/tags/" + digit * 40 for digit in "df"}:
            value = {
                "object": {
                    "type": "commit",
                    "sha": self.changed if url.endswith("f" * 40) else self.original,
                }
            }
        elif url == self.api + "/releases/tags/v2.0.0":
            value = self.release
        elif url == self.api + "/commits/" + self.original:
            value = {
                "sha": self.original,
                "commit": {"committer": {"date": self.date}},
            }
        elif url == self.api + "/releases/assets/1":
            value = self.metadata
        elif url == self.api + "/releases/assets/2":
            value = self.archive
            self.downloaded = True
        else:
            self.fail(f"Unexpected HTTP request: {url}")
        response = io.BytesIO(
            value if isinstance(value, bytes) else json.dumps(value).encode()
        )
        response.headers = {"Content-Type": "application/json"}
        return response

    def test_unchanged_lightweight_tag_is_read_again_without_losing_snapshot_cache(
        self,
    ):
        with patch.object(registry, "urlopen", side_effect=self.transport):
            result = subject.release_assets(self.selected, {}, self.now)
            self.assertEqual(result[2], self.original)
            self.assertEqual(
                [header for url, header in self.requests if url == self.ref],
                [None, "no-cache"],
            )
            count = len(self.requests)
            self.assertEqual(
                registry.github_commit("chainmandev/chainman", "v2.0.0"),
                self.original,
            )
            self.assertEqual(len(self.requests), count)
            registry.github_commit("chainmandev/chainman", "v2.0.0", fresh=True)
            self.assertEqual(len(self.requests), count + 1)

    def test_unchanged_annotated_tag_refreshes_every_hop(self):
        self.annotated = True
        with patch.object(registry, "urlopen", side_effect=self.transport):
            result = subject.release_assets(self.selected, {}, self.now)
        self.assertEqual(result[2], self.original)
        for url in [
            self.ref,
            *(self.api + "/git/tags/" + digit * 40 for digit in "cd"),
        ]:
            self.assertEqual(
                [header for requested, header in self.requests if requested == url],
                [None, "no-cache"],
            )

    def assert_stops_before_evaluation(self, expected, message):
        with (
            patch.object(registry, "urlopen", side_effect=self.transport),
            patch.object(registry, "github_releases", return_value=[self.selected]),
            patch.object(subject, "fetch_runtime") as evaluate,
            patch.object(registry.time, "sleep"),
            self.assertRaisesRegex(expected, message),
        ):
            try:
                subject.runtime_candidate(self.root, {}, self.now)
            finally:
                evaluate.assert_not_called()
                self.assertEqual(self.lock.read_bytes(), self.before)

    def test_moving_lightweight_tag_stops_before_candidate_evaluation(self):
        self.move = True
        self.assert_stops_before_evaluation(ValueError, "tag changed")
        self.assertEqual(sum(url == self.ref for url, _ in self.requests), 2)

    def test_moving_annotated_tag_stops_before_candidate_evaluation(self):
        self.annotated = self.move = True
        self.assert_stops_before_evaluation(ValueError, "tag changed")
        for digit in "ef":
            self.assertIn(
                (self.api + "/git/tags/" + digit * 40, "no-cache"), self.requests
            )

    def test_fresh_http_error_is_not_hidden_by_the_cached_tag(self):
        self.failure = "http"
        self.assert_stops_before_evaluation(registry.RegistryHTTPError, "HTTP 403")

    def test_fresh_transport_error_preserves_the_bounded_failure(self):
        self.failure = "transport"
        self.assert_stops_before_evaluation(ValueError, "Registry unavailable")
        self.assertEqual(sum(url == self.ref for url, _ in self.requests), 4)


if __name__ == "__main__":
    unittest.main()
