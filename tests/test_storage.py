"""Destructive storage boundaries are exercised only inside disposable fixtures."""

import json
import os
import pty
import select
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import storage
import toolchain as tc


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chainman-storage-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = patch.dict(
            os.environ,
            {
                "XDG_CACHE_HOME": str(self.root),
                "TOOLCHAIN_MODE": "host-nix",
                "CHAINMAN_MODE": "host-nix",
                storage.PATHS: "[]",
                storage.FDS: "[]",
                "TOOLCHAIN_DOWNLOAD_CACHE": str(self.root / "nix-just-downloads"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pool = self.root / "nix-just-downloads" / storage.POOL

    def populated(self):
        with storage.use(self.pool, "downloads", "downloads") as entry:
            payload = entry / "data/cargo/registry/cache"
            payload.mkdir(parents=True)
            (payload / "package").write_bytes(b"x" * 128)
            (entry / "data/cargo/credentials.toml").write_text("fixture credential")
            (entry / "data/cargo/config.toml").write_text("fixture config")
            installed = entry / "data/cargo/bin/tool"
            installed.parent.mkdir()
            installed.write_text("fixture installed tool")
        return entry

    def test_budget_evicts_payload_but_preserves_home_and_changes_epoch(self):
        entry = self.populated()
        before = storage.receipt(entry)["epoch"]
        rows, removed = storage.collect(self.pool, apply=True, limit=1)
        self.assertTrue(rows[0]["eligible"])
        self.assertEqual(removed, [str(entry / "data/cargo/registry")])
        self.assertFalse((entry / "data/cargo/registry").exists())
        for name in ("credentials.toml", "config.toml", "bin/tool"):
            self.assertTrue((entry / "data/cargo" / name).exists())
        self.assertNotEqual(before, storage.receipt(entry)["epoch"])

    def test_all_retains_running_child_after_parent_releases_lease(self):
        entry = self.populated()
        child = None
        try:
            with storage.use(self.pool, "downloads", "downloads"):
                _, fds = storage.inherited()
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"], pass_fds=fds
                )
            rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
            self.assertTrue(rows[0]["active"])
            self.assertEqual(removed, [])
            self.assertTrue((entry / "data/cargo/registry").exists())
        finally:
            if child is not None:
                child.terminate()
                child.wait(timeout=10)
        self.assertTrue(storage.collect(self.pool, apply=True, all_idle=True)[1])

    def test_nested_use_retains_same_lease_and_epoch(self):
        with storage.use(self.pool, "downloads", "downloads"):
            before = storage.inherited()
            epoch = storage.download_epoch()
            with storage.use(self.pool, "downloads", "downloads"):
                self.assertEqual(storage.inherited(), before)
                self.assertEqual(storage.download_epoch(), epoch)
                self.assertEqual(
                    storage.collect(self.pool, apply=True, all_idle=True)[1], []
                )

    def test_age_and_status_are_non_destructive(self):
        entry = self.populated()
        saved = storage.receipt(entry)
        now = saved["touched"] + storage.AGE + 1
        before = (entry / ".receipt.json").read_bytes()
        rows, removed = storage.collect(self.pool, now=now)
        self.assertTrue(rows[0]["eligible"])
        self.assertFalse(removed)
        self.assertEqual((entry / ".receipt.json").read_bytes(), before)
        self.assertTrue(storage.collect(self.pool, apply=True, now=now)[1])

    def test_budget_stops_after_oldest_cache_family(self):
        entry = self.populated()
        older = entry / "data/cargo/registry"
        newer = entry / "data/go-mod"
        newer.mkdir()
        (newer / "package").write_bytes(b"y" * 128)
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))
        _, removed = storage.collect(self.pool, apply=True, limit=128)
        self.assertEqual(removed, [str(older)])
        self.assertTrue(newer.exists())

    def test_readonly_package_directories_are_removed_without_changing_links(self):
        entry = self.populated()
        package = entry / "data/go-mod/example@v1"
        package.mkdir(parents=True)
        (package / "module.go").write_text("package example")
        outside = self.root / "installed-module.go"
        os.link(package / "module.go", outside)
        outside.chmod(0o444)
        external = self.root / "external"
        external.mkdir()
        (package / "external").symlink_to(external, target_is_directory=True)
        external.chmod(0o555)
        package.chmod(0o555)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertFalse(rows[0]["error"])
        self.assertIn(str(package.parent), removed)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o444)
        self.assertEqual(external.stat().st_mode & 0o777, 0o555)

    def test_runtime_collection_rejects_directories_in_place_of_gc_roots(self):
        pool = self.root / "runtimes" / storage.POOL
        with storage.use(pool, "a" * 40, "runtime") as entry:
            (entry / "source").mkdir()
            (entry / "source/keep").write_text("not a GC root")
        rows, removed = storage.collect(pool, apply=True, all_idle=True)
        self.assertTrue(rows[0]["error"])
        self.assertFalse(removed)
        self.assertTrue((entry / "source/keep").exists())

    def test_runtime_cancellation_reaches_child_and_preserves_signal_status(self):
        ready, exited = self.root / "ready", self.root / "exited"
        body = (
            "import signal,time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, lambda *_: (Path({str(exited)!r}).touch(), exit(0))); "
            f"Path({str(ready)!r}).touch(); time.sleep(60)"
        )
        wrapper = (
            f"import sys,os; sys.path.insert(0,{str(tc.RUNTIME / 'scripts')!r}); "
            f"import storage; sys.exit(storage.runtime_child({[sys.executable, '-c', body]!r},dict(os.environ)))"
        )
        parent = subprocess.Popen([sys.executable, "-c", wrapper])
        try:
            deadline = time.monotonic() + 10
            while (
                not ready.exists()
                and parent.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            parent.send_signal(signal.SIGTERM)
            self.assertEqual(parent.wait(timeout=10), 143)
            self.assertTrue(exited.exists())
        finally:
            if parent.poll() is None:
                parent.terminate()
                parent.wait(timeout=10)

    def test_runtime_handoff_preserves_caller_descriptor(self):
        read_fd, write_fd = os.pipe()
        try:
            os.set_inheritable(write_fd, True)
            with storage.use(self.pool, "downloads", "downloads"):
                result = storage.runtime_child(
                    [sys.executable, "-c", f"import os; os.write({write_fd}, b'ok')"],
                    dict(os.environ),
                )
            self.assertEqual(result, 0)
            self.assertEqual(os.read(read_fd, 2), b"ok")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_runtime_handoff_restores_terminal_after_foreground_interrupt(self):
        result = self.root / "terminal-result"
        pid, master = pty.fork()
        if pid == 0:
            try:
                status = storage.runtime_child(
                    [
                        sys.executable,
                        "-c",
                        "import os; assert os.tcgetpgrp(0)==os.getpgrp(); "
                        "print('ready', flush=True); input()",
                    ],
                    dict(os.environ),
                )
                result.write_text(f"{status}:{os.tcgetpgrp(0) == os.getpgrp()}")
                os._exit(0)
            except BaseException:
                os._exit(1)
        try:
            output = b""
            deadline = time.monotonic() + 10
            while b"ready" not in output:
                self.assertLess(time.monotonic(), deadline, output)
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 4096)
            os.write(master, b"\x03")
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(result.exists())
            self.assertEqual(result.read_text(), "130:True")
            self.assertEqual(os.waitpid(pid, 0)[1], 0)
        finally:
            if not result.exists():
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)

    def test_runtime_handoff_requires_a_matching_inherited_lease(self):
        base = self.root / "runtimes"
        revision, source = "a" * 40, "/nix/store/fixture-source"
        args = [str(base), revision, source]
        with storage.use(base / storage.POOL, revision, "runtime") as entry:
            (entry / "source").symlink_to(source)
            self.assertEqual(storage.runtime_check(args), 0)
            with self.assertRaises(ValueError):
                storage.runtime_check([*args[:2], "/nix/store/other-source"])
            with patch.dict(os.environ, {storage.FDS: "[999999]"}):
                with self.assertRaises(OSError):
                    storage.runtime_check(args)
        self.assertEqual(storage.runtime_check(args), 3)

    def test_corrupt_and_unknown_receipts_are_preserved(self):
        entry = self.populated()
        tc.atomic_json(entry / ".receipt.json", {"schema": 99})
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertTrue(rows[0]["error"])
        self.assertFalse(removed)
        self.assertTrue((entry / "data/cargo/registry").exists())

    def test_symlink_payload_and_ancestor_cannot_escape(self):
        entry = self.populated()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("keep")
        (entry / "data/pnpm").symlink_to(outside, target_is_directory=True)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertTrue(rows[0]["error"])
        self.assertFalse(removed)
        self.assertTrue((outside / "keep").exists())
        linked = self.root / "linked"
        linked.symlink_to(self.pool, target_is_directory=True)
        with self.assertRaises(ValueError):
            storage.collect(linked, apply=True, all_idle=True)

    def test_partial_removal_invalidates_setup_and_reports_actual_paths(self):
        entry = self.populated()
        extra = entry / "data/go-mod"
        extra.mkdir()
        (extra / "module").write_text("module")
        epoch = storage.receipt(entry)["epoch"]
        remove = storage.shutil.rmtree

        def fail_second(path):
            if path == extra:
                raise PermissionError("fixture refusal")
            remove(path)

        with patch.object(storage.shutil, "rmtree", side_effect=fail_second):
            rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertTrue(rows[0]["error"])
        self.assertEqual(removed, [str(entry / "data/cargo/registry")])
        self.assertNotEqual(storage.receipt(entry)["epoch"], epoch)
        self.assertTrue(extra.exists())

    def test_legacy_and_custom_cache_paths_are_not_collected(self):
        legacy = self.pool.parent / "cargo/registry"
        legacy.mkdir(parents=True)
        (legacy / "keep").write_text("legacy")
        self.populated()
        storage.collect(self.pool, apply=True, all_idle=True)
        self.assertTrue((legacy / "keep").exists())
        with patch.dict(
            os.environ, {"TOOLCHAIN_DOWNLOAD_CACHE": str(self.root / "custom")}
        ):
            self.assertFalse(storage.managed_downloads())
            with storage.downloads():
                self.assertFalse((self.root / "custom").exists())

    def test_initial_migration_preserves_configuration_and_installed_tools(self):
        legacy = self.pool.parent / "cargo"
        (legacy / "bin").mkdir(parents=True)
        (legacy / "config.toml").write_text("fixture configuration")
        (legacy / "credentials.toml").write_text("fixture credential")
        (legacy / "bin/tool").write_text("fixture tool")
        with storage.use(self.pool, "downloads", "downloads") as entry:
            home = entry / "data/cargo"
            self.assertEqual(
                (home / "config.toml").read_text(), "fixture configuration"
            )
            self.assertEqual((home / "credentials.toml").stat().st_mode & 0o777, 0o600)
            self.assertEqual((home / "bin/tool").read_text(), "fixture tool")
        storage.collect(self.pool, apply=True, all_idle=True)
        self.assertTrue((legacy / "credentials.toml").exists())
        self.assertTrue((home / "bin/tool").exists())

    def test_runtime_collection_unlinks_owned_roots_only(self):
        pool = self.root / "runtimes" / storage.POOL
        with storage.use(pool, "a" * 40, "runtime") as entry:
            (entry / "source").symlink_to("/nix/store/fixture-source")
            (entry / "bootstrap-1-link").symlink_to("/nix/store/fixture-bootstrap")
            (entry / "bootstrap").symlink_to("bootstrap-1-link")
            (entry / "unrecognized").write_text("keep")
            self.assertFalse(storage.collect(pool, apply=True, all_idle=True)[1])
        rows, removed = storage.collect(pool, apply=True, all_idle=True)
        self.assertFalse(rows[0]["error"])
        self.assertEqual(len(removed), 3)
        self.assertTrue((entry / "unrecognized").exists())
        self.assertTrue((entry / ".lease").exists())

    def test_environment_initialization_creates_a_recognized_private_pool(self):
        (self.root / "toolchain.toml").write_text('schema=1\nmodules=["core"]\n')
        env = tc.environment(self.root)
        entry = self.pool / "downloads"
        self.assertEqual(storage.receipt(entry)["kind"], "downloads")
        self.assertEqual(self.pool.stat().st_mode & 0o777, 0o700)
        with storage.downloads():
            self.assertEqual(env["CARGO_HOME"], str(entry / "data/cargo"))

    def test_retired_runtime_metadata_can_be_recreated(self):
        pool = self.root / "runtimes" / storage.POOL
        with storage.use(pool, "a" * 40, "runtime") as entry:
            before = storage.receipt(entry)["epoch"]
        storage.collect(pool, apply=True, all_idle=True)
        self.assertFalse(entry.exists())
        with storage.use(pool, "a" * 40, "runtime"):
            self.assertNotEqual(before, storage.receipt(entry)["epoch"])

    def test_missing_or_reused_inherited_descriptor_is_rejected(self):
        entry = self.populated()
        with patch.dict(
            os.environ,
            {storage.PATHS: json.dumps([str(entry)]), storage.FDS: "[999999]"},
        ):
            with self.assertRaises(OSError):
                storage.inherited()


if __name__ == "__main__":
    unittest.main()
