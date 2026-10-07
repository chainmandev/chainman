"""Forensic fixture signals must remain inside the disposable PTY session."""

import errno
import json
import signal
import unittest
from unittest.mock import call, patch

from terminal_fixture import terminal_outer_stack


def snapshot():
    return {
        "session": 100,
        "processes": [
            {
                "pid": 121,
                "parent": 120,
                "group": 120,
                "state": "S",
                "executable": "chainman-control",
            },
            {
                "pid": 122,
                "parent": 121,
                "group": 122,
                "state": "T",
                "executable": "chainman-control",
            },
        ],
    }


class TerminalDiagnosticsTests(unittest.TestCase):
    def test_changed_session_ownership_prevents_any_signal(self):
        with (
            patch("terminal_fixture.os.getsid", return_value=999),
            patch("terminal_fixture.os.kill") as kill,
            patch("terminal_fixture.os.killpg") as kill_group,
        ):
            result = json.loads(terminal_outer_stack(7, snapshot()))
        self.assertIn("ownership changed", result["skipped"])
        kill.assert_not_called()
        kill_group.assert_not_called()

    def test_sequence_stop_without_unique_outer_anchor_pair_is_skipped(self):
        data = snapshot()
        data["processes"][1]["group"] = 121
        with patch("terminal_fixture.os.kill") as kill:
            result = json.loads(terminal_outer_stack(7, data))
        self.assertIn("No unique", result["skipped"])
        kill.assert_not_called()

    def test_capture_is_bounded_and_cleanup_only_addresses_owned_processes(self):
        with (
            patch("terminal_fixture.os.getsid", return_value=100),
            patch(
                "terminal_fixture.os.getpgid",
                side_effect=lambda pid: {121: 120, 122: 122}[pid],
            ),
            patch("terminal_fixture.os.kill") as kill,
            patch("terminal_fixture.os.killpg") as kill_group,
            patch("terminal_fixture.time.monotonic", return_value=0),
            patch("terminal_fixture.select.select", return_value=([7], [], [])),
            patch("terminal_fixture.os.read", return_value=b"x" * 4096),
        ):
            result = json.loads(terminal_outer_stack(7, snapshot()))
        self.assertEqual(result["stack_bytes"], 65536)
        self.assertEqual(result["outer_pid"], 121)
        self.assertEqual(
            kill.call_args_list,
            [call(121, signal.SIGQUIT), call(121, signal.SIGKILL)],
        )
        kill_group.assert_called_once_with(122, signal.SIGKILL)

    def test_unavailable_process_queries_preserve_diagnostic_error(self):
        with (
            patch("terminal_fixture.os.getsid", side_effect=ProcessLookupError),
            patch("terminal_fixture.os.kill") as kill,
        ):
            result = json.loads(terminal_outer_stack(7, snapshot()))
        self.assertIn("ownership changed", result["skipped"])
        kill.assert_not_called()

    def test_closed_pty_retains_stack_before_cleanup(self):
        with (
            patch("terminal_fixture.os.getsid", return_value=100),
            patch(
                "terminal_fixture.os.getpgid",
                side_effect=lambda pid: {121: 120, 122: 122}[pid],
            ),
            patch("terminal_fixture.os.kill"),
            patch("terminal_fixture.os.killpg"),
            patch("terminal_fixture.time.monotonic", return_value=0),
            patch("terminal_fixture.select.select", return_value=([7], [], [])),
            patch(
                "terminal_fixture.os.read",
                side_effect=[b"fixture Go stack", OSError(errno.EIO, "fixture closed")],
            ),
        ):
            result = json.loads(terminal_outer_stack(7, snapshot()))
        self.assertEqual(result["stack"], "fixture Go stack")
        self.assertNotIn("error", result)
