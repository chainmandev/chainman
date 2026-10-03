"""A noisy PTY child must terminate without an undrained terminal deadlock."""

import errno
import os
import pty
import subprocess
import sys
import termios
import unittest
from unittest.mock import patch

from terminal_fixture import terminal_modes, wait_terminal


class TerminalFixtureTests(unittest.TestCase):
    def test_job_control_fixture_retries_only_interrupted_mode_changes(self):
        from test_storage import TERMINAL_BODY

        for number in (errno.EINTR, errno.EIO):
            with (
                self.subTest(errno=number),
                patch("termios.tcgetattr", return_value=[0, 0, 0, 0]),
                patch(
                    "termios.tcsetattr",
                    side_effect=[termios.error(number, "fixture"), None],
                ) as setting,
            ):
                if number == errno.EINTR:
                    with self.assertRaises(SystemExit) as exited:
                        exec(
                            TERMINAL_BODY,
                            {"input": lambda: "finish", "print": lambda *a, **k: None},
                        )
                    self.assertEqual(exited.exception.code, 7)
                    self.assertEqual(setting.call_count, 2)
                else:
                    with self.assertRaises(termios.error):
                        exec(TERMINAL_BODY, {})
                    self.assertEqual(setting.call_count, 1)

    def test_mode_comparison_excludes_only_darwin_pending_input(self):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
        finally:
            os.close(master)
            os.close(slave)
        original[3] &= ~termios.PENDIN
        pending = [*original]
        pending[3] |= termios.PENDIN
        with patch("terminal_fixture.sys.platform", "darwin"):
            with patch("terminal_fixture.termios.tcgetattr", return_value=pending):
                self.assertEqual(terminal_modes(0), original)
            for index in range(6):
                changed = [*original]
                changed[index] ^= termios.ECHO if index == 3 else 1
                with self.subTest(field=index):
                    with patch(
                        "terminal_fixture.termios.tcgetattr", return_value=changed
                    ):
                        self.assertNotEqual(terminal_modes(0), original)
            changed = [*original[:6], [*original[6]]]
            changed[6][termios.VINTR] = b"x"
            with patch("terminal_fixture.termios.tcgetattr", return_value=changed):
                self.assertNotEqual(terminal_modes(0), original)
        pending = [*original]
        pending[3] |= termios.PENDIN
        with (
            patch("terminal_fixture.sys.platform", "linux"),
            patch("terminal_fixture.termios.tcgetattr", return_value=pending),
        ):
            self.assertNotEqual(terminal_modes(0), original)

    def test_raw_round_trip_preserves_configured_modes(self):
        import tty

        master, slave = pty.openpty()
        try:
            before = termios.tcgetattr(slave)
            expected = terminal_modes(slave)
            tty.setraw(slave)
            termios.tcsetattr(slave, termios.TCSANOW, before)
            after = termios.tcgetattr(slave)
            difference = before[3] ^ after[3]
            # Keep the native kernel observation in CI without hiding any mode.
            print(
                f"RAW-ROUND-TRIP lflag-difference={difference:#x} PENDIN={termios.PENDIN:#x}"
            )
            self.assertEqual(terminal_modes(slave), expected, (before, after))
        finally:
            os.close(master)
            os.close(slave)

    def test_wait_drains_output_larger_than_the_terminal_buffer(self):
        master, slave = pty.openpty()
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 262144); sys.exit(17)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        try:
            status, output = wait_terminal(child, master, 5)
            self.assertEqual(status, 17)
            self.assertGreater(len(output), 131072)
            self.assertEqual(set(output), {ord("x")})
        finally:
            os.close(master)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
