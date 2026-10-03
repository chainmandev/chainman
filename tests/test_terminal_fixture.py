"""A noisy PTY child must terminate without an undrained terminal deadlock."""

import errno
import os
import pty
import select
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch

from terminal_fixture import terminal_modes, wait_terminal, write_terminal


class TerminalFixtureTests(unittest.TestCase):
    def test_write_deadline_restores_descriptor_mode(self):
        import tty

        master, slave = pty.openpty()
        try:
            tty.setraw(slave)
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                write_terminal(master, b"x" * 262144, 0.1)
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(os.get_blocking(master))
        finally:
            os.close(master)
            os.close(slave)

    def test_write_drains_bidirectional_terminal_backpressure(self):
        master, slave = pty.openpty()
        size = 262144
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,tty; tty.setraw(0); os.write(1,b'READY'); "
                f"remaining={size}\n"
                "while remaining:\n"
                " chunk=os.read(0,min(1024,remaining)); remaining-=len(chunk); "
                "os.write(1,chunk)\n"
                "os.write(1,b'DONE'); assert os.read(0,4)==b'exit'; raise SystemExit(17)",
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
        )
        os.close(slave)
        try:
            self.assertTrue(select.select([master], [], [], 5)[0])
            self.assertEqual(os.read(master, 5), b"READY")
            output = write_terminal(master, b"x" * size, 5)
            self.assertTrue(os.get_blocking(master))
            deadline = time.monotonic() + 5
            while not output.endswith(b"DONE"):
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0)
                self.assertTrue(select.select([master], [], [], remaining)[0])
                output += os.read(master, 65536)
            self.assertEqual(output, b"x" * size + b"DONE")
            write_terminal(master, b"exit", 5)
            self.assertEqual(wait_terminal(child, master, 5)[0], 17)
        finally:
            if child.poll() is None:
                child.kill()
                wait_terminal(child, master, 5)
            os.close(master)

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
