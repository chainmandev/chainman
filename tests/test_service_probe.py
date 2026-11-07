"""Real application status and lease boundaries of the internal probe worker."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import storage
import toolchain as tc


class ServiceProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman probe worker ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(
            os.environ,
            XDG_CACHE_HOME=str(self.root / "cache"),
            XDG_CONFIG_HOME=str(self.root / "config"),
            XDG_DATA_HOME=str(self.root / "data"),
            XDG_STATE_HOME=str(self.root / "state"),
        )
        environment.start()
        self.addCleanup(environment.stop)

    def command(self, seconds, body):
        return [
            sys.executable,
            "-E",
            "-s",
            "-S",
            "-B",
            str(SCRIPTS / "service_probe.py"),
            str(seconds),
            sys.executable,
            "-c",
            body,
        ]

    def test_actual_timeout_reaps_direct_application(self):
        result = tc.managed_run(
            self.command(
                2,
                "import os,time; from pathlib import Path; Path('pid').write_text(str(os.getpid()));time.sleep(60)",
            ),
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        pid = int((self.root / "pid").read_text())
        if Path("/proc").exists():
            self.assertFalse(Path(f"/proc/{pid}").exists())
        else:
            self.assertRaises(ProcessLookupError, os.kill, pid, 0)

    def test_real_application_exit_codes_are_not_timeouts(self):
        for body, expected in (
            ("raise SystemExit(0)", 0),
            ("raise SystemExit(23)", 23),
            ("raise SystemExit(137)", 137),
            ("import os,signal;os.kill(os.getpid(),signal.SIGKILL)", 137),
        ):
            with self.subTest(body=body):
                result = tc.managed_run(
                    self.command(10, body),
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_worker_and_payload_retain_real_lease_after_parent_release(self):
        pool = self.root / storage.POOL
        release = self.root / "release"
        payload = """import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import storage
paths,fds=storage.inherited()
entry=Path(sys.argv[2])
fd=fds[paths.index(entry)]
assert os.path.samestat(os.fstat(fd),(entry/'.lease').stat())
Path('held.next').write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}))
os.replace('held.next','held')
while not Path('release').exists():time.sleep(.02)
"""
        with storage.use(pool, "b" * 40, "runtime") as entry:
            command = [
                sys.executable,
                "-E",
                "-s",
                "-S",
                "-B",
                str(SCRIPTS / "service_probe.py"),
                "10",
                sys.executable,
                "-c",
                payload,
                str(SCRIPTS),
                str(entry),
            ]
            process = subprocess.Popen(
                command,
                **tc.managed_options(
                    {
                        "cwd": self.root,
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.PIPE,
                        "text": True,
                    }
                ),
            )
            self.addCleanup(release.touch)
            try:
                deadline = time.monotonic() + 10
                while not (self.root / "held").exists():
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.02)
                proof = json.loads((self.root / "held").read_text())
                self.assertEqual(proof["group"], os.getpgrp())
            except BaseException:
                release.touch()
                process.communicate(timeout=12)
                raise
        try:
            rows, removed = storage.collect(pool, apply=True, all_idle=True)
            self.assertFalse(removed)
            self.assertTrue(
                any(row["path"] == str(entry) and row["active"] for row in rows)
            )
            self.assertIsNone(process.poll())
        finally:
            release.touch()
            output, error = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, (output, error))
        _, removed = storage.collect(pool, apply=True, all_idle=True)
        self.assertEqual(removed, [str(entry)])


if __name__ == "__main__":
    unittest.main()
