"""Native ownership contracts against the actual Process Compose backend."""

import json
import fcntl
import os
from pathlib import Path
import platform
import pty
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_tasks
import services

CONTROL = os.environ.get("CHAINMAN_TEST_CONTROL")
BACKEND = os.environ.get("CHAINMAN_TEST_PROCESS_COMPOSE")
WATCHER = os.environ.get("CHAINMAN_TEST_WATCHEXEC")


@unittest.skipUnless(
    CONTROL and BACKEND, "run just control-test for native service qualification"
)
class ServiceControlTests(unittest.TestCase):
    def setUp(self):
        setup_policy = patch.dict(os.environ, {"CHAINMAN_SETUP": "auto"})
        setup_policy.start()
        self.addCleanup(setup_policy.stop)
        temporary = tempfile.TemporaryDirectory(prefix="chainman service ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.state = self.base / "state {{literal}}"
        self.state.mkdir(mode=0o700)
        worker = self.root / "worker.py"
        worker.write_text("""import os,signal,time
from pathlib import Path
Path('pid.next').write_text(str(os.getpid()))
os.replace('pid.next', 'pid')
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

    def terminal_command(
        self, code, *, interrupt=False, timeout=0, ignored_interrupt=False
    ):
        """Use a controlling terminal, not killpg on a redirected subprocess."""
        task = self.base / "terminal-task.json"
        recovery = self.base / "terminal-owner"
        task.write_text(
            json.dumps(
                {
                    "commands": [self.command([sys.executable, "-c", code])],
                    "shutdown_seconds": 1,
                    "timeout_seconds": timeout,
                    "recovery_state": str(recovery),
                }
            )
        )
        harness = (
            "import os,subprocess,sys,termios; "
            "before=termios.tcgetattr(0); "
            "r=subprocess.run(sys.argv[1:]); "
            "assert os.tcgetpgrp(0)==os.getpgrp(), 'foreground not restored'; "
            "assert termios.tcgetattr(0)==before, 'terminal settings not restored'; "
            "print('RESTORED',r.returncode,flush=True)"
        )
        if ignored_interrupt:
            harness = (
                "import signal; signal.signal(signal.SIGINT,signal.SIG_IGN); " + harness
            )
        pid, master = pty.fork()
        if pid == 0:
            os.execv(
                sys.executable,
                [sys.executable, "-c", harness, CONTROL, "command", str(task)],
            )
        output = bytearray()
        exited = False
        sent = False
        deadline = time.monotonic() + 12
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
                if not sent and b"INPUT READY" in output:
                    os.write(master, b"literal spaces $()\n")
                    sent = True
                if interrupt and b"INTERRUPT READY" in output:
                    os.write(master, b"\x03")
                    interrupt = False
                if b"RESTORED " in output:
                    break
            self.assertIn(b"RESTORED ", output, output.decode(errors="replace"))
            _, status = os.waitpid(pid, 0)
            exited = True
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, output)
            return output.decode(errors="replace")
        finally:
            if not exited:
                owner = recovery / "task.owner.json"
                if owner.exists():
                    group = json.loads(owner.read_text())["identity"]["pid"]
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            os.close(master)

    def test_owned_terminal_input_output_and_restore(self):
        code = (
            "import os,sys,termios; "
            "assert os.tcgetpgrp(0)==os.getpgrp(); "
            "print('INPUT READY',flush=True); "
            "assert input()=='literal spaces $()'; "
            "settings=termios.tcgetattr(0); settings[3]&=~termios.ECHO; "
            "termios.tcsetattr(0,termios.TCSANOW,settings); "
            "print('APPLICATION READY',flush=True); sys.exit(7)"
        )
        output = self.terminal_command(code)
        self.assertIn("APPLICATION READY", output)
        self.assertIn("RESTORED 7", output)

    def test_owned_terminal_ctrl_c_and_restore(self):
        code = (
            "import os,signal,time; "
            "assert os.tcgetpgrp(0)==os.getpgrp(); "
            "signal.signal(signal.SIGINT,lambda *_:exit(130)); "
            "print('INTERRUPT READY',flush=True); time.sleep(120)"
        )
        output = self.terminal_command(code, interrupt=True)
        self.assertIn("RESTORED 130", output)
        self.assertIn("stopping task", output)

    def test_owned_terminal_timeout_restores_raw_terminal(self):
        code = (
            "import signal,time,tty; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "tty.setraw(0); print('RAW READY',flush=True); time.sleep(120)"
        )
        output = self.terminal_command(code, timeout=1)
        self.assertIn("RAW READY", output)
        self.assertIn("RESTORED 124", output)

    def test_owned_terminal_interrupt_cannot_become_successful_preparation(self):
        # Async shell/container entry can inherit ignored SIGINT. A cooperative
        # command may then exit zero after the anchor forwards cancellation.
        code = (
            "import signal,time; "
            "signal.signal(signal.SIGINT,lambda *_:exit(0)); "
            "print('INTERRUPT READY',flush=True); time.sleep(120)"
        )
        output = self.terminal_command(code, interrupt=True, ignored_interrupt=True)
        self.assertIn("RESTORED 130", output)

    def test_stopped_task_owner_handles_cancellation_without_kill_timeout(self):
        receipt = self.base / "stopped-owner"
        ready = self.root / "stopped-ready"
        task = self.base / "stopped-task.json"
        task.write_text(
            json.dumps(
                {
                    "commands": [
                        self.command(
                            [
                                sys.executable,
                                "-c",
                                "import signal,time; from pathlib import Path; "
                                "signal.signal(signal.SIGTERM,lambda *_:(Path('term-received').touch(),exit(23))); "
                                f"Path({str(ready)!r}).touch(); time.sleep(120)",
                            ]
                        )
                    ],
                    "shutdown_seconds": 5,
                    "recovery_state": str(receipt),
                }
            )
        )
        client = subprocess.Popen(
            [CONTROL, "command", str(task)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        group = None
        try:
            self.wait_file(ready)
            group = json.loads((receipt / "task.owner.json").read_text())["identity"][
                "pid"
            ]
            os.killpg(group, signal.SIGSTOP)
            client.terminate()
            _, error = client.communicate(timeout=3)
            self.assertEqual(client.returncode, 143, error)
            self.assertTrue((self.root / "term-received").exists(), error)
        finally:
            if client.poll() is None:
                if group:
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                client.kill()
                client.communicate(timeout=5)

    def test_cancelled_task_cannot_report_success_when_child_exits_zero(self):
        ready = self.root / "cooperative-ready"
        spec = {
            "command": self.command(
                [
                    sys.executable,
                    "-c",
                    "import signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGINT,lambda *_:exit(0)); "
                    f"Path({str(ready)!r}).touch(); time.sleep(120)",
                ]
            ),
            "shutdown_seconds": 1,
            "forward_leases": True,
        }
        (self.state / "task.command.json").write_text(json.dumps(spec))
        client = subprocess.Popen(
            [CONTROL, "exec", str(self.state), "task"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.wait_file(ready)
            client.send_signal(signal.SIGINT)
            _, error = client.communicate(timeout=3)
            self.assertEqual(client.returncode, 130, error)
        finally:
            if client.poll() is None:
                client.terminate()
                client.communicate(timeout=5)

    def test_exported_plan_runs_after_readiness_and_preserves_task_exit(self):
        output = self.base / "exported"
        output.mkdir(mode=0o700)
        (output / "host-environment").write_bytes(b"")
        launcher = self.root / "launch"
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(
                [
                    sys.executable,
                    str(services.chainman.RUNTIME / "scripts/chainman.py"),
                    "--root",
                    str(self.root),
                ]
            )
            + ' "$@"\n'
        )
        launcher.chmod(0o700)
        task = [
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('ready').is_file(); "
            "Path('task-result').write_text('observed readiness'); raise SystemExit(7)",
        ]
        readiness = [
            sys.executable,
            "-c",
            "from pathlib import Path; raise SystemExit(0 if Path('ready').is_file() else 1)",
        ]
        (self.root / "chainman.toml").write_text(
            'schema=3\n[project]\ndefault_profile="host"\n'
            '[tasks.check]\nservices=["worker"]\ncommands='
            + json.dumps([task])
            + "\n[services.worker]\ncommand="
            + json.dumps([sys.executable, str(self.root / "worker.py")])
            + "\nshutdown_seconds=1\nreadiness={command="
            + json.dumps(readiness)
            + ",period_seconds=1,timeout_seconds=2,failure_threshold=10}\n"
        )
        fixture_env = dict(os.environ, CHAINMAN_MODE="host-nix")
        # The fixture is a separate project. Its controller gets its own leases;
        # it cannot advertise the outer Just process's non-inherited descriptors.
        for key in (
            "TOOLCHAIN_LOCK_FD",
            "TOOLCHAIN_GATE_FD",
            "TOOLCHAIN_COMPAT_FD",
            "TOOLCHAIN_OPERATION_ID",
            "TOOLCHAIN_ANCESTOR_FDS",
        ):
            fixture_env.pop(key, None)
        target = (
            platform.system().lower()
            + "-"
            + {
                "aarch64": "arm64",
                "arm64": "arm64",
                "x86_64": "amd64",
            }[platform.machine()]
        )
        with patch.dict(os.environ, fixture_env, clear=True):
            services.export(
                self.root,
                [
                    str(output),
                    target,
                    str(self.base / "cache"),
                    "",
                    str(launcher),
                    "run",
                    "check",
                ],
            )
            self.plan = json.loads((output / "plan.json").read_text())
            self.state = Path(self.plan["state"])
            self.run_control("run", check=7)
            self.assertEqual(
                (self.root / "task-result").read_text(), "observed readiness"
            )
            pid = self.pid()
            self.run_control("stop", check=0)
            self.wait_until(lambda: not self.alive(pid))

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

    def test_private_bridge_lease_outlives_shared_endpoints_and_releases_last(self):
        first, second, shared = self.shared_resource()
        network_state = self.base / "network-state"
        network_file = self.base / "network.json"
        events = self.base / "network-events"
        scope = "a" * 24
        network = {
            "Id": "b" * 64,
            "Name": "chainman-" + scope,
            "Driver": "bridge",
            "Labels": {"dev.chainman.scope": scope, "dev.chainman.network": "1"},
        }
        engine = self.base / "docker"
        engine.write_text(
            f"#!{sys.executable}\n"
            + f"""import json,os,sys
from pathlib import Path
state=Path({str(network_file)!r}); events=Path({str(events)!r})
if sys.argv[1]=='info': print('fixture-engine'); sys.exit(0)
assert sys.argv[1]=='network'
action=sys.argv[2]
if action=='inspect':
 if not state.exists(): sys.exit(1)
 print(state.read_text())
elif action=='ls':
 if state.exists(): print({network["Name"]!r})
elif action=='create':
 assert not state.exists()
 state.write_text(json.dumps([{network!r}]))
 with events.open('a') as f: f.write('create\\n')
elif action=='rm':
 assert sys.argv[3:]==[{network["Id"]!r}]
 pidfile=Path({str(self.root / "pid")!r})
 if pidfile.exists():
  try: os.kill(int(pidfile.read_text()),0)
  except ProcessLookupError: pass
  else: raise AssertionError('network removal preceded endpoint shutdown')
 state.unlink()
 with events.open('a') as f: f.write('remove\\n')
else: raise AssertionError(sys.argv)
"""
        )
        engine.chmod(0o700)
        pool = {
            "schema": 1,
            "root": str(self.root),
            "state": str(network_state),
            "backend": BACKEND,
            "fingerprint": "bridge-fixture",
            "services": {},
            "requested": [],
            "bridge": {"engine": str(engine), "name": network["Name"], "scope": scope},
        }
        self.plan["resources"].insert(0, pool)
        self.run_control("up", check=0)
        self.assertTrue(network_file.exists())
        self.assertFalse((network_state / "controller.json").exists())
        self.select_scope(second)
        self.run_control("up", check=0)
        status = json.loads(self.run_control("status", check=0).stdout)
        bridge_status = next(
            entry for entry in status["resources"] if "bridge" in entry
        )
        self.assertTrue(bridge_status["running"])
        self.assertEqual(bridge_status["clients"], 2)
        self.assertEqual(bridge_status["network_id"], network["Id"])
        human = subprocess.run(
            [CONTROL, "status", str(self.state), "--human"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn(f"Shared resource {network_state}:", human)
        self.assertIn(
            f"Network bridge {network['Name']}: running=true (clients=2)", human
        )
        self.select_scope(first)
        self.run_control("stop", check=0)
        self.assertTrue(network_file.exists())
        self.assertEqual(events.read_text(), "create\n")
        self.select_scope(second)
        self.run_control("stop", check=0)
        self.assertFalse(network_file.exists())
        self.assertEqual(events.read_text(), "create\nremove\n")

    def test_empty_local_scope_keeps_existing_resource_cleanup_intent(self):
        self.shared_resource()
        self.run_control("up", check=0)
        pid = int((self.root / "pid").read_text())
        self.plan["resources"] = []
        self.run_control("run", check=7)
        self.assertTrue(self.alive(pid))
        saved = json.loads((self.state / "plan.json").read_text())
        self.assertEqual(len(saved["resources"]), 1)
        self.run_control("stop", check=0)
        self.wait_until(lambda: not self.alive(pid))

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
        human = subprocess.run(
            [CONTROL, "status", str(self.state), "--human"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertIn(f"Shared resource {shared}:", human)
        self.assertIn("Service worker: Running (ready=Ready)", human)
        self.run_control("stop", check=0)
        self.wait_until(lambda: not self.alive(pid))

    def test_repository_only_task_does_not_start_unrequested_local_services(self):
        self.shared_resource()
        unused = json.loads(json.dumps(self.plan["resources"][0]["services"]["worker"]))
        unused["command"] = self.command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; Path('unexpected-local-start').touch(); time.sleep(120)",
            ]
        )
        unused.pop("readiness")
        self.plan["services"] = {"unused": unused}
        self.run_control("up", check=0)
        self.assertFalse((self.root / "unexpected-local-start").exists())
        self.assertFalse((self.state / "controller.json").exists())
        # The existing empty local lease is valid without a local controller.
        self.run_control("run", check=7)
        self.assertFalse((self.root / "unexpected-local-start").exists())
        self.plan["requested"] = ["unused"]
        self.run_control("up", check=0)
        self.wait_until(lambda: (self.root / "unexpected-local-start").exists())
        self.run_control("stop", check=0)

    def test_probe_timeout_reaps_descendants_and_preserves_service(self):
        child = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path('probe-child').write_text(str(os.getpid())); time.sleep(120)"
        parent = "import os,signal,subprocess,sys,time; from pathlib import Path;\nif Path('probe-child').exists(): sys.exit(0)\nsignal.signal(signal.SIGTERM,signal.SIG_IGN); Path('probe-parent').write_text(str(os.getpid())); subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(120)"
        readiness = self.plan["services"]["worker"]["readiness"]
        readiness["command"] = self.command([sys.executable, "-c", parent, child])
        readiness["timeout_seconds"] = 1
        readiness["failure_threshold"] = 5
        self.run_control("up", check=0, timeout=12)
        for name in ("probe-parent", "probe-child"):
            self.assertFalse(self.alive(int((self.root / name).read_text())))
        self.assertTrue(self.alive(int((self.root / "pid").read_text())))
        self.run_control("stop", check=0)

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
            ["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
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
        log_path = self.base / "viewer.log"
        with log_path.open("w") as output:
            viewer = subprocess.Popen(
                [CONTROL, "logs", str(self.state), "--follow"],
                stdout=output,
                stderr=subprocess.PIPE,
                text=True,
            )

        def close_viewer():
            if viewer.poll() is None:
                viewer.kill()
            viewer.communicate(timeout=5)

        self.addCleanup(close_viewer)
        pid = self.pid()
        current.write_text("bad")
        self.wait_until(
            lambda: (self.root / "finished").read_text().splitlines()[-1] == "bad"
        )
        time.sleep(0.2)
        self.assertTrue(self.alive(pid))
        try:
            self.wait_until(
                lambda: (
                    "build failed; retaining the last successful service"
                    in log_path.read_text()
                )
            )
        except AssertionError as error:
            error.add_note(
                f"viewer status={viewer.poll()}; logs={log_path.read_text()!r}"
            )
            raise
        viewer.send_signal(signal.SIGINT)
        _, error = viewer.communicate(timeout=5)
        self.assertEqual(viewer.returncode, 0, error)
        self.assertTrue(self.alive(pid), "detaching logs stopped a persistent service")
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
        logs = subprocess.run(
            [CONTROL, "logs", str(self.state)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(logs.returncode, 0, logs.stderr)
        self.assertIn("Building: worker", logs.stdout)
        self.assertIn("Rebuilt and restarted: worker", logs.stdout)
        self.assertEqual(
            (self.root / "finished").read_text().splitlines(),
            ["good", "bad", "slow-good", "queued-4"],
        )
        self.run_control("run", check=7)
        self.assertTrue(self.alive(self.pid()))
        self.run_control("stop", check=0)
        self.assertFalse(self.alive(self.pid()))

    def test_foreground_waiting_task_displays_service_output(self):
        worker = self.root / "worker.py"
        worker.write_text(
            "print('fixture service output', flush=True)\n" + worker.read_text()
        )
        self.plan["wait_for_services"] = True
        self.plan["task"] = self.command(
            [sys.executable, "-c", "import time; time.sleep(.3)"]
        )
        result = self.run_control("run", check=0)
        self.assertIn("fixture service output", result.stderr)
        self.assertIn("preparing", result.stderr)
        self.assertFalse(self.alive(self.pid()))

    def test_development_summary_preparation_input_stop_and_retained_logs(self):
        worker = self.root / "worker.py"
        worker.write_text(
            "print('routine service noise\\n'*3000, flush=True)\n" + worker.read_text()
        )
        self.plan.update(
            wait_for_services=True,
            own_task=True,
            task_shutdown_seconds=2,
            presentation={
                "task": "dev",
                "title": "Fixture app",
                "urls": {"Browser": "http://localhost:4321"},
            },
        )
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                f"""import sys,time
from pathlib import Path
sys.path.insert(0, {scripts!r})
import development_status
development_status.publish('dev','preparing')
Path('preparing').touch()
assert sys.stdin.readline() == 'literal $() input\\n'
print('foreground preparation output', flush=True)
development_status.publish('dev','ready')
time.sleep(120)
""",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        with (self.base / "display").open("w+b") as display:
            client = subprocess.Popen(
                [CONTROL, "run", str(self.path)],
                stdin=subprocess.PIPE,
                stdout=display,
                stderr=display,
                env=dict(os.environ, CHAINMAN_DEV_OUTPUT="summary"),
            )
            try:
                self.wait_file(self.root / "preparing")
                display.seek(0)
                starting = display.read().decode()
                self.assertIn("http://localhost:4321", starting)
                self.assertNotIn("— ready", starting)
                self.assertNotIn("routine service noise", starting)
                status = json.loads(self.run_control("status", check=0).stdout)
                self.assertFalse(status["applications"][0]["reached_ready"])
                client.stdin.write(b"literal $() input\n")
                client.stdin.flush()
                self.wait_until(
                    lambda: (
                        json.loads(self.run_control("status", check=0).stdout)[
                            "applications"
                        ][0]["phase"]
                        == "ready"
                    )
                )
                human = subprocess.run(
                    [CONTROL, "status", str(self.state), "--human"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("Fixture app — ready", human.stdout)
                self.run_control("stop", check=0)
                self.assertEqual(client.wait(timeout=15), 0)
                display.seek(0)
                output = display.read().decode()
                self.assertIn("foreground preparation output", output)
                self.assertIn("Fixture app — ready", output)
                self.assertNotIn("routine service noise", output)
                self.assertLess(len(output), 10000)
                status = json.loads(self.run_control("status", check=0).stdout)
                self.assertEqual(status["applications"][0]["phase"], "stopped")
                logs = subprocess.run(
                    [CONTROL, "logs", str(self.state)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn("routine service noise", logs.stdout)
            finally:
                client.stdin.close()
                if client.poll() is None:
                    client.terminate()
                    client.wait(timeout=15)

    def test_foreground_group_interrupt_releases_services(self):
        self.foreground_signal_releases_services(signal.SIGINT, group=True)

    def test_direct_hangup_releases_services(self):
        self.foreground_signal_releases_services(signal.SIGHUP, group=False)

    def test_direct_termination_during_preparation_stops_owned_work(self):
        self.plan.update(own_task=True, task_shutdown_seconds=2)
        self.plan["prepare"] = self.command(
            [
                sys.executable,
                "-c",
                "import os,time; from pathlib import Path; Path('preparation-pid').write_text(str(os.getpid())); time.sleep(120)",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        with (self.base / "preparation.log").open("w+b") as log:
            client = subprocess.Popen(
                [CONTROL, "run", str(self.path)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            worker = None
            try:
                self.wait_file(self.root / "preparation-pid")
                worker = int((self.root / "preparation-pid").read_text())
                client.terminate()
                client.wait(timeout=15)
                self.assertFalse(self.alive(worker), "preparation outlived its caller")
                self.assertFalse(
                    (self.root / "pid").exists(), "service started after cancellation"
                )
                self.assertNotEqual(client.returncode, 0)
            finally:
                if client.poll() is None:
                    client.kill()
                    client.wait(timeout=5)
                if worker and self.alive(worker):
                    os.kill(worker, signal.SIGKILL)

    def foreground_signal_releases_services(self, sent, *, group):
        self.plan.update(own_task=True, task_shutdown_seconds=2, wait_for_services=True)
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; Path('task-ready').touch(); time.sleep(120)",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        with (self.base / "client.log").open("w+b") as log:
            client = subprocess.Popen(
                [CONTROL, "run", str(self.path)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                self.wait_file(self.root / "task-ready")
                pid = self.pid()
                (os.killpg if group else os.kill)(client.pid, sent)
                client.wait(timeout=15)
                status = json.loads(self.run_control("status", check=0).stdout)
                log.seek(0)
                self.assertFalse(status["running"], log.read().decode())
                self.assertFalse(self.alive(pid))
                self.assertEqual(status["leases"], {})
                self.assertEqual(status["applications"][0]["phase"], "stopped")
                self.assertFalse(status["applications"][0]["reached_ready"])
            finally:
                if client.poll() is None:
                    client.kill()
                    client.wait(timeout=5)

    def test_killed_task_supervisor_releases_anchor_before_services(self):
        client = self.waiting_task()
        try:
            pid = self.pid()
            receipts = list(self.state.glob("*.lease.task.json"))
            self.assertEqual(len(receipts), 1)
            task = json.loads(receipts[0].read_text())
            # Bounded cancellation can kill this intermediary before its owned
            # process group has finished shutting down (notably Docker clients).
            os.kill(task["pid"], signal.SIGKILL)
            self.assertEqual(client.wait(timeout=20), 137)
            self.assertFalse(self.alive(pid), "service survived its last client")
            status = json.loads(self.run_control("status", check=0).stdout)
            self.assertFalse(status["running"])
            self.assertEqual(status["leases"], {})
            self.assertFalse(list((self.state / "tasks").glob("*/task.owner.json")))
        finally:
            if client.poll() is None:
                client.kill()
                client.wait(timeout=5)

    def test_log_viewer_interrupts_with_a_full_output_pipe(self):
        self.run_control("up", check=0)
        (self.state / "services.log").write_text("fixture output\n" * 10000)
        viewer = subprocess.Popen(
            [CONTROL, "logs", str(self.state), "--follow"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            time.sleep(0.3)
            self.assertIsNone(viewer.poll())
            viewer.send_signal(signal.SIGINT)
            self.assertEqual(viewer.wait(timeout=3), 0)
            self.assertTrue(self.alive(self.pid()))
        finally:
            if viewer.poll() is None:
                viewer.kill()
            viewer.communicate(timeout=5)

    @unittest.skipUnless(WATCHER, "requires the pinned Watchexec backend")
    def test_development_reports_failed_rebuild_with_old_server_running(self):
        source = self.root / "source"
        source.write_text("good")
        self.plan.update(
            wait_for_services=True,
            own_task=True,
            task_shutdown_seconds=2,
            watcher=WATCHER,
            presentation={"task": "dev", "title": "Watched app"},
        )
        self.plan["services"]["worker"]["watch"] = {
            "build": self.command(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; sys.exit(1 if Path('source').read_text()=='bad' else 0)",
                ]
            ),
            "paths": [str(source)],
            "ignore": [],
            "debounce_ms": 100,
            "startup_seconds": 10,
        }
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                f"import sys,time; sys.path.insert(0,{scripts!r}); import development_status; development_status.publish('dev','ready'); time.sleep(120)",
            ]
        )
        self.path.write_text(json.dumps(self.plan))
        with (self.base / "display").open("w+b") as display:
            parent = subprocess.Popen(
                [CONTROL, "run", str(self.path)],
                stdout=display,
                stderr=display,
                env=dict(os.environ, CHAINMAN_DEV_OUTPUT="summary"),
            )

            def phase():
                rows = list((self.state / "applications").glob("*.json"))
                return json.loads(rows[0].read_text())["phase"] if rows else ""

            try:
                self.wait_until(lambda: phase() == "ready")
                previous = self.pid()
                source.write_text("bad")
                self.wait_until(lambda: phase() == "degraded")
                self.assertTrue(self.alive(previous))
                self.assertEqual(self.pid(), previous)
                self.assertIsNone(parent.poll())
                display.seek(0)
                self.assertIn("Build failed: worker", display.read().decode())
                source.write_text("good again")
                self.wait_until(lambda: self.pid() != previous and phase() == "ready")
                self.run_control("stop", check=0)
                self.assertEqual(parent.wait(timeout=15), 0)
            finally:
                if parent.poll() is None:
                    parent.terminate()
                    parent.wait(timeout=15)

    @unittest.skipUnless(WATCHER, "requires the pinned Watchexec backend")
    def test_watch_restart_preserves_active_client_but_real_exit_cancels_it(self):
        source = self.root / "source"
        source.write_text("first")
        self.plan["watcher"] = WATCHER
        self.plan["services"]["worker"]["watch"] = {
            "build": self.command([sys.executable, "-c", "pass"]),
            "paths": [str(source)],
            "ignore": [],
            "debounce_ms": 100,
            "startup_seconds": 10,
        }
        parent = self.waiting_task(wait=False)
        try:
            for iteration in range(3):
                previous = self.pid()
                source.write_text(str(iteration))
                self.wait_until(lambda: self.pid() != previous)
                # Observe several monitor ticks after each real backend restart.
                time.sleep(2)
                self.assertIsNone(parent.poll())
                self.assertTrue(self.alive(self.pid()))
            os.kill(self.pid(), signal.SIGKILL)
            self.assertEqual(parent.wait(timeout=15), 1)
            self.assertFalse(self.alive(int((self.root / "waiting").read_text())))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()

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

    def test_native_http_readiness_admits_only_the_expected_status(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        worker = self.root / "http-worker.py"
        worker.write_text("""import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(int(Path('http-status').read_text()))
        self.end_headers()
Path('pid').write_text(str(os.getpid()))
HTTPServer(('127.0.0.1', int(__import__('sys').argv[1])), Handler).serve_forever()
""")
        service = self.plan["services"]["worker"]
        service["command"] = self.command([sys.executable, str(worker), str(port)])
        service["readiness"] = {
            "http_get": {"port": port, "path": "/health", "status_code": 204},
            "period_seconds": 1,
            "timeout_seconds": 1,
            "failure_threshold": 3,
        }
        self.plan["task"] = self.command(
            [sys.executable, "-c", "from pathlib import Path; Path('task-ran').touch()"]
        )
        for status in (200, 204):
            with self.subTest(status=status):
                (self.root / "http-status").write_text(str(status))
                self.run_control("run", check=0 if status == 204 else 1)
                self.assertEqual((self.root / "task-ran").exists(), status == 204)
                self.assertFalse(self.alive(self.pid()))
                compose = json.loads((self.state / "compose.json").read_text())
                probe = compose["processes"]["worker"]["readiness_probe"]
                self.assertNotIn("exec", probe)
                self.assertEqual(probe["http_get"]["host"], "127.0.0.1")
                self.assertEqual(probe["timeout_seconds"], 1)

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

    def assert_single_shutdown_signal(self, backend):
        worker = self.root / "worker.py"
        worker.write_text("""import os,signal,time
from pathlib import Path
count = 0
def stop(*_):
    global count
    count += 1
    Path('signals').write_text(str(count))
    time.sleep(.75)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path('pid').write_text(str(os.getpid()))
Path('ready').touch()
while True: time.sleep(.1)
""")
        self.plan["services"]["worker"]["shutdown_seconds"] = 3
        self.run_control("up", check=0)
        owner = json.loads((self.state / "worker.owner.json").read_text())["identity"][
            "pid"
        ]
        if backend:
            import hashlib

            socket = (
                Path("/tmp").resolve()
                / f"chainman-control-{os.geteuid()}"
                / (hashlib.sha256(str(self.state).encode()).hexdigest()[:24] + ".sock")
            )
            command = [
                BACKEND,
                "--use-uds",
                "--unix-socket",
                str(socket),
                "process",
                "stop",
                "worker",
            ]
        else:
            command = [CONTROL, "stop", str(self.state)]
        # Linux permits a deterministic duplicate-delivery check: pause the
        # owner so direct group delivery cannot coalesce with its later forward.
        # On the Darwin qualification runner, even a standalone Go signal.Notify
        # program loses SIGTERM queued between SIGSTOP/SIGCONT. Keep graceful
        # single-signal shutdown covered there without that platform assumption.
        pause_owner = platform.system() == "Linux"
        if pause_owner:
            os.kill(owner, signal.SIGSTOP)
        stopper = None
        try:
            stopper = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            if pause_owner:
                time.sleep(0.25)
                self.assertFalse(
                    (self.root / "signals").exists(),
                    "shutdown bypassed the forwarding owner",
                )
        finally:
            if pause_owner:
                os.kill(owner, signal.SIGCONT)
            if stopper is not None:
                stdout, stderr = stopper.communicate(timeout=15)
                self.assertEqual(stopper.returncode, 0, stdout + stderr)
        self.wait_until(lambda: not self.alive(self.pid()))
        self.assertEqual((self.root / "signals").read_text(), "1")

    def test_controller_sends_single_shutdown_signal(self):
        self.assert_single_shutdown_signal(backend=False)

    def test_backend_sends_single_shutdown_signal(self):
        self.assert_single_shutdown_signal(backend=True)

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
            [
                sys.executable,
                "-c",
                "print('current startup problem',flush=True); raise SystemExit(3)",
            ]
        )
        (self.state / "services.log").write_text("obsolete error from a previous run\n")
        self.plan["task"] = self.command(
            [sys.executable, "-c", "from pathlib import Path; Path('task-ran').touch()"]
        )
        result = self.run_control("run", check=1)
        self.assertIn("exited before readiness", result.stderr)
        self.assertIn("exit code 3", result.stderr)
        self.assertIn("current startup problem", result.stderr)
        self.assertNotIn("obsolete error", result.stderr)
        self.assertIn(str(self.state / "services.log"), result.stderr)
        self.assertFalse((self.root / "task-ran").exists())
        self.assertFalse(list(self.state.glob("*.lease")))

    def test_readiness_failure_is_bounded_and_cleans_up(self):
        self.plan["services"]["worker"]["readiness"]["command"] = self.command(
            [sys.executable, "-c", "raise SystemExit(1)"]
        )
        self.run_control("run", check=1, timeout=25)
        self.assertFalse(self.alive(self.pid()))

    def interrupt_unready_startup(self, *, shared=False, interrupt=None):
        self.plan["services"]["worker"]["readiness"].update(
            command=self.command([sys.executable, "-c", "raise SystemExit(1)"]),
            failure_threshold=120,
        )
        self.plan["task"] = self.command(
            [sys.executable, "-c", "from pathlib import Path; Path('task-ran').touch()"]
        )
        if shared:
            self.shared_resource()
        self.path.write_text(json.dumps(self.plan))
        with (self.base / "startup.log").open("w") as log:
            parent = subprocess.Popen(
                [CONTROL, "run", str(self.path)], stdout=log, stderr=log
            )
            try:
                self.wait_file(self.root / "pid")
                started = time.monotonic()
                snapshot = self.run_control("status", check=0, timeout=3)
                data = json.loads(snapshot.stdout)
                self.assertTrue(data["busy"])
                self.assertFalse(data["complete"])
                self.assertIsNone(data["recovery_required"])
                if shared:
                    self.assertTrue(data["resources"][0]["busy"])
                self.assertLess(time.monotonic() - started, 3)
                if interrupt is None:
                    self.run_control("stop", check=0, timeout=8)
                    expected = 1
                else:
                    parent.send_signal(interrupt)
                    expected = 128 + interrupt
                self.assertEqual(parent.wait(timeout=8), expected)
                self.assertLess(time.monotonic() - started, 8)
                self.assertFalse(self.alive(self.pid()))
                self.assertFalse((self.root / "task-ran").exists())
                self.assertFalse(list(self.state.glob("*.lease")))
                log.flush()
                self.assertNotIn(
                    "Service startup failed", (self.base / "startup.log").read_text()
                )
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)

    def test_stop_interrupts_unready_local_service_startup(self):
        self.interrupt_unready_startup()
        self.plan["services"]["worker"]["readiness"]["command"] = self.command(
            [shutil.which("test"), "-f", str(self.root / "ready")]
        )
        # A completed stop must not permanently fence later starts.
        self.run_control("up", check=0)
        self.assertTrue(self.alive(self.pid()))

    def test_stop_interrupts_unready_repository_service_startup(self):
        self.interrupt_unready_startup(shared=True)

    def test_sigint_cleans_unready_local_service_startup(self):
        self.interrupt_unready_startup(interrupt=signal.SIGINT)

    def test_sigterm_cleans_unready_repository_service_startup(self):
        self.interrupt_unready_startup(shared=True, interrupt=signal.SIGTERM)

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

    def test_task_entry_preserves_lookup_and_inherited_directory(self):
        entry = self.root / "task-entry"
        entry.write_text("#!/bin/sh\ntouch project-ran\nexit 7\n")
        entry.chmod(0o700)
        for inherit in (False, True):
            with self.subTest(inherit=inherit):
                self.plan["task"] = self.command(
                    [str(entry) if inherit else entry.name]
                )
                if inherit:
                    self.plan["task"]["directory"] = ""
                self.path.write_text(json.dumps(self.plan))
                result = subprocess.run(
                    [CONTROL, "run", str(self.path)],
                    cwd=self.base if inherit else self.root,
                    env=dict(os.environ, PATH="." + os.pathsep + os.environ["PATH"]),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 7 if inherit else 1, result.stderr)
                if inherit:
                    self.assertTrue((self.base / "project-ran").exists())
                else:
                    self.assertIn("relative to current directory", result.stderr)
                self.assertFalse((self.root / "project-ran").exists())

    def test_owned_task_overrides_inherited_lease_environment(self):
        self.plan.update(own_task=True, task_shutdown_seconds=1)
        self.plan["task"] = self.command(
            [
                sys.executable,
                "-c",
                "import os; from pathlib import Path; "
                "assert 'CHAINMAN_SERVICE_LEASE_FDS' not in os.environ; "
                "assert Path('ready').is_file(); "
                "Path('task-result').write_text(os.environ['DECLARED']); "
                "raise SystemExit(7)",
            ]
        )
        # The owned anchor receives the new lease; project code must not
        # advertise its host descriptors through a subsequent Nix/engine entry.
        self.plan["task"]["environment"] = {"DECLARED": "project override"}
        self.path.write_text(json.dumps(self.plan))
        result = subprocess.run(
            [CONTROL, "run", str(self.path)],
            env=dict(
                os.environ,
                CHAINMAN_SERVICE_LEASE_FDS="stale caller metadata",
                DECLARED="caller value",
            ),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual((self.root / "task-result").read_text(), "project override")
        self.assertFalse(list(self.state.glob("*.lease*")))

    def test_task_identity_is_published_before_project_entry(self):
        arguments = ["two words", "", "$(touch injected)", "'quoted'"]
        script = """import json,os,sys
from pathlib import Path
state = Path(os.environ['LEASE_STATE'])
lease, = state.glob('*.lease')
assert json.loads(lease.read_text())['task_receipt']
assert lease.stat().st_ino == os.fstat(3).st_ino
identity = json.loads(Path(str(lease) + '.task.json').read_text())
assert identity['pid'] == os.getpid()
Path('task-result').write_text(json.dumps([sys.argv[1:], sys.stdin.read(), os.environ['LITERAL']]))
raise SystemExit(7)
"""
        self.plan["task"] = self.command([sys.executable, "-c", script, *arguments])
        self.plan["task"]["environment"] = {
            "LEASE_STATE": str(self.state),
            "LITERAL": "value with spaces $()",
        }
        self.path.write_text(json.dumps(self.plan))
        result = subprocess.run(
            [CONTROL, "run", str(self.path)],
            input="literal input\nsecond line\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(
            json.loads((self.root / "task-result").read_text()),
            [arguments, "literal input\nsecond line\n", "value with spaces $()"],
        )
        self.assertFalse((self.root / "injected").exists())
        self.assertFalse(list(self.state.glob("*.lease*")))

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
