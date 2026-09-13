"""Independent development sessions coexist without losing mutation protection."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import toolchain as tc


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman concurrent ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "chainman.toml").write_text(
            'schema=1\n[project]\ndefault_profile="host"\n'
            "[cache]\nbuild_limit_gib=0\nstale_hours=0\n"
        )
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.entry = [
            sys.executable,
            str(tc.RUNTIME / "scripts/chainman.py"),
            "--root",
            str(self.root),
        ]

    def command(self, *arguments):
        return subprocess.run(
            [*self.entry, *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @contextmanager
    def session(self, *, inherited=False):
        body = (
            "import json,os,sys\n"
            "print(json.dumps({k:os.environ[k] for k in "
            '["TOOLCHAIN_OPERATION_ID","SCCACHE_SERVER_UDS","TOOLCHAIN_WORK"]}),flush=True)\n'
            "sys.stdin.readline()\n"
        )
        options = {"env": self.env}
        if inherited:
            options = tc.managed_options(options)
        process = subprocess.Popen(
            [*self.entry, "exec", "--", sys.executable, "-c", body],
            **options,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertTrue(
                select.select([process.stdout], [], [], 10)[0], "Session did not start"
            )
            ready = process.stdout.readline()
            if not ready:
                self.fail(process.stderr.read())
            yield json.loads(ready)
        finally:
            if process.poll() is None:
                process.stdin.write("\n")
                process.stdin.flush()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)

    def test_independent_shell_commands_keep_active_outputs_and_distinct_servers(self):
        with self.session() as first:
            active = Path(first["TOOLCHAIN_WORK"]) / "object"
            active.write_text("in use")
            os.utime(active.parent / "last-used", (1, 1))
            with self.session() as second:
                self.assertNotEqual(
                    first["TOOLCHAIN_OPERATION_ID"], second["TOOLCHAIN_OPERATION_ID"]
                )
                self.assertNotEqual(
                    first["SCCACHE_SERVER_UDS"], second["SCCACHE_SERVER_UDS"]
                )
                self.assertEqual(active.read_text(), "in use")
                result = self.command(
                    "exec", "--", sys.executable, "-c", "print('stop command reached')"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                staging = self.root.parent / "update-staging"
                for directory in (staging / "control", staging / "candidate"):
                    directory.mkdir(parents=True)
                for command in (
                    ("clean",),
                    ("cache-prune",),
                    ("_update-prepare", str(staging)),
                ):
                    result = self.command(*command)
                    self.assertNotEqual(result.returncode, 0, command)
                    self.assertIn("active", result.stderr)
                self.assertEqual(active.read_text(), "in use")
        result = self.command("cache-prune", "--all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(active.exists())

    def test_nested_update_excludes_other_sessions_then_returns_to_execution(self):
        with tc.operation(self.root, exclusive=False):
            with tc.operation(self.root):
                result = self.command("exec", "--", sys.executable, "-c", "pass")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("active", result.stderr)
                nested = tc.managed_run(
                    [
                        *self.entry,
                        "exec",
                        "--",
                        sys.executable,
                        "-c",
                        "print('nested')",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(nested.returncode, 0, nested.stderr)
            with self.session():
                for attempt in range(2):
                    with self.assertRaisesRegex(ValueError, "active"):
                        with tc.operation(self.root):
                            self.fail(
                                f"Update entered while another session was live: {attempt}"
                            )
            # A failed upgrade must not unlock the parent's execution lease.
            result = self.command("cache-prune", "--all")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active", result.stderr)
        self.assertEqual(self.command("cache-prune", "--all").returncode, 0)

    def test_legacy_exclusive_lease_is_retained_across_runtime_handoff(self):
        directory = self.root / ".cache/toolchain"
        directory.mkdir(parents=True)
        work = directory / "work/legacy"
        work.mkdir(parents=True)
        (work / "object").write_text("legacy output in use")
        (work / "last-used").touch()
        os.utime(work / "last-used", (1, 1))
        with (directory / "operation.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            env = dict(self.env, TOOLCHAIN_LOCK_FD=str(lease.fileno()))
            result = subprocess.run(
                [*self.entry, "exec", "--", sys.executable, "-c", "print('handoff')"],
                env=env,
                pass_fds=(lease.fileno(),),
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("handoff", result.stdout)
            self.assertEqual((work / "object").read_text(), "legacy output in use")
            self.assertNotEqual(self.command("cache-prune", "--all").returncode, 0)

    def test_legacy_writers_cannot_bypass_a_current_execution(self):
        with self.session():
            with (self.root / ".cache/toolchain/operation.lock").open("a") as legacy:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_legacy_compatibility_does_not_bypass_a_later_modern_writer(self):
        directory = self.root / ".cache/toolchain"
        directory.mkdir(parents=True)
        with (directory / "operation.lock").open("a") as legacy:
            fcntl.flock(legacy, fcntl.LOCK_EX)
            with patch.dict(os.environ, TOOLCHAIN_LOCK_FD=str(legacy.fileno())):
                with tc.operation(self.root, exclusive=False, new_execution=True):
                    parent_options = tc.managed_options({"env": self.env})
                    with tc.operation(self.root):
                        # This late arrival inherits the idle parent's lease,
                        # not the nested writer's newly acquired gate.
                        result = subprocess.run(
                            [*self.entry, "exec", "--", sys.executable, "-c", "pass"],
                            **parent_options,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("active", result.stderr)
                    with self.session(inherited=True):
                        pass

    def test_background_command_from_same_shell_blocks_nested_update(self):
        with tc.operation(self.root, exclusive=False):
            parent = tc.managed_options({})["env"]["TOOLCHAIN_OPERATION_ID"]
            with self.session(inherited=True) as child:
                self.assertNotEqual(child["TOOLCHAIN_OPERATION_ID"], parent)
                with self.assertRaisesRegex(ValueError, "active"):
                    with tc.operation(self.root):
                        self.fail("A background command was mistaken for an idle shell")
            with tc.operation(self.root):
                pass

    def test_descriptor_closing_wrapper_readmits_shared_commands_independently(self):
        with tc.operation(self.root, exclusive=False):
            work = self.root / ".cache/toolchain/work/held"
            work.mkdir(parents=True)
            artifact = work / "object"
            artifact.write_text("parent output")
            (work / "last-used").touch()
            os.utime(work / "last-used", (1, 1))
            inherited = tc.managed_options({"env": self.env})["env"]
            # Ordinary Python/Node subprocess defaults retain environment strings
            # but close the parent's operation descriptors.
            body = "import os; print(os.environ['TOOLCHAIN_OPERATION_ID'])\n"
            result = subprocess.run(
                [*self.entry, "exec", "--", sys.executable, "-c", body],
                env=inherited,
                close_fds=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual(
                result.stdout.strip(), inherited["TOOLCHAIN_OPERATION_ID"]
            )
            # A descriptor-closing wrapper cannot share the parent's mutation
            # admission: the still-running parent must keep its outputs alive.
            blocked = subprocess.run(
                [*self.entry, "cache-prune", "--all"],
                env=inherited,
                close_fds=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("active", blocked.stderr)
            self.assertEqual(artifact.read_text(), "parent output")
        self.assertEqual(self.command("cache-prune", "--all").returncode, 0)
        self.assertFalse(artifact.exists())

    def test_managed_child_accepts_inherited_environment_and_preserves_io_mode(self):
        body = (
            "import os,sys\n"
            "os.fstat(int(os.environ['TOOLCHAIN_LOCK_FD']))\n"
            "sys.stdout.buffer.write(os.environ['APP_VALUE'].encode() + b':' + "
            "sys.stdin.buffer.read())\n"
        )
        with (
            patch.dict(os.environ, APP_VALUE="inherited"),
            tc.operation(self.root, exclusive=False),
        ):
            for text_mode, payload in ((True, "literal λ\n"), (False, b"\x00\xff\n")):
                with self.subTest(text=text_mode):
                    result = tc.managed_run(
                        [sys.executable, "-c", body],
                        env=None,
                        text=text_mode,
                        input=payload,
                        capture_output=True,
                        check=True,
                        timeout=10,
                    )
                    prefix = "inherited:" if text_mode else b"inherited:"
                    self.assertEqual(result.stdout, prefix + payload)
                    self.assertEqual(result.stderr, "" if text_mode else b"")

    def test_public_child_does_not_borrow_its_parent_compiler_lifetime(self):
        with tc.operation(self.root, exclusive=False):
            inherited = tc.managed_options({})["env"]
            inherited["CHAINMAN_COMPILER_OWNER"] = str(self.root)
            with patch.dict(os.environ, inherited):
                with tc.operation(self.root, exclusive=False):
                    self.assertEqual(
                        tc.environment(self.root)["CHAINMAN_COMPILER_OWNER"],
                        str(self.root),
                    )
                with tc.operation(self.root, exclusive=False, new_execution=True):
                    self.assertNotIn(
                        "CHAINMAN_COMPILER_OWNER", tc.environment(self.root)
                    )

    def test_inherited_writer_gate_still_excludes_background_siblings(self):
        with tc.operation(self.root):
            with self.session(inherited=True):
                with self.assertRaisesRegex(ValueError, "active"):
                    with tc.operation(self.root):
                        self.fail("Inherited gate bypassed its live child lease")
            with tc.operation(self.root):
                pass

    def test_returning_to_ancestor_project_cannot_clean_its_active_outputs(self):
        other = self.root / "other"
        other.mkdir()
        (other / "chainman.toml").write_bytes(
            (self.root / "chainman.toml").read_bytes()
        )
        with tc.operation(self.root, exclusive=False):
            output = Path(tc.environment(self.root)["TOOLCHAIN_WORK"]) / "object"
            output.write_text("ancestor output in use")
            with tc.operation(other, exclusive=False):
                with tc.operation(self.root, exclusive=False) as outer:
                    self.assertFalse(outer)
                for action in ("clean", "cache-prune"):
                    result = tc.managed_run(
                        [*self.entry, action], capture_output=True, text=True
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("active managed operation", result.stderr)
            self.assertEqual(output.read_text(), "ancestor output in use")

    def test_module_setup_cannot_replace_an_environment_during_use(self):
        with self.session():
            result = self.command("module", "project", "setup")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active", result.stderr)

    def test_direct_runtime_cleanup_also_rejects_nesting(self):
        with tc.operation(self.root, exclusive=False):
            for action in ("clean", "cache-prune"):
                result = tc.managed_run(
                    [sys.executable, str(tc.RUNTIME / "scripts/toolchain.py"), action],
                    env=dict(self.env, CHAINMAN_ROOT=str(self.root)),
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("active managed operation", result.stderr)

    def test_unexpected_lease_files_cannot_escape_inventory(self):
        with tc.operation(self.root, exclusive=False):
            outside = self.root / "outside"
            outside.write_text("keep")
            lease = self.root / ".cache/toolchain/operations/foreign"
            lease.symlink_to(outside)
            result = self.command("cache-prune", "--all")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(outside.read_text(), "keep")
            lease.unlink()


if __name__ == "__main__":
    unittest.main()
