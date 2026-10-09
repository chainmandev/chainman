"""Phase capture retains failures without waiting for inherited pipe EOF."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from control_cancellation_diagnostic import main, run_phase


class ControlCancellationDiagnosticTests(unittest.TestCase):
    def test_successful_probe_does_not_hide_failed_full_gate(self):
        with tempfile.TemporaryDirectory(prefix="chainman phase status ") as directory:
            root = Path(directory)
            with (
                patch("control_cancellation_diagnostic.ROOT", root),
                patch(
                    "control_cancellation_diagnostic.subprocess.check_output",
                    return_value="a" * 40,
                ),
                patch(
                    "control_cancellation_diagnostic.run_phase",
                    side_effect=[
                        {
                            "exit": 23,
                            "observation_timeout": False,
                            "elapsed_seconds": 1,
                        },
                        {"exit": 0, "observation_timeout": False, "elapsed_seconds": 1},
                    ],
                ),
            ):
                code = main()
            receipt = json.loads(
                (root / "cancellation-diagnostic/phases.json").read_text()
            )
        self.assertEqual(code, 23)
        self.assertEqual(receipt["ownership"]["exit"], 23)
        self.assertEqual(receipt["repetitions"]["exit"], 0)

    def test_large_output_and_nonzero_status_are_retained(self):
        with tempfile.TemporaryDirectory(prefix="chainman phase capture ") as directory:
            log = Path(directory) / "phase.log"
            result = run_phase(
                [
                    sys.executable,
                    "-c",
                    "import os,sys; assert not os.isatty(0); "
                    "assert os.getsid(0)==os.getpid(); "
                    "assert os.getpgrp()==os.getpid(); "
                    "print('x'*100000); sys.exit(23)",
                ],
                log,
                dict(os.environ),
                5,
            )
            output = log.read_text()
        self.assertEqual(result["exit"], 23)
        self.assertFalse(result["observation_timeout"])
        self.assertEqual(output, "x" * 100000 + "\n")

    def test_unresponsive_phase_has_a_failed_observation_result(self):
        with tempfile.TemporaryDirectory(prefix="chainman phase timeout ") as directory:
            log = Path(directory) / "phase.log"
            spawn = subprocess.Popen

            def start_ready_phase(*args, **kwargs):
                child = spawn(*args, **kwargs)
                try:
                    deadline = time.monotonic() + 5
                    while "PHASE READY" not in log.read_text():
                        self.assertIsNone(child.poll(), log.read_text())
                        self.assertLess(time.monotonic(), deadline, log.read_text())
                        time.sleep(0.01)
                except BaseException:
                    child.kill()
                    child.wait(timeout=5)
                    raise
                return child

            # Exercise timeout escalation only after the real child has
            # installed its signal handler. Interpreter startup can exceed
            # the short observation timeout on a busy qualification runner.
            with patch(
                "control_cancellation_diagnostic.subprocess.Popen",
                side_effect=start_ready_phase,
            ):
                result = run_phase(
                    [
                        sys.executable,
                        "-c",
                        "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                        "print('PHASE READY',flush=True); time.sleep(120)",
                    ],
                    log,
                    dict(os.environ),
                    0.2,
                    cleanup_timeout=0.1,
                )
            output = log.read_text()
        self.assertEqual(result["exit"], 124)
        self.assertTrue(result["observation_timeout"])
        self.assertIn("PHASE READY", output)
