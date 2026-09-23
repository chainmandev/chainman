"""A noisy PTY child must terminate without an undrained terminal deadlock."""

import os
import pty
import subprocess
import sys
import unittest

from terminal_fixture import wait_terminal


class TerminalFixtureTests(unittest.TestCase):
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
