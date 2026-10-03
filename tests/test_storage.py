"""Destructive storage boundaries are exercised only inside disposable fixtures."""

import json
import os
import pty
import select
import signal
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from terminal_fixture import wait_terminal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import storage
import toolchain as tc


TERMINAL_BODY = """import errno, os, termios
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
while True:
    try:
        termios.tcsetattr(0, termios.TCSANOW, settings)
        break
    except termios.error as error:
        # Darwin returns EINTR after SIGTTOU stops a background ioctl.
        if error.args[0] != errno.EINTR:
            raise
while True:
    assert not termios.tcgetattr(0)[3] & termios.ECHO
    print('CHILD-READY', flush=True)
    if input() == 'finish':
        break
raise SystemExit(7)
"""


def terminal_process(argv, env, cwd):
    """Acquire the controlling PTY only after exec into a fresh interpreter."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                "import os,sys; os.login_tty(0); "
                "os.execve(sys.argv[1], sys.argv[1:], os.environ)",
                *argv,
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=cwd,
            env=env,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return process, master


def exercise_job_control(
    test, argv, env, cwd, stopped=lambda: None, *, cancel=False, background=False
):
    """A real interactive shell owns the job, including waiting bootstrap shells."""
    shell = shutil.which("bash")
    test.assertIsNotNone(shell)
    process, master = terminal_process(
        [shell, "--noprofile", "--norc", "-i"],
        dict(env, PS1="PROMPT> ", PS2=""),
        cwd,
    )
    pid = process.pid
    output = b""
    timeout = 180

    def expect(marker):
        nonlocal output
        deadline = time.monotonic() + timeout
        while marker not in output:
            test.assertLess(time.monotonic(), deadline, output.decode(errors="replace"))
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        before, output = output.split(marker, 1)
        return before

    def send(value):
        os.write(master, value)

    try:
        expect(b"PROMPT> ")
        body = shlex.join(argv) + "; printf 'RESULT:%s\\n' \"$?\""
        send(
            (
                shlex.join([shell, "-c", body]) + (" &\n" if background else "\n")
            ).encode()
        )
        if background:
            expect(b"PROMPT> ")
            deadline = time.monotonic() + timeout
            while True:
                send(b"jobs\n")
                if b"Stopped" in expect(b"PROMPT> "):
                    break
                test.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            test.assertEqual(os.tcgetpgrp(master), pid)
            stopped()
            send(b"fg\n")
        expect(b"CHILD-READY\r\n")
        timeout = 10
        for _ in range(2):
            send(b"\x1a")
            expect(b"PROMPT> ")
            test.assertEqual(os.tcgetpgrp(master), pid)
            stopped()
            send(b"bg\nsleep 0.2; jobs\n")
            expect(b"PROMPT> ")
            background = expect(b"PROMPT> ")
            test.assertIn(b"Stopped", background)
            test.assertEqual(os.tcgetpgrp(master), pid)
            stopped()
            send(b"fg\ncontinue\n")
            expect(b"CHILD-READY\r\n")
        send(b"\x03" if cancel else b"finish\n")
        expect(b"RESULT:130\r\n" if cancel else b"RESULT:7\r\n")
        expect(b"PROMPT> ")
        test.assertEqual(os.tcgetpgrp(master), pid)
    finally:
        # Only groups belonging to this disposable PTY session are addressed.
        try:
            foreground = os.tcgetpgrp(master)
        except OSError:
            foreground = 0
        if foreground > 0 and foreground != pid:
            try:
                os.killpg(foreground, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            try:
                send(b"kill -KILL $(jobs -p) 2>/dev/null; exit\n")
            except OSError:
                pass
            try:
                wait_terminal(process, master, 5)
            except subprocess.TimeoutExpired:
                process.kill()
                wait_terminal(process, master, 5)
        finally:
            os.close(master)


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
        child = """import os, signal, sys, termios
assert os.tcgetpgrp(0) == os.getpgrp()
settings = termios.tcgetattr(0)
settings[3] &= ~termios.ECHO
termios.tcsetattr(0, termios.TCSANOW, settings)
def interrupted(signum, frame):
    sys.stderr.write('x' * 262144)
    sys.stderr.flush()
    raise KeyboardInterrupt
signal.signal(signal.SIGINT, interrupted)
print('ready', flush=True)
input()
"""
        wrapper = (
            "import os,sys,termios; from pathlib import Path; "
            f"sys.path.insert(0,{str(tc.RUNTIME / 'scripts')!r}); import storage; "
            "before=termios.tcgetattr(0); "
            f"status=storage.runtime_child({[sys.executable, '-c', child]!r}, dict(os.environ)); "
            "Path(sys.argv[1]).write_text("
            "f'{status}:{os.tcgetpgrp(0)==os.getpgrp()}:{termios.tcgetattr(0)==before}')"
        )
        process, master = terminal_process(
            [sys.executable, "-c", wrapper, str(result)], dict(os.environ), self.root
        )
        try:
            output = b""
            deadline = time.monotonic() + 10
            while b"ready" not in output:
                self.assertLess(time.monotonic(), deadline, output)
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 4096)
            os.write(master, b"\x03")
            status, remaining_output = wait_terminal(process, master, 10)
            self.assertEqual(status, 0)
            self.assertGreater(len(remaining_output), 131072)
            self.assertTrue(result.exists())
            self.assertEqual(result.read_text(), "130:True:True")
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                wait_terminal(process, master, 5)
            finally:
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

    def test_terminal_launcher_avoids_forking_a_threaded_test_runner(self):
        wrapper = (
            f"import sys,os; sys.path.insert(0,{str(tc.RUNTIME / 'scripts')!r}); "
            "import storage; "
            f"sys.exit(storage.runtime_child({[sys.executable, '-c', TERMINAL_BODY]!r}, dict(os.environ)))"
        )
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait)
        worker.start()
        try:
            with (
                patch.object(
                    pty, "fork", side_effect=AssertionError("unsafe pty.fork")
                ),
                patch.object(
                    os, "forkpty", side_effect=AssertionError("unsafe forkpty")
                ),
            ):
                exercise_job_control(
                    self,
                    [sys.executable, "-c", wrapper],
                    dict(os.environ),
                    self.root,
                )
        finally:
            stop.set()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_runtime_job_control_keeps_leases_across_stop_and_resume(self):
        entry = self.populated()
        wrapper = (
            f"import sys,os; from pathlib import Path; sys.path.insert(0,{str(tc.RUNTIME / 'scripts')!r}); "
            "import storage\n"
            f"with storage.use(Path({str(self.pool)!r}), 'downloads', 'downloads'):\n"
            f" raise SystemExit(storage.runtime_child({[sys.executable, '-c', TERMINAL_BODY]!r}, dict(os.environ)))\n"
        )

        def stopped():
            rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
            self.assertTrue(rows[0]["active"])
            self.assertFalse(removed)

        exercise_job_control(
            self, [sys.executable, "-c", wrapper], dict(os.environ), self.root, stopped
        )
        self.assertTrue(storage.collect(self.pool, apply=True, all_idle=True)[1])
        self.assertFalse((entry / "data/cargo/registry").exists())

    def test_runtime_job_control_cancellation_after_resume(self):
        wrapper = (
            f"import sys,os; sys.path.insert(0,{str(tc.RUNTIME / 'scripts')!r}); "
            "import storage; "
            f"sys.exit(storage.runtime_child({[sys.executable, '-c', TERMINAL_BODY]!r}, dict(os.environ)))"
        )
        exercise_job_control(
            self,
            [sys.executable, "-c", wrapper],
            dict(os.environ),
            self.root,
            cancel=True,
        )

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

    def pub_fixture(self):
        with storage.use(self.pool, "downloads", "downloads") as entry:
            pub = entry / "data/pub"
            for relative in (
                "git/cache/mirror",
                "git/tool-revision/packages/tool",
                "git/obsolete-revision",
                "hosted/pub.dev/dependency-1.0.0",
                "hosted/pub.dev/obsolete-1.0.0",
                "hosted-hashes/pub.dev",
            ):
                path = pub / relative
                path.mkdir(parents=True, exist_ok=True)
                (path / "payload").write_bytes(b"x" * 128)
        return entry, pub

    def activate_pub(self, pub, *, absolute=False):
        config = pub / "global_packages/tool/.dart_tool/package_config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        dependencies = [
            pub / "git/tool-revision/packages/tool",
            pub / "hosted/pub.dev/dependency-1.0.0",
        ]
        tc.atomic_json(
            config,
            {
                "configVersion": 2,
                "packages": [
                    {
                        "name": f"package{index}",
                        "rootUri": path.as_uri()
                        if absolute
                        else os.path.relpath(path, config.parent) + "/",
                    }
                    for index, path in enumerate(dependencies)
                ],
            },
        )
        return config

    def test_pub_git_is_accounted_and_collected_by_budget_age_and_all(self):
        for options in (
            {"limit": 1},
            {"all_idle": True},
            {"now": time.time() + storage.AGE + 1},
        ):
            with self.subTest(options=options):
                entry, pub = self.pub_fixture()
                epoch = storage.receipt(entry)["epoch"]
                before = (entry / ".receipt.json").read_bytes()
                rows, removed = storage.collect(self.pool, **options)
                self.assertEqual(rows[0]["bytes"], 6 * 128)
                self.assertTrue(rows[0]["eligible"])
                self.assertFalse(removed)
                self.assertEqual((entry / ".receipt.json").read_bytes(), before)
                rows, removed = storage.collect(self.pool, apply=True, **options)
                self.assertIn(str(pub / "git"), removed)
                self.assertFalse(rows[0]["error"])
                self.assertNotEqual(storage.receipt(entry)["epoch"], epoch)

    def test_pub_active_lease_prevents_git_collection(self):
        entry, pub = self.pub_fixture()
        with storage.use(self.pool, "downloads", "downloads"):
            rows, removed = storage.collect(
                self.pool, apply=True, all_idle=True, limit=1
            )
            self.assertTrue(rows[0]["active"])
            self.assertFalse(removed)
            self.assertTrue((pub / "git/obsolete-revision").exists())

    def test_pub_budget_counts_protected_dependencies_and_stops_after_enough_eviction(
        self,
    ):
        entry, pub = self.pub_fixture()
        self.activate_pub(pub)
        for age, relative in enumerate(
            ("git/cache", "git/obsolete-revision", "hosted/pub.dev/obsolete-1.0.0"), 1
        ):
            os.utime(pub / relative, (age, age))
        rows, removed = storage.collect(self.pool, apply=True, limit=5 * 128)
        self.assertEqual(rows[0]["bytes"], 6 * 128)
        self.assertEqual(rows[0]["protected_bytes"], 3 * 128)
        self.assertEqual(removed, [str(pub / "git/cache")])
        self.assertTrue((pub / "git/tool-revision/packages/tool/payload").exists())
        self.assertTrue((pub / "git/obsolete-revision/payload").exists())

    def test_pub_global_dependencies_survive_while_unreferenced_payloads_expire(self):
        for absolute in (False, True):
            with self.subTest(absolute=absolute):
                entry, pub = self.pub_fixture()
                config = self.activate_pub(pub, absolute=absolute)
                before = config.read_bytes()
                (pub / "bin").mkdir(exist_ok=True)
                (pub / "bin/tool").write_text("installed tool")
                (pub / "credentials.json").write_text("fixture credentials")
                rows, removed = storage.collect(
                    self.pool, apply=True, all_idle=True, limit=1
                )
                self.assertFalse(rows[0]["error"])
                self.assertEqual(rows[0]["bytes"], 6 * 128)
                self.assertEqual(rows[0]["protected_bytes"], 3 * 128)
                self.assertIn("globally activated", rows[0]["reason"])
                for relative in (
                    "git/cache",
                    "git/obsolete-revision",
                    "hosted/pub.dev/obsolete-1.0.0",
                ):
                    self.assertIn(str(pub / relative), removed)
                    self.assertFalse((pub / relative).exists())
                for relative in (
                    "git/tool-revision/packages/tool/payload",
                    "hosted/pub.dev/dependency-1.0.0/payload",
                    "hosted-hashes/pub.dev/payload",
                    "bin/tool",
                    "credentials.json",
                ):
                    self.assertTrue((pub / relative).is_file())
                self.assertEqual(config.read_bytes(), before)
                epoch = storage.receipt(entry)["epoch"]
                rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
                self.assertFalse(removed)
                self.assertFalse(rows[0]["eligible"])
                self.assertEqual(storage.receipt(entry)["epoch"], epoch)

    def test_pub_legacy_activation_link_protects_new_home_dependencies(self):
        legacy = self.pool.parent / "pub/global_packages"
        legacy.mkdir(parents=True)
        entry, pub = self.pub_fixture()
        self.assertTrue((pub / "global_packages").is_symlink())
        self.activate_pub(pub, absolute=True)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertFalse(rows[0]["error"])
        self.assertEqual(rows[0]["protected_bytes"], 3 * 128)
        self.assertTrue(removed)
        self.assertTrue((legacy / "tool/.dart_tool/package_config.json").exists())

    def test_pub_relative_legacy_activations_preserve_both_possible_locations(self):
        legacy = self.pool.parent / "pub/global_packages"
        legacy.mkdir(parents=True)
        for relative in (
            "git/tool-revision/packages/tool",
            "hosted/pub.dev/dependency-1.0.0",
        ):
            (legacy.parent / relative).mkdir(parents=True)
        entry, pub = self.pub_fixture()
        self.activate_pub(pub)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertEqual(rows[0]["protected_bytes"], 3 * 128)
        self.assertIn(str(pub / "git/obsolete-revision"), removed)
        self.assertTrue((pub / "git/tool-revision/packages/tool/payload").exists())
        self.assertTrue((legacy.parent / "git/tool-revision/packages/tool").exists())

    def test_pub_external_package_references_never_authorize_external_removal(self):
        entry, pub = self.pub_fixture()
        external = self.root / "external package"
        external.mkdir()
        (external / "payload").write_text("keep")
        config = self.activate_pub(pub)
        saved = json.loads(config.read_bytes())
        saved["packages"].append({"name": "external", "rootUri": external.as_uri()})
        tc.atomic_json(config, saved)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertFalse(rows[0]["error"])
        self.assertEqual(rows[0]["protected_bytes"], 3 * 128)
        self.assertTrue(removed)
        self.assertEqual((external / "payload").read_text(), "keep")

    def test_pub_unknown_global_directory_link_preserves_pub_payloads(self):
        entry, pub = self.pub_fixture()
        external = self.root / "external-activations"
        external.mkdir()
        (pub / "global_packages").symlink_to(external, target_is_directory=True)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertFalse(removed)
        self.assertEqual(rows[0]["protected_bytes"], 6 * 128)
        self.assertIn("unrecognized global_packages link", rows[0]["reason"])

    def test_pub_uncertain_activation_metadata_preserves_only_pub_families(self):
        entry, pub = self.pub_fixture()
        config = self.activate_pub(pub)
        corruptions = (
            None,
            "not json",
            '{"configVersion":99,"packages":[]}',
            '{"configVersion":2,"packages":[{"rootUri":"https://example.invalid/package"}]}',
            '{"configVersion":2,"packages":[{"rootUri":"file:///missing-fixture-package"}]}',
        )
        for body in corruptions:
            with self.subTest(body=body):
                if body is None:
                    config.unlink()
                else:
                    config.write_text(body)
                other = entry / "data/uv"
                other.mkdir()
                (other / "payload").write_bytes(b"disposable")
                rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
                self.assertEqual(removed, [str(other)])
                self.assertEqual(rows[0]["protected_bytes"], 6 * 128)
                self.assertIn("Pub payloads preserved", rows[0]["reason"])
                self.assertTrue((pub / "git/obsolete-revision/payload").exists())

    def test_pub_symlink_metadata_is_preserved_without_following_unknown_links(self):
        entry, pub = self.pub_fixture()
        config = self.activate_pub(pub)
        external = self.root / "outside.json"
        external.write_bytes(config.read_bytes())
        config.unlink()
        config.symlink_to(external)
        rows, removed = storage.collect(self.pool, apply=True, all_idle=True)
        self.assertFalse(removed)
        self.assertEqual(rows[0]["protected_bytes"], 6 * 128)
        self.assertIn("Pub payloads preserved", rows[0]["reason"])
        self.assertTrue(external.exists())

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
