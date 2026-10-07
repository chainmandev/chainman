"""Bounded process waits that keep a fixture's pseudo-terminal draining."""

import errno
import json
import os
import select
import signal
import subprocess
import sys
import termios
import time


def terminal_process_snapshot(master, session):
    """Read bounded failure evidence for only the disposable terminal session."""
    snapshot = {"session": session, "processes": []}
    try:
        snapshot["foreground_group"] = os.tcgetpgrp(master)
        # Explicit columns avoid argv and environment disclosure. Darwin has no
        # /proc; obtain process IDs from ps and check session ownership ourselves.
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,comm="],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        for line in result.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) != 5:
                continue
            try:
                pid, parent, group = map(int, fields[:3])
                if os.getsid(pid) != session:
                    continue
            except (OSError, ValueError):
                continue
            snapshot["processes"].append(
                {
                    "pid": pid,
                    "parent": parent,
                    "group": group,
                    "state": fields[3],
                    "executable": os.path.basename(fields[4]),
                }
            )
            if len(snapshot["processes"]) >= 64:
                snapshot["truncated"] = True
                break
    except (OSError, subprocess.SubprocessError) as error:
        # Diagnostics must never replace the original assertion failure.
        snapshot["error"] = type(error).__name__
    return json.dumps(snapshot, sort_keys=True)


def terminal_outer_stack(master, snapshot):
    """Finish an already-failed, owned fixture after a bounded Go stack dump."""
    evidence = {"diagnostic_only": True}
    try:
        session = snapshot["session"]
        rows = snapshot["processes"]
        pairs = [
            (parent, child)
            for parent in rows
            for child in rows
            if parent["executable"].startswith("chainman-contro")
            and parent["state"].startswith(("S", "R"))
            and parent["pid"] != parent["group"]
            and child["executable"].startswith("chainman-contro")
            and child["state"].startswith("T")
            and child["parent"] == parent["pid"]
            and child["pid"] == child["group"]
        ]
        if len(pairs) != 1:
            evidence["skipped"] = "No unique running outer controller/stopped anchor"
            return json.dumps(evidence)
        parent, child = pairs[0]

        def still_owned(row):
            try:
                return (
                    os.getsid(row["pid"]) == session
                    and os.getpgid(row["pid"]) == row["group"]
                )
            except OSError:
                return False

        if not all(still_owned(row) for row in (parent, child)):
            evidence["skipped"] = "Fixture process ownership changed"
            return json.dumps(evidence)
        evidence.update(outer_pid=parent["pid"], anchor_group=child["group"])
        try:
            # Go's SIGQUIT dump is fatal. This opt-in path runs only after the
            # original assertion failed and addresses only this PTY session.
            os.kill(parent["pid"], signal.SIGQUIT)
            output = bytearray()
            deadline = time.monotonic() + 2
            while len(output) < 65536 and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        chunk = os.read(master, min(4096, 65536 - len(output)))
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        break  # PTY closes after the fatal dump completes.
                    if not chunk:
                        break
                    output.extend(chunk)
            evidence["stack"] = output.decode(errors="replace")
            evidence["stack_bytes"] = len(output)
        finally:
            # Finish only the verified disposable anchor group and controller;
            # unrelated jobs, process arguments and environment are untouched.
            if still_owned(child):
                try:
                    os.killpg(child["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if still_owned(parent):
                try:
                    os.kill(parent["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except (OSError, KeyError, ValueError) as error:
        evidence["error"] = type(error).__name__
    return json.dumps(evidence)


def terminal_modes(fd):
    """Compare configured modes, not Darwin's pending-input bookkeeping bit."""
    settings = termios.tcgetattr(fd)
    if sys.platform == "darwin":
        # XNU adds PENDIN when ICANON is restored, even with no queued input.
        # The next input/read clears it. All actual mode bits remain checked.
        settings[3] &= ~termios.PENDIN
    return settings


def write_terminal(master, data, timeout):
    """Send input while draining echo/output, with a deadline on both directions."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    written = 0
    blocking = os.get_blocking(master)
    os.set_blocking(master, False)
    try:
        while written < len(data):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out writing fixture terminal input")
            readable, writable, _ = select.select([master], [master], [], remaining)
            if readable:
                try:
                    chunk = os.read(master, 65536)
                except BlockingIOError:
                    pass
                else:
                    if not chunk:
                        raise BrokenPipeError("Fixture terminal closed during input")
                    output.extend(chunk)
            if writable:
                try:
                    written += os.write(master, data[written : written + 4096])
                except BlockingIOError:
                    pass
    finally:
        os.set_blocking(master, blocking)
    return bytes(output)


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
