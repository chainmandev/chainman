"""Native ownership contracts against the actual Process Compose backend."""

import json
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_tasks

CONTROL = os.environ.get("CHAINMAN_TEST_CONTROL")
BACKEND = os.environ.get("CHAINMAN_TEST_PROCESS_COMPOSE")
WATCHER = os.environ.get("CHAINMAN_TEST_WATCHEXEC")


@unittest.skipUnless(
    CONTROL and BACKEND, "run just control-test for native service qualification"
)
class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman service ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.state = self.base / "state {{literal}}"
        self.state.mkdir(mode=0o700)
        worker = self.root / "worker.py"
        worker.write_text("""import os,signal,time
from pathlib import Path
Path('pid').write_text(str(os.getpid()))
signal.signal(signal.SIGTERM,lambda *_:exit(0))
Path('ready').touch()
while True:time.sleep(.1)
""")
        self.plan = {
            "schema": 1,
            "root": str(self.root),
            "state": str(self.state),
            "backend": BACKEND,
            "fingerprint": "fixture-1",
            "requested": ["worker"],
            "services": {
                "worker": {
                    "command": self.command([sys.executable, str(worker)]),
                    "depends_on": [],
                    "restart": "no",
                    "shutdown_seconds": 1,
                    "readiness": {
                        "command": self.command(
                            [shutil.which("test"), "-f", str(self.root / "ready")]
                        ),
                        "period_seconds": 1,
                        "timeout_seconds": 1,
                        "failure_threshold": 2,
                    },
                }
            },
            "task": self.command([sys.executable, "-c", "raise SystemExit(7)"]),
        }
        self.path = self.base / "plan.json"
        self.addCleanup(self.cleanup)

    def command(self, argv):
        return {"argv": argv, "directory": str(self.root)}

    def test_finite_command_retains_its_nix_package_until_context_exits(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                try:
                    with native_tasks.command(self.root, [["true"]], {}) as command:
                        root = Path(command[2]).parent / "runtime"
                        package = Path(command[0]).parents[1]
                        self.assertEqual(root.resolve(), package)
                        roots = subprocess.check_output(
                            ["nix-store", "--query", "--roots", str(package)],
                            text=True,
                        )
                        self.assertIn(str(root), roots)
                        self.assertTrue(Path(command[0]).is_file())
                        if fail:
                            raise RuntimeError("fixture interruption")
                except RuntimeError as error:
                    self.assertEqual(str(error), "fixture interruption")
                self.assertFalse(root.exists())
                self.assertFalse(root.is_symlink())
                self.assertFalse(root.parent.exists())

    def shared_resource(self):
        resource = json.loads(json.dumps(self.plan))
        resource["state"] = str(self.base / "repository-state")
        resource.pop("task")
        self.plan["services"] = {}
        self.plan["requested"] = []
        self.plan["resources"] = [resource]
        first = self.state
        second = self.base / "second-worktree-state"
        second.mkdir(mode=0o700)
        self.addCleanup(
            lambda: subprocess.run(
                [CONTROL, "stop", str(second)], capture_output=True, timeout=30
            )
        )
        self.addCleanup(
            lambda: subprocess.run(
                [CONTROL, "stop", str(first)], capture_output=True, timeout=30
            )
        )
        return first, second, Path(resource["state"])

    def select_scope(self, state):
        self.state = state
        self.plan["state"] = str(state)

    def test_worktree_stop_releases_only_its_repository_claim(self):
        first, second, shared = self.shared_resource()
        self.run_control("up", check=0)
        pid = int((self.root / "pid").read_text())
        controller = (shared / "controller.json").read_bytes()
        self.select_scope(second)
        self.run_control("up", check=0)
        self.assertEqual((shared / "controller.json").read_bytes(), controller)
        self.select_scope(first)
        self.run_control("stop", check=0)
        self.assertTrue(self.alive(pid))
        self.select_scope(second)
        self.run_control("run", check=7)
        self.assertTrue(self.alive(pid))
        status = json.loads(self.run_control("status", check=0).stdout)
        self.assertTrue(status["resources"][0]["running"])
        self.run_control("stop", check=0)
        self.wait_until(lambda: not self.alive(pid))

    def test_exclusive_service_access_refuses_both_directions_without_stopping_owner(
        self,
    ):
        for first, second in ((False, True), (True, False), (True, True)):
            with self.subTest(first=first, second=second):
                self.plan["exclusive_services"] = first
                self.run_control("up", check=0)
                pid = int((self.root / "pid").read_text())
                self.plan["exclusive_services"] = second
                result = self.run_control("run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exclusive access", result.stderr)
                self.assertTrue(self.alive(pid))
                self.run_control("stop", check=0)
                self.run_control("run", check=7)

    def test_exclusive_repository_claim_survives_rejected_worktree_acquisition(self):
        first, second, _ = self.shared_resource()
        for first_exclusive, second_exclusive in ((True, False), (False, True)):
            with self.subTest(first_exclusive=first_exclusive):
                self.select_scope(first)
                self.plan["resources"][0]["exclusive_services"] = first_exclusive
                self.run_control("up", check=0)
                pid = int((self.root / "pid").read_text())
                self.select_scope(second)
                self.plan["resources"][0]["exclusive_services"] = second_exclusive
                result = self.run_control("run")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exclusive access", result.stderr)
                self.assertTrue(self.alive(pid))
                self.select_scope(first)
                self.run_control("stop", check=0)
                self.select_scope(second)
                self.run_control("run", check=7)

    def test_exclusive_access_does_not_block_unrelated_services(self):
        self.plan["services"]["other"] = {
            "command": self.command(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            ),
            "depends_on": [],
            "restart": "no",
            "shutdown_seconds": 1,
        }
        self.plan["exclusive_services"] = True
        self.run_control("up", check=0)
        pid = int((self.root / "pid").read_text())
        self.plan["requested"] = ["other"]
        self.plan["exclusive_services"] = False
        self.run_control("run", check=7)
        self.assertTrue(self.alive(pid))
        self.run_control("stop", check=0)

    def test_exclusive_task_identity_keeps_access_reserved_until_completion(self):
        self.plan["exclusive_services"] = True
        self.plan["own_task"] = True
        self.plan["task_shutdown_seconds"] = 1
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; Path('task-ready').touch(); "
                "exec(\"while not Path('finish-task').exists(): time.sleep(.05)\")",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        client = subprocess.Popen(
            [CONTROL, "run", str(self.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: client.poll() is None and client.kill())
        self.wait_file(self.root / "task-ready")
        self.plan["exclusive_services"] = False
        result = self.run_control("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exclusive access", result.stderr)
        (self.root / "finish-task").touch()
        self.assertEqual(client.wait(timeout=20), 0)
        self.run_control("up", check=0)
        self.run_control("stop", check=0)

    def test_incompatible_shared_inputs_refuse_while_another_worktree_uses_them(self):
        first, second, _ = self.shared_resource()
        self.run_control("up", check=0)
        pid = int((self.root / "pid").read_text())
        self.select_scope(second)
        self.plan["resources"][0]["fingerprint"] = "changed-data"
        result = self.run_control("up")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incompatible", result.stderr)
        self.assertTrue(self.alive(pid))
        self.select_scope(first)
        self.run_control("stop", check=0)
        self.select_scope(second)
        self.run_control("up", check=0)
        self.run_control("stop", check=0)

    def test_shared_resource_retains_surviving_tasks_parent_lease(self):
        first, second, shared = self.shared_resource()
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; Path('shared-task').touch(); time.sleep(3)",
            ]
        )
        self.plan["own_task"] = True
        self.plan["task_shutdown_seconds"] = 1
        self.path.write_text(json.dumps(self.plan))
        client = subprocess.Popen(
            [CONTROL, "run", str(self.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: client.poll() is None and client.kill())
        self.wait_file(self.root / "shared-task")
        pid = int((self.root / "pid").read_text())
        client.kill()
        client.wait(timeout=5)
        self.select_scope(second)
        self.plan["task"] = self.command([sys.executable, "-c", "raise SystemExit(7)"])
        self.run_control("run", check=7)
        self.assertTrue(self.alive(pid))
        time.sleep(3)
        self.run_control("run", check=7)
        self.wait_until(lambda: not self.alive(pid))

    def test_waiting_worktree_reports_shared_resource_failure(self):
        _, _, shared = self.shared_resource()
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; Path('shared-task').touch(); time.sleep(120)",
            ]
        )
        self.plan.update(wait_for_services=True, own_task=True, task_shutdown_seconds=1)
        self.path.write_text(json.dumps(self.plan))
        client = subprocess.Popen(
            [CONTROL, "run", str(self.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: client.poll() is None and client.kill())
        self.wait_file(self.root / "shared-task")
        os.kill(int((self.root / "pid").read_text()), signal.SIGTERM)
        _, error = client.communicate(timeout=15)
        self.assertEqual(client.returncode, 1, error)
        self.assertIn("worker ended", error)

    def test_borrower_exit_does_not_end_waiter_with_only_shared_services(self):
        self.shared_resource()
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import time; from pathlib import Path; Path('shared-task').touch(); time.sleep(120)",
            ]
        )
        self.plan.update(wait_for_services=True, own_task=True, task_shutdown_seconds=1)
        self.path.write_text(json.dumps(self.plan))
        client = subprocess.Popen(
            [CONTROL, "run", str(self.path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: client.poll() is None and client.kill())
        self.wait_file(self.root / "shared-task")
        self.plan["wait_for_services"] = False
        self.plan["task"] = self.command([sys.executable, "-c", "raise SystemExit(7)"])
        self.run_control("run", check=7)
        time.sleep(1.1)
        self.assertIsNone(client.poll())
        self.run_control("stop", check=0)
        _, error = client.communicate(timeout=15)
        self.assertEqual(client.returncode, 0, error)

    def test_backend_registers_before_exec_and_recovers_abandoned_start(self):
        backend = self.base / "backend"
        backend.write_text(
            f"#!{sys.executable}\n"
            "import json,os,sys,time\nfrom pathlib import Path\n"
            "if 'up' not in sys.argv: raise SystemExit(1)\n"
            "receipt=json.loads(Path('controller.json').read_text())\n"
            "assert receipt['pid']==os.getpid()\n"
            f"Path({str(self.root / 'backend-pid')!r}).write_text(str(os.getpid()))\n"
            "time.sleep(120)\n"
        )
        backend.chmod(0o700)
        self.plan["backend"] = str(backend)
        self.path.write_text(json.dumps(self.plan))
        client = subprocess.Popen(
            [CONTROL, "up", str(self.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(lambda: client.poll() is None and client.kill())
        self.wait_file(self.root / "backend-pid")
        pid = int((self.root / "backend-pid").read_text())
        client.kill()
        client.wait(timeout=5)
        self.run_control("stop", check=0)
        self.wait_until(lambda: not self.alive(pid))

    def test_delayed_old_generation_cannot_launch_or_replace_owner(self):
        spec = dict(self.plan["services"]["worker"], generation="new")
        (self.state / "worker.command.json").write_text(json.dumps(spec))
        owner = self.state / "worker.owner.json"
        owner.write_text("{}")
        result = subprocess.run(
            [CONTROL, "exec", str(self.state), "worker", "old"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(owner.read_text(), "{}")
        self.assertFalse((self.root / "pid").exists())

        owner.unlink()
        (self.state / "worker.stopping").write_text("true")
        result = subprocess.run(
            [CONTROL, "exec", str(self.state), "worker", "new"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "pid").exists())

    def test_old_controller_generation_cannot_replace_current_receipt(self):
        self.plan["generation"] = "new"
        path = self.state / "plan.json"
        path.write_text(json.dumps(self.plan))
        receipt = self.state / "controller.json"
        receipt.write_text("{}")
        result = subprocess.run(
            [CONTROL, "controller", str(path), "old"],
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generation changed", result.stderr)
        self.assertEqual(receipt.read_text(), "{}")
        receipt.unlink()

    def run_control(self, action, check=None, timeout=30):
        self.path.write_text(json.dumps(self.plan))
        result = subprocess.run(
            [
                CONTROL,
                action,
                str(self.state if action in {"status", "stop"} else self.path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check is not None:
            self.assertEqual(result.returncode, check, result.stdout + result.stderr)
        return result

    def run_commands(self, commands, timeout=0):
        path = self.base / "commands.json"
        path.write_text(
            json.dumps(
                {
                    "commands": [self.command(argv) for argv in commands],
                    "timeout_seconds": timeout,
                    "shutdown_seconds": 1,
                }
            )
        )
        return subprocess.run(
            [CONTROL, "command", str(path)], capture_output=True, text=True, timeout=15
        )

    def test_owned_command_sequence_preserves_arguments_and_failure_status(self):
        result = self.run_commands(
            [
                [
                    sys.executable,
                    "-c",
                    "import json,sys; from pathlib import Path; Path('args').write_text(json.dumps(sys.argv[1:]))",
                    "two words",
                    "",
                    "$(literal)",
                ],
                [sys.executable, "-c", "raise SystemExit(7)"],
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('unexpected').touch()",
                ],
            ]
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(
            json.loads((self.root / "args").read_text()),
            ["two words", "", "$(literal)"],
        )
        self.assertFalse((self.root / "unexpected").exists())

    def test_owned_command_cleans_descendants_after_success(self):
        result = self.run_commands(
            [
                [
                    sys.executable,
                    "-c",
                    "import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); Path('descendant').write_text(str(p.pid))",
                ]
            ]
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.alive(int((self.root / "descendant").read_text())))

    def test_owned_command_timeout_kills_signal_resistant_descendants(self):
        code = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('timeout-pid').write_text(str(os.getpid())); time.sleep(120)"
        started = time.monotonic()
        result = self.run_commands([[sys.executable, "-c", code]], timeout=1)
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assertLess(time.monotonic() - started, 6)
        self.assertFalse(self.alive(int((self.root / "timeout-pid").read_text())))

    def test_owned_command_retains_setup_lease_after_client_is_killed(self):
        path = self.base / "commands.json"
        path.write_text(
            json.dumps(
                {
                    "commands": [
                        self.command(
                            [
                                sys.executable,
                                "-c",
                                "import time; from pathlib import Path; Path('task-ready').touch(); time.sleep(2)",
                            ]
                        )
                    ],
                    "timeout_seconds": 10,
                    "shutdown_seconds": 1,
                }
            )
        )
        lock = self.base / "setup.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        env.update(
            TOOLCHAIN_LOCK_FD=str(descriptor),
            TOOLCHAIN_OPERATION_ID="123456789abc",
            TOOLCHAIN_ANCESTOR_FDS="[]",
        )
        parent = subprocess.Popen(
            [CONTROL, "command", str(path)],
            pass_fds=(descriptor,),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_file(self.root / "task-ready")
            os.close(descriptor)
            descriptor = None
            parent.kill()
            parent.wait(timeout=5)
            with lock.open("rb") as observer:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def released():
                    try:
                        fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return True
                    except BlockingIOError:
                        return False

                self.wait_until(released)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_owned_command_cancellation_cleans_signal_resistant_child(self):
        path = self.base / "commands.json"
        code = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('cancel-pid').write_text(str(os.getpid())); time.sleep(120)"
        path.write_text(
            json.dumps(
                {
                    "commands": [self.command([sys.executable, "-c", code])],
                    "timeout_seconds": 0,
                    "shutdown_seconds": 1,
                }
            )
        )
        parent = subprocess.Popen(
            [CONTROL, "command", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_file(self.root / "cancel-pid")
            parent.terminate()
            self.assertNotEqual(parent.wait(timeout=6), 0)
            self.assertFalse(self.alive(int((self.root / "cancel-pid").read_text())))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def cleanup(self):
        result = self.run_control("stop")
        self.assertEqual(result.returncode, 0, result.stderr)

    def pid(self):
        return int((self.root / "pid").read_text())

    def alive(self, pid):
        if Path("/proc").exists():
            try:
                return (
                    Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0]
                    != "Z"
                )
            except FileNotFoundError:
                return False
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        )
        return result.returncode == 0 and not result.stdout.strip().startswith("Z")

    def wait_file(self, path):
        deadline = time.monotonic() + 15
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, str(path))
            time.sleep(0.05)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)

    @unittest.skipUnless(WATCHER, "requires the pinned Watchexec backend")
    def test_stop_during_build_is_bounded_and_cannot_restart_service(self):
        source = self.root / "src"
        source.mkdir()
        (source / "input").write_text("good")
        self.plan["watcher"] = WATCHER
        self.plan["services"]["worker"]["watch"] = {
            "build": self.command(
                [
                    sys.executable,
                    "-c",
                    "import time; from pathlib import Path; value=Path('src/input').read_text(); Path('building').touch() if value=='slow' else None; time.sleep(120 if value=='slow' else .1)",
                ]
            ),
            "paths": [str(source)],
            "ignore": [],
            "debounce_ms": 100,
            "startup_seconds": 5,
        }
        self.run_control("up", check=0)
        pid = self.pid()
        (source / "input").write_text("slow")
        self.wait_file(self.root / "building")
        started = time.monotonic()
        self.run_control("stop", check=0, timeout=15)
        self.assertLess(time.monotonic() - started, 15)
        self.assertFalse(self.alive(pid))
        self.assertFalse(
            json.loads(self.run_control("status", check=0).stdout)["running"]
        )

    @unittest.skipUnless(WATCHER, "requires the pinned Watchexec backend")
    def test_stale_success_cannot_admit_service_before_failed_initial_build(self):
        (self.state / "worker.built.json").write_text('{"finished":"old"}')
        (self.root / "ready").touch()
        self.plan["watcher"] = WATCHER
        self.plan["services"]["worker"]["watch"] = {
            "build": self.command(
                [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(1); raise SystemExit(3)",
                ]
            ),
            "paths": [str(self.root / "worker.py")],
            "ignore": [],
            "debounce_ms": 100,
            "startup_seconds": 2,
        }
        result = self.run_control("run")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "pid").exists())

    @unittest.skipUnless(WATCHER, "requires the pinned Watchexec backend")
    def test_queued_builds_preserve_working_service_on_failure(self):
        source = self.root / "src"
        source.mkdir()
        current = source / "input"
        current.write_text("good")
        build = self.root / "build.py"
        build.write_text("""import time
from pathlib import Path
text=Path('src/input').read_text()
with Path('started').open('a') as out: out.write(text+'\\n')
time.sleep(1)
with Path('finished').open('a') as out: out.write(text+'\\n')
if text=='bad': raise SystemExit(3)
""")
        self.plan["watcher"] = WATCHER
        self.plan["services"]["worker"]["watch"] = {
            "build": self.command([sys.executable, str(build)]),
            "paths": [str(source)],
            "ignore": [],
            "debounce_ms": 100,
            "startup_seconds": 10,
        }
        self.run_control("up", check=0)
        pid = self.pid()
        current.write_text("bad")
        self.wait_until(
            lambda: (self.root / "finished").read_text().splitlines()[-1] == "bad"
        )
        time.sleep(0.2)
        self.assertTrue(self.alive(pid))
        current.write_text("slow-good")
        self.wait_until(
            lambda: (self.root / "started").read_text().splitlines()[-1] == "slow-good"
        )
        for n in range(5):
            current.write_text(f"queued-{n}")
            time.sleep(0.05)
        self.wait_until(
            lambda: (self.root / "finished").read_text().splitlines()[-1] == "queued-4"
        )
        self.wait_until(lambda: self.pid() != pid)
        self.assertEqual(
            (self.root / "finished").read_text().splitlines(),
            ["good", "bad", "slow-good", "queued-4"],
        )
        self.run_control("run", check=7)
        self.assertTrue(self.alive(self.pid()))
        self.run_control("stop", check=0)
        self.assertFalse(self.alive(self.pid()))

    def test_stop_while_dependency_is_pending_does_not_start_late_child(self):
        self.plan["services"]["worker"]["readiness"]["command"] = self.command(
            [shutil.which("test"), "-f", str(self.root / "allow")]
        )
        self.plan["services"]["worker"]["readiness"]["failure_threshold"] = 15
        self.plan["services"]["dependent"] = {
            "command": self.command(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('late-child').touch()",
                ]
            ),
            "depends_on": ["worker"],
            "restart": "no",
            "shutdown_seconds": 1,
        }
        # First start only the dependency, then admit the dependent via the backend
        # while its prerequisite remains unhealthy. The upstream stop must cancel it.
        self.path.write_text(json.dumps(self.plan))
        caller = subprocess.Popen(
            [CONTROL, "up", str(self.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_file(self.root / "pid")
            import hashlib

            socket = (
                Path("/tmp").resolve()
                / f"chainman-control-{os.geteuid()}"
                / (hashlib.sha256(str(self.state).encode()).hexdigest()[:24] + ".sock")
            )
            client = [
                BACKEND,
                "--use-uds",
                "--unix-socket",
                str(socket),
                "--log-file",
                os.devnull,
                "process",
            ]
            subprocess.run(
                [*client, "start", "dependent"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                [*client, "stop", "dependent"],
                check=True,
                capture_output=True,
                timeout=5,
            )
            (self.root / "allow").touch()
            self.assertEqual(caller.wait(timeout=15), 0)
            time.sleep(0.4)
            self.assertFalse((self.root / "late-child").exists())
        finally:
            if caller.poll() is None:
                caller.kill()
                caller.wait()

    def test_readiness_exit_status_and_cleanup(self):
        self.run_control("run", check=7)
        self.assertFalse(self.alive(self.pid()))

    def waiting_task(self, *, wait=True):
        self.plan.update(wait_for_services=wait, own_task=True, task_shutdown_seconds=1)
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import os,time; from pathlib import Path; Path('waiting').write_text(str(os.getpid())); time.sleep(120)",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        parent = subprocess.Popen(
            [CONTROL, "run", str(self.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.wait_file(self.root / "waiting")
        return parent

    def test_explicit_stop_cancels_finite_task_with_failure(self):
        parent = self.waiting_task(wait=False)
        try:
            self.run_control("stop", check=0)
            self.assertNotEqual(parent.wait(timeout=15), 0)
            self.assertFalse(self.alive(int((self.root / "waiting").read_text())))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_finite_task_is_cancelled_when_its_service_fails(self):
        parent = self.waiting_task(wait=False)
        try:
            os.kill(self.pid(), signal.SIGKILL)
            self.assertEqual(parent.wait(timeout=15), 1)
            self.assertFalse(self.alive(int((self.root / "waiting").read_text())))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_stop_recovers_finite_task_after_client_death_and_receipt_corruption(self):
        parent = self.waiting_task(wait=False)
        task = int((self.root / "waiting").read_text())
        parent.kill()
        parent.wait(timeout=5)
        self.assertTrue(self.alive(task))
        for path in self.state.glob("*.lease"):
            path.write_text("corrupt client receipt")
        self.run_control("stop", check=0)
        self.wait_until(lambda: not self.alive(task))
        self.assertFalse(self.alive(self.pid()))

    def test_waiting_task_reports_service_failure_and_cleans_its_command(self):
        parent = self.waiting_task()
        try:
            os.kill(self.pid(), signal.SIGKILL)
            self.assertEqual(parent.wait(timeout=15), 1)
            self.assertFalse(self.alive(int((self.root / "waiting").read_text())))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_explicit_stop_ends_waiting_task_successfully(self):
        parent = self.waiting_task()
        try:
            self.run_control("stop", check=0)
            self.assertEqual(parent.wait(timeout=15), 0)
            self.assertFalse(self.alive(int((self.root / "waiting").read_text())))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

    def test_descendant_descriptor_retains_lease_after_task_returns(self):
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],pass_fds=(3,),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); Path('descendant').write_text(str(p.pid)); raise SystemExit(7)",
            ]
        )
        self.run_control("run", check=7)
        pid = int((self.root / "descendant").read_text())
        try:
            self.assertTrue(self.alive(self.pid()))
            status = json.loads(self.run_control("status", check=0).stdout)
            self.assertTrue(status["leases"]["worker"])
        finally:
            os.kill(pid, signal.SIGTERM)

    def test_persistent_owner_is_reused_and_not_stopped_by_borrower(self):
        self.run_control("up", check=0)
        pid = self.pid()
        self.run_control("run", check=7)
        self.assertEqual(self.pid(), pid)
        self.assertTrue(self.alive(pid))
        self.run_control("stop", check=0)
        self.assertFalse(self.alive(pid))

    def test_controller_crash_can_be_recovered_without_project_config(self):
        self.run_control("up", check=0)
        pid = self.pid()
        controller = json.loads((self.state / "controller.json").read_text())["pid"]
        os.kill(controller, signal.SIGKILL)
        (self.root / "chainman.toml").write_text("broken [ TOML")
        self.assertTrue(self.alive(pid))
        self.run_control("stop", check=0)
        self.assertFalse(self.alive(pid))

    def test_stale_identity_does_not_signal_unrelated_process(self):
        self.run_control("up", check=0)
        self.run_control("stop", check=0)
        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            (self.state / "worker.owner.json").write_text(
                json.dumps({"identity": {"pid": foreign.pid, "birth": "stale"}})
            )
            self.run_control("stop", check=0)
            self.assertIsNone(foreign.poll())
        finally:
            foreign.terminate()
            foreign.wait(timeout=5)

    def test_changed_inputs_are_refused_while_owned_service_is_in_use(self):
        self.run_control("up", check=0)
        pid = self.pid()
        self.plan["fingerprint"] = "fixture-2"
        result = self.run_control("run", check=1)
        self.assertIn("incompatible", result.stderr)
        self.assertTrue(self.alive(pid))

    def test_failed_startup_does_not_run_task(self):
        self.plan["services"]["worker"]["command"] = self.command(
            [sys.executable, "-c", "raise SystemExit(3)"]
        )
        self.plan["task"] = self.command(
            [sys.executable, "-c", "from pathlib import Path; Path('task-ran').touch()"]
        )
        self.run_control("run", check=1)
        self.assertFalse((self.root / "task-ran").exists())
        self.assertFalse(list(self.state.glob("*.lease")))

    def test_readiness_failure_is_bounded_and_cleans_up(self):
        self.plan["services"]["worker"]["readiness"]["command"] = self.command(
            [sys.executable, "-c", "raise SystemExit(1)"]
        )
        self.run_control("run", check=1, timeout=25)
        self.assertFalse(self.alive(self.pid()))

    def test_argv_is_literal_through_backend_and_service_wrapper(self):
        arguments = ["two words", "", "$(touch injected)", "{{unknown}}", "'quoted'"]
        self.plan["services"]["worker"]["command"]["argv"] += arguments
        worker = self.root / "worker.py"
        worker.write_text(
            "import json,sys\nfrom pathlib import Path\nPath('argv').write_text(json.dumps(sys.argv[1:]))\n"
            + worker.read_text()
        )
        self.run_control("run", check=7)
        self.assertEqual(json.loads((self.root / "argv").read_text()), arguments)
        self.assertFalse((self.root / "injected").exists())

    def test_task_retains_resource_lease_after_client_is_killed(self):
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import os,time; from pathlib import Path; os.close(3); Path('task-ready').touch(); time.sleep(30)",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        with (self.base / "client.log").open("w") as log:
            parent = subprocess.Popen(
                [CONTROL, "run", str(self.path)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                self.wait_file(self.root / "task-ready")
                parent.kill()
                parent.wait(timeout=5)
                pid = self.pid()
                self.plan["task"] = self.command(
                    [sys.executable, "-c", "raise SystemExit(7)"]
                )
                self.run_control("run", check=7)
                self.assertTrue(
                    self.alive(pid), "borrower stopped a still-leased service"
                )
            finally:
                try:
                    os.killpg(parent.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                parent.wait(timeout=5)

    def test_persistent_controller_does_not_depend_on_export_directory(self):
        exported = self.base / "export"
        exported.mkdir()
        backend = exported / "process-compose"
        shutil.copyfile(BACKEND, backend)
        backend.chmod(0o700)
        self.plan["backend"] = str(backend)
        self.run_control("up", check=0)
        shutil.rmtree(exported)
        result = self.run_control("status", check=0)
        self.assertTrue(json.loads(result.stdout)["running"])
        self.run_control("stop", check=0)
        self.assertFalse(self.alive(self.pid()))

    def test_explicit_stop_recovers_services_despite_a_corrupt_client_receipt(self):
        self.run_control("up", check=0)
        pid = self.pid()
        (self.state / "invalid.lease").write_text("incomplete JSON")
        self.run_control("stop", check=0)
        self.assertFalse(self.alive(pid))


if __name__ == "__main__":
    unittest.main()
