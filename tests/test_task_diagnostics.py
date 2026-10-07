"""Task timeout diagnosis must omit unrelated processes and retain failures."""

import json
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_diagnostics import task_process_snapshot


class TaskDiagnosticsTests(unittest.TestCase):
    def test_only_outer_descendants_and_owned_group_are_recorded(self):
        result = subprocess.CompletedProcess(
            [],
            0,
            "40 1 40 S /private/runner\n"
            "41 40 40 S /store/chainman-control\n"
            "42 41 42 T /store/chainman-control\n"
            "43 42 42 T /store/chainman-control\n"
            "44 1 42 T /store/python3\n"
            "45 40 40 S /private/unrelated-secret\n",
        )
        with patch("task_diagnostics.subprocess.run", return_value=result) as run:
            snapshot = json.loads(task_process_snapshot(41, 42))
        self.assertEqual(
            [row["pid"] for row in snapshot["processes"]], [41, 42, 43, 44]
        )
        self.assertNotIn("unrelated-secret", json.dumps(snapshot))
        self.assertEqual(snapshot["processes"][1]["state"], "T")
        argv = run.call_args.args[0]
        self.assertIn("pid=,ppid=,pgid=,stat=,comm=", argv)
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_unavailable_process_query_does_not_replace_failure(self):
        with patch("task_diagnostics.subprocess.run", side_effect=OSError):
            snapshot = json.loads(task_process_snapshot(41, 42))
        self.assertEqual(snapshot["error"], "OSError")
        self.assertEqual(snapshot["processes"], [])

    def test_missing_owned_identity_does_not_query_other_processes(self):
        with patch("task_diagnostics.subprocess.run") as run:
            snapshot = json.loads(task_process_snapshot(41, None))
        run.assert_not_called()
        self.assertEqual(snapshot["error"], "ValueError")

    def test_process_rows_are_bounded(self):
        result = subprocess.CompletedProcess(
            [],
            0,
            "".join(f"{pid} 41 42 T /store/python3\n" for pid in range(42, 112)),
        )
        with patch("task_diagnostics.subprocess.run", return_value=result):
            snapshot = json.loads(task_process_snapshot(41, 42))
        self.assertEqual(len(snapshot["processes"]), 64)
        self.assertTrue(snapshot["truncated"])

    def test_timeout_snapshot_reraises_the_original_native_assertion(self):
        from test_services_control import ServiceControlTests

        failure = subprocess.TimeoutExpired(["fixture-control"], 3)
        client = Mock(pid=41)
        client.poll.return_value = 143
        client.communicate.side_effect = failure
        fixture = ServiceControlTests(
            "test_stopped_task_owner_handles_cancellation_without_kill_timeout"
        )
        with tempfile.TemporaryDirectory(
            prefix="chainman task diagnostic "
        ) as directory:
            fixture.base = Path(directory)
            fixture.root = fixture.base / "project"
            fixture.root.mkdir()
            receipt = fixture.base / "stopped-owner"
            receipt.mkdir()
            (receipt / "task.owner.json").write_text(
                json.dumps({"identity": {"pid": 42}})
            )
            output = io.StringIO()
            with (
                patch.object(fixture, "wait_file"),
                patch("test_services_control.subprocess.Popen", return_value=client),
                patch("test_services_control.os.killpg"),
                patch(
                    "test_services_control.task_process_snapshot", return_value="{}"
                ) as snapshot,
                contextlib.redirect_stderr(output),
                self.assertRaises(subprocess.TimeoutExpired) as raised,
            ):
                fixture.test_stopped_task_owner_handles_cancellation_without_kill_timeout()
        self.assertIs(raised.exception, failure)
        client.communicate.assert_called_once_with(timeout=3)
        snapshot.assert_called_once_with(41, 42)
        self.assertIn("Owned stopped-task cancellation snapshot", output.getvalue())
