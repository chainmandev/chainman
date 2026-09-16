"""Exercise real controlling terminals while command stdin remains a pipe."""

import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SetupTerminalTests(unittest.TestCase):
    def run_prompt(self, mode, answer):
        with tempfile.TemporaryDirectory(prefix="setup tty ") as directory:
            root = Path(directory)
            engine = root / "engine"
            engine.write_text(f"""#!{sys.executable}
import os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / "scripts")!r})
import setup_readiness
env = {{}}
for arg in sys.argv:
    if arg.startswith('type=bind,src='):
        env['CHAINMAN_SETUP_CHANNEL'] = arg.split('src=',1)[1].split(',dst=',1)[0]
try:
    setup_readiness.authorize({{'javascript':'stale'}}, env)
except ValueError as error:
    print(error, file=sys.stderr); sys.exit(2)
Path({str(root / "input")!r}).write_bytes(sys.stdin.buffer.read())
""")
            engine.chmod(0o755)
            master, slave = pty.openpty()

            def terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            command = (
                ["sh", str(ROOT / "bootstrap/setup-prompt.sh"), str(engine)]
                if mode == "container"
                else [str(engine)]
            )
            child = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(slave,),
                preexec_fn=terminal,
                env=dict(os.environ, CHAINMAN_SETUP="prompt"),
            )
            os.close(slave)
            try:
                output = b""
                deadline = time.monotonic() + 8
                while b"[Y/n]" not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output += os.read(master, 4096)
                        except OSError:
                            break
                    if child.poll() is not None:
                        break
                if b"[Y/n]" not in output:
                    child.kill()
                    _, error = child.communicate(timeout=3)
                    self.fail(f"No prompt: {output!r}; {error!r}")
                if answer is None:
                    child.send_signal(signal.SIGTERM)
                else:
                    os.write(master, answer)
                _, error = child.communicate(
                    b"refs/heads/master 123 remote 456\n", timeout=5
                )
                if answer in (b"y\n", b"\n"):
                    self.assertEqual(child.returncode, 0, error)
                    self.assertEqual(
                        (root / "input").read_bytes(),
                        b"refs/heads/master 123 remote 456\n",
                    )
                else:
                    self.assertNotEqual(child.returncode, 0)
                    self.assertFalse((root / "input").exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
                os.close(master)

    def test_host_accepts_without_consuming_git_input(self):
        self.run_prompt("host", b"y\n")

    def test_container_relay_accepts_default_without_consuming_git_input(self):
        self.run_prompt("container", b"\n")

    def test_container_decline(self):
        self.run_prompt("container", b"n\n")

    def test_interactive_container_owns_terminal_input(self):
        with tempfile.TemporaryDirectory(prefix="setup interactive tty ") as directory:
            root = Path(directory)
            engine = root / "engine"
            engine.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / "scripts")!r})
import setup_readiness
# An attached engine owns the terminal. The relay must not add a second reader.
assert not any(arg.startswith('type=bind,src=') for arg in sys.argv)
setup_readiness.authorize({{'javascript':'stale'}}, {{}})
Path({str(root / "input")!r}).write_bytes(sys.stdin.buffer.readline())
""")
            engine.chmod(0o755)
            master, slave = pty.openpty()

            def terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

            child = subprocess.Popen(
                ["sh", str(ROOT / "bootstrap/setup-prompt.sh"), str(engine)],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                preexec_fn=terminal,
                env=dict(os.environ, CHAINMAN_SETUP="prompt"),
            )
            os.close(slave)
            try:
                output = b""
                deadline = time.monotonic() + 5
                while b"[Y/n]" not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], 0.1)[0]:
                        try:
                            output += os.read(master, 4096)
                        except OSError:
                            break
                self.assertIn(b"[Y/n]", output)
                os.write(master, b"y\nliteral terminal input\n")
                self.assertEqual(child.wait(timeout=3), 0)
                self.assertEqual(
                    (root / "input").read_bytes(), b"literal terminal input\n"
                )
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                os.close(master)

    def test_container_interruption(self):
        self.run_prompt("container", None)
