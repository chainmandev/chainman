"""Bounded process waits that keep a fixture's pseudo-terminal draining."""

import os
import select
import subprocess
import time


def wait_terminal(child, master, timeout):
    # Darwin can wait for pending output to drain while closing the terminal.
    # Waiting without reading can also block any child that fills the PTY.
    deadline = time.monotonic() + timeout
    output = bytearray()
    while child.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(child.args, timeout, output=bytes(output))
        if select.select([master], [], [], min(0.1, remaining))[0]:
            try:
                data = os.read(master, 8192)
            except OSError:
                data = b""
            if data:
                output.extend(data)
                continue
            return child.wait(timeout=remaining), bytes(output)
    return child.returncode, bytes(output)
