"""Prepare an owned container profile before execing its one user command."""

import ctypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile


def preparation_parent_guard(parent: int) -> None:
    """Container Nix must not survive a killed entry/timeout owner (Linux)."""
    libc = ctypes.CDLL(None, use_errno=True)
    # PR_SET_PDEATHSIG is inherited through exec of the unprivileged Nix client.
    # The daemon owns builders; ending this client closes its request as usual.
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot guard profile preparation owner")
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)


def prepare(nix: str, ref: str, profile: str) -> int:
    """Retry only a collected-derivation failure, once, before shell hooks run."""
    for attempt in range(2):
        command = [
            nix,
            "--extra-experimental-features",
            "nix-command flakes",
            "print-dev-env",
            ref,
            *(["--no-eval-cache"] if attempt else []),
            "--no-write-lock-file",
            "--profile",
            profile,
        ]
        # Preparation cannot consume application stdin. Disk-backed diagnostics
        # avoid buffering pressure-GC logs in memory, and remain within the
        # caller's original stderr redirection/capture policy.
        with tempfile.TemporaryFile() as diagnostics:
            parent = os.getpid()
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                check=False,
                preexec_fn=lambda: preparation_parent_guard(parent),
            )
            diagnostics.seek(0, os.SEEK_END)
            size = diagnostics.tell()
            diagnostics.seek(max(0, size - 65536))
            tail = diagnostics.read()
            diagnostics.seek(0)
            shutil.copyfileobj(diagnostics, sys.stderr.buffer)
            sys.stderr.flush()
        if result.returncode == 0:
            return 0
        collected = re.search(
            rb"error: path '/nix/store/[a-z0-9]{32}-[^'\n]+\.drv' is not valid",
            tail,
        )
        if result.returncode != 1 or not collected or attempt:
            return result.returncode
    raise AssertionError("Unreachable profile preparation state")


def main(arguments: list[str]) -> int:
    if len(arguments) < 4:
        raise ValueError("Profile entry requires Nix, reference, root and command")
    nix, ref, profile, *command = arguments
    result = prepare(nix, ref, profile)
    if result:
        return result
    # Never catch a command failure or inspect its stderr for retry eligibility.
    # exec also preserves the managed PID, stdio, inherited leases and timeout.
    os.execvp(command[0], command)
    raise AssertionError("exec returned")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
