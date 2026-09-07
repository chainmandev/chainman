"""Managed child lifetime and disposal boundaries protect active build outputs."""

from contextlib import chdir
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import toolchain


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # Each fixture is an independent project. Its raw subprocesses must not
        # advertise the outer test runner's FD without inheriting that descriptor.
        fixture_env = dict(os.environ)
        fixture_env.pop("TOOLCHAIN_LOCK_FD", None)
        isolated = patch.dict(os.environ, fixture_env, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="toolchain runtime ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "toolchain.toml").write_text(
            'schema=1\nmodules=["core"]\n[cache]\nbuild_limit_gib=0\nstale_hours=0\n'
        )

    def cache_fixture(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        enter = scripts / "enter.sh"
        enter.write_text('#!/bin/sh\nshift\nexec "$@"\n')
        enter.chmod(0o755)
        server = scripts / "sccache"
        server.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent("""\
                import os, socket, sys, time
                from pathlib import Path
                endpoint = os.environ["SCCACHE_SERVER_UDS"]
                if "--stop-server" in sys.argv:
                    if os.environ.get("FAIL_STOP"):
                        sys.exit(42)
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(endpoint)
                        client.sendall(b"stop")
                    if os.environ.get("REPLACE_SOCKET"):
                        deadline = time.monotonic() + 5
                        while not Path("server-exited").exists():
                            if time.monotonic() > deadline:
                                sys.exit(44)
                            time.sleep(0.01)
                        Path(endpoint).unlink()
                        Path(endpoint).write_bytes(b"replacement")
                else:
                    os.fstat(int(os.environ["TOOLCHAIN_LOCK_FD"]))
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(endpoint)
                        listener.listen()
                        while True:
                            connection, _ = listener.accept()
                            with connection:
                                if connection.recv(16) == b"stop":
                                    break
                    Path("server-exited").write_text("yes")
                """)
        )
        server.chmod(0o755)
        env = toolchain.environment(self.root)
        env["PATH"] = str(scripts) + os.pathsep + env["PATH"]
        return env

    def test_owned_cache_exits_and_releases_lock_after_success_and_failure(self):
        env = self.cache_fixture()
        for status in (0, 23):
            with self.subTest(status=status):
                spec = {
                    "name": "rust",
                    "profile": "rust",
                    "directory": ".",
                    "commands": {
                        "verify": [
                            [sys.executable, "-c", f"import sys; sys.exit({status})"]
                        ]
                    },
                }
                with toolchain.operation(self.root):
                    if status:
                        with self.assertRaises(subprocess.CalledProcessError) as raised:
                            toolchain.run_commands(spec, "verify", env, self.root)
                        self.assertEqual(raised.exception.returncode, status)
                    else:
                        toolchain.run_commands(spec, "verify", env, self.root)
                with toolchain.operation(self.root):
                    self.assertEqual((self.root / "server-exited").read_text(), "yes")
                self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())
                (self.root / "server-exited").unlink()

    def test_cache_refuses_existing_endpoint_without_mutating_it(self):
        env = self.cache_fixture()
        endpoint = Path(env["SCCACHE_SERVER_UDS"])
        endpoint.write_bytes(b"unowned")
        self.addCleanup(endpoint.unlink)
        with toolchain.operation(self.root):
            with self.assertRaisesRegex(ValueError, "already exists"):
                with toolchain.compiler_cache("rust", env, self.root):
                    self.fail("Started work at an unowned endpoint")
        self.assertEqual(endpoint.read_bytes(), b"unowned")

    def test_cache_stop_failure_preserves_command_error_and_active_lock(self):
        env = self.cache_fixture()
        env["FAIL_STOP"] = "1"
        original_popen = subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        spec = {
            "name": "rust",
            "profile": "rust",
            "directory": ".",
            "commands": {
                "verify": [[sys.executable, "-c", "import sys; sys.exit(23)"]]
            },
        }
        try:
            with patch.object(subprocess, "Popen", side_effect=capture):
                with toolchain.operation(self.root):
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        toolchain.run_commands(spec, "verify", env, self.root)
            self.assertEqual(raised.exception.returncode, 23)
            self.assertIn("cleanup also failed", raised.exception.__notes__[0])
            with self.assertRaisesRegex(ValueError, "active"):
                with toolchain.operation(self.root):
                    self.fail("Failed cleanup released a live server's lock")
        finally:
            env.pop("FAIL_STOP")
            subprocess.run(
                [str(self.root / "scripts/sccache"), "--stop-server"],
                env=env,
                check=True,
            )
            processes[0].wait(timeout=5)
            Path(env["SCCACHE_SERVER_UDS"]).unlink()
        with toolchain.operation(self.root):
            self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())

    def test_cache_preserves_endpoint_replaced_during_shutdown(self):
        env = self.cache_fixture()
        env["REPLACE_SOCKET"] = "1"
        endpoint = Path(env["SCCACHE_SERVER_UDS"])
        self.addCleanup(lambda: endpoint.unlink(missing_ok=True))
        with toolchain.operation(self.root):
            with self.assertRaisesRegex(ValueError, "changed"):
                with toolchain.compiler_cache("rust", env, self.root):
                    pass
        with toolchain.operation(self.root):
            self.assertEqual(endpoint.read_bytes(), b"replacement")

    def test_relative_download_cache_overrides_fail_before_creation(self):
        for overrides in (
            {"TOOLCHAIN_DOWNLOAD_CACHE": "relative-cache"},
            {"XDG_CACHE_HOME": "relative-cache"},
        ):
            with self.subTest(overrides=overrides):
                env = dict(os.environ)
                env.pop("TOOLCHAIN_DOWNLOAD_CACHE", None)
                env.update(overrides)
                with patch.dict(os.environ, env, clear=True), chdir(self.root):
                    with self.assertRaisesRegex(ValueError, "absolute"):
                        toolchain.environment(self.root)
        self.assertFalse((self.root / "relative-cache").exists())

    def test_special_operation_lock_fails_without_blocking(self):
        directory = self.root / ".cache/toolchain"
        directory.mkdir(parents=True)
        os.mkfifo(directory / "operation.lock")
        command = "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import toolchain; toolchain.operation(Path(sys.argv[2])).__enter__()"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                command,
                str(Path(toolchain.__file__).parent),
                str(self.root),
            ],
            capture_output=True,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"regular file", result.stderr)

    def test_generated_inputs_and_outputs_refuse_symlink_escapes(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("generate.py", "toolchain_build.py", "toolchain.py"):
            shutil.copyfile(Path(toolchain.__file__).parent / name, scripts / name)
        source = self.root / "examples/core"
        source.mkdir(parents=True)
        (source / "labels.json").write_text('{"a":"b"}')
        (source / "labels.txt").write_text("a=b\n")
        (source / "greeting.py").write_text("print('hello')\n")
        outside = self.root / "kept"
        outside.mkdir()
        for script, target in (
            ("generate.py", source / "labels.txt"),
            ("toolchain_build.py", self.root / "dist"),
            ("generate.py", source / "labels.json"),
            ("toolchain_build.py", source / "greeting.py"),
        ):
            with self.subTest(script=script, target=target.name):
                saved = target.read_bytes() if target.exists() else None
                if target.exists():
                    target.unlink()
                canary = outside if target.name == "dist" else outside / "canary"
                if canary != outside:
                    canary.write_bytes(saved)
                target.symlink_to(canary, target_is_directory=canary.is_dir())
                before = {
                    p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()
                }
                result = subprocess.run(
                    [sys.executable, str(scripts / script)],
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"symlink", result.stderr)
                self.assertEqual(
                    before,
                    {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()},
                )
                target.unlink()
                if saved is not None:
                    target.write_bytes(saved)

    def test_atomic_output_failure_keeps_old_bytes_and_cleans_temporary(self):
        target = self.root / "output"
        target.write_bytes(b"original")
        before = set(self.root.iterdir())
        with patch.object(toolchain.os, "fsync", side_effect=OSError("disk fault")):
            with self.assertRaisesRegex(OSError, "disk fault"):
                toolchain.atomic_bytes(target, b"replacement", 0o644)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_symlinked_cache_escape_is_rejected_without_deleting(self):
        outside = self.root / "kept"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        base = self.root / ".cache/toolchain/work"
        base.mkdir(parents=True)
        (base / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            toolchain.prune(self.root, all_outputs=True)
        self.assertEqual((outside / "precious").read_text(), "keep")

    def test_deletion_failure_is_not_reported_as_reclaimed_space(self):
        entry = self.root / ".cache/toolchain/work/old"
        entry.mkdir(parents=True)
        (entry / "output").write_text("build")
        with patch.object(
            toolchain.shutil, "rmtree", side_effect=PermissionError("in use")
        ):
            with self.assertRaises(PermissionError):
                toolchain.prune(self.root, all_outputs=True)
        self.assertTrue(entry.exists())

    def test_internal_build_alias_is_counted_once_and_disposable(self):
        entry = self.root / ".cache/toolchain/work/swift"
        (entry / "target/debug").mkdir(parents=True)
        (entry / "target/debug/program").write_bytes(b"executable")
        (entry / "debug").symlink_to("target/debug", target_is_directory=True)
        self.assertEqual(
            toolchain.size(entry), len(b"executable") + len("target/debug")
        )
        with toolchain.operation(self.root):
            self.assertEqual(
                toolchain.prune(self.root, all_outputs=True),
                [".cache/toolchain/work/swift"],
            )
        self.assertFalse(entry.exists())

    def test_nested_escape_rejects_whole_prune_before_any_deletion(self):
        base = self.root / ".cache/toolchain/work"
        for name in ("old", "new"):
            (base / name).mkdir(parents=True)
            (base / name / "build").write_text("keep")
        (base / "new/escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlink"):
            toolchain.prune(self.root, all_outputs=True)
        self.assertEqual((base / "old/build").read_text(), "keep")

    def test_cache_report_does_not_follow_outside_links(self):
        entry = self.root / "report"
        entry.mkdir()
        target = self.root / "outside"
        target.write_bytes(b"x" * 1000)
        (entry / "link").symlink_to(target)
        self.assertEqual(toolchain.size(entry, reporting=True), len(str(target)))

    def test_virtual_environment_interpreter_readiness_is_narrow(self):
        interpreter = self.root / "example/.venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        expected = os.environ["UV_PYTHON"]
        interpreter.symlink_to(expected)
        artifact = {"path": "example/.venv/bin/python", "interpreter": "python"}
        self.assertTrue(
            toolchain.artifact_ready(self.root, artifact, {"UV_PYTHON": expected})
        )
        interpreter.unlink()
        interpreter.symlink_to(self.root / "unrelated")
        self.assertFalse(
            toolchain.artifact_ready(self.root, artifact, {"UV_PYTHON": expected})
        )
        with self.assertRaisesRegex(ValueError, "symlink"):
            toolchain.contained(self.root, "example/.venv/bin/python")

    def test_direct_child_retains_lock_after_wrapper_termination(self):
        scripts = Path(toolchain.__file__).parent
        ready = self.root / "ready"
        child = self.root / "child.py"
        child.write_text(
            "import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(30)\n"
        )
        wrapper = self.root / "wrapper.py"
        wrapper.write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport toolchain\nwith toolchain.operation(Path(sys.argv[2])):\n toolchain.managed_run([sys.executable,sys.argv[3],sys.argv[4]],check=True)\n"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(wrapper),
                str(scripts),
                str(self.root),
                str(child),
                str(ready),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while (
            not ready.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not ready.exists():
            output = process.communicate(timeout=2)[1].decode()
            self.fail(f"Managed child did not start: {output}")
        pid = int(ready.read_text())
        try:
            process.terminate()
            process.wait(timeout=5)
            with self.assertRaisesRegex(ValueError, "active"):
                with toolchain.operation(self.root):
                    self.fail("Orphaned build lost cleanup protection")
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
