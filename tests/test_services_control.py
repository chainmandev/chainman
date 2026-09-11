"""Native ownership contracts against the actual Process Compose backend."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

CONTROL = os.environ.get("CHAINMAN_TEST_CONTROL")
BACKEND = os.environ.get("CHAINMAN_TEST_PROCESS_COMPOSE")


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

    def test_readiness_exit_status_and_cleanup(self):
        self.run_control("run", check=7)
        self.assertFalse(self.alive(self.pid()))

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
