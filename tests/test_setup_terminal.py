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

from terminal_fixture import wait_terminal

ROOT = Path(__file__).resolve().parents[1]
CONTROL = os.environ.get("CHAINMAN_TEST_HOOK_CONTROL")


class SetupTerminalTests(unittest.TestCase):
    def test_owned_container_without_private_cleanup_becomes_engine(self):
        with tempfile.TemporaryDirectory(prefix="setup engine signals ") as directory:
            root = Path(directory)
            engine = root / "engine"
            engine.write_text(f"""#!{sys.executable}
import json,os,signal,sys,time
from pathlib import Path
signals = []
for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, lambda number, _: signals.append(number))
assert sys.argv[1:] == ['run', 'literal value', '', '$(literal)']
assert sys.stdin.readline() == 'preserved input\\n'
Path({str(root / "ready")!r}).write_text(str(os.getpid()))
while not signals: time.sleep(.01)
time.sleep(.3)
print(json.dumps(signals), flush=True)
raise SystemExit(128 + signals[0])
""")
            engine.chmod(0o755)
            for number, group in ((signal.SIGTERM, False), (signal.SIGINT, True)):
                with self.subTest(signal=number, group=group):
                    ready = root / "ready"
                    ready.unlink(missing_ok=True)
                    child = subprocess.Popen(
                        [
                            "sh",
                            str(ROOT / "bootstrap/setup-prompt.sh"),
                            str(engine),
                            "literal value",
                            "",
                            "$(literal)",
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,
                        env={
                            "CHAINMAN_CONTAINER_OWNER": "a" * 32,
                            **{
                                key: value
                                for key, value in os.environ.items()
                                if not key.startswith("CHAINMAN_")
                            },
                        },
                    )
                    try:
                        child.stdin.write("preserved input\n")
                        child.stdin.flush()
                        deadline = time.monotonic() + 5
                        while not ready.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertEqual(int(ready.read_text()), child.pid)
                        if group:
                            os.killpg(child.pid, number)
                        else:
                            child.send_signal(number)
                        output, error = child.communicate(timeout=5)
                        self.assertEqual(child.returncode, 128 + number, error)
                        self.assertEqual(output.strip(), str([int(number)]))
                    finally:
                        if child.poll() is None:
                            child.kill()
                        child.communicate(timeout=5)

    def test_unowned_container_keeps_bounded_client_shutdown(self):
        with tempfile.TemporaryDirectory(
            prefix="bootstrap stalled engine "
        ) as directory:
            root = Path(directory)
            engine = root / "engine"
            ready = root / "ready"
            engine.write_text(f"""#!{sys.executable}
import os,signal,time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
time.sleep(60)
""")
            engine.chmod(0o755)
            child = subprocess.Popen(
                ["sh", str(ROOT / "bootstrap/setup-prompt.sh"), str(engine)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("CHAINMAN_")
                },
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                child.terminate()
                _, error = child.communicate(timeout=8)
                self.assertEqual(child.returncode, 143, error)
                self.assertIn(b"owned helper did not stop", error)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)
                if ready.exists():
                    try:
                        os.kill(int(ready.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

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
        env['CHAINMAN_SETUP_CHANNEL'] = str(Path(arg.split('src=',1)[1].split(',dst=',1)[0]).parent)
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

            private = root / "private transport"
            private.mkdir()
            command = (
                [
                    "sh",
                    str(ROOT / "bootstrap/setup-prompt.sh"),
                    "--cleanup-directory",
                    str(private),
                    str(engine),
                ]
                if mode == "container"
                else [str(engine)]
            )
            child = subprocess.Popen(
                (
                    [CONTROL, "setup-consent", *command]
                    if mode == "container"
                    else command
                ),
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
                try:
                    child.stdin.write(b"refs/heads/master 123 remote 456\n")
                    child.stdin.close()
                except BrokenPipeError:
                    pass
                child.stdin = None
                wait_terminal(child, master, 12)
                _, error = child.communicate(timeout=3)
                if mode == "container":
                    self.assertFalse(private.exists())
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
                os.close(master)
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=3)

    def test_host_accepts_without_consuming_git_input(self):
        self.run_prompt("host", b"y\n")

    def test_background_host_refuses_instead_of_stopping_on_terminal_read(self):
        master, slave = pty.openpty()

        def terminal():
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        program = (
            f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
            "import setup_readiness; "
            "setup_readiness.authorize({'fixture': 'stale'}, {})"
        )
        parent = (
            "import subprocess,sys; "
            "sys.exit(subprocess.run(sys.argv[1:], process_group=0, timeout=3).returncode)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", parent, sys.executable, "-c", program],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=terminal,
            pass_fds=(slave,),
        )
        os.close(slave)
        try:
            _, error = child.communicate(timeout=5)
            self.assertNotEqual(child.returncode, 0)
            self.assertIn(b"Setup is not ready", error)
            self.assertNotIn(b"TimeoutExpired", error)
        finally:
            os.close(master)
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    @unittest.skipUnless(CONTROL, "run just control-test for native consent")
    def test_container_relay_accepts_default_without_consuming_git_input(self):
        self.run_prompt("container", b"\n")

    @unittest.skipUnless(CONTROL, "run just control-test for native consent")
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
                self.assertEqual(wait_terminal(child, master, 3)[0], 0)
                self.assertEqual(
                    (root / "input").read_bytes(), b"literal terminal input\n"
                )
            finally:
                os.close(master)
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)

    @unittest.skipUnless(CONTROL, "run just control-test for native consent")
    def test_container_interruption(self):
        self.run_prompt("container", None)

    @unittest.skipUnless(CONTROL, "run just control-test for native consent")
    def test_unresponsive_engine_before_prompt_is_bounded_and_reaped(self):
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private = root / "private"
                private.mkdir()
                engine = root / "engine"
                ready = root / "ready"
                engine.write_text(f"""#!{sys.executable}
import os, signal, time
from pathlib import Path
for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
time.sleep(30)
""")
                engine.chmod(0o755)
                master, slave = pty.openpty()

                def terminal():
                    os.setsid()
                    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

                child = subprocess.Popen(
                    [
                        CONTROL,
                        "setup-consent",
                        "sh",
                        str(ROOT / "bootstrap/setup-prompt.sh"),
                        "--cleanup-directory",
                        str(private),
                        str(engine),
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(slave,),
                    preexec_fn=terminal,
                    env=dict(os.environ, CHAINMAN_SETUP="prompt", TMPDIR=str(root)),
                )
                os.close(slave)
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    child.send_signal(sig)
                    _, error = child.communicate(timeout=12)
                    self.assertEqual(child.returncode, 128 + sig, error)
                    self.assertFalse(private.exists())
                    self.assertEqual(list(root.glob("chainman-consent-*")), [])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(ready.read_text()), 0)
                finally:
                    os.close(master)
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=3)
                    if ready.exists():
                        try:
                            os.kill(int(ready.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
