"""Setup probes and consent, shared by inspection and task admission."""

from collections.abc import Mapping
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys

import chainman
import toolchain as tc
from adapter_data import Table, strings, table, text


def declaration(value: object) -> Table:
    probe = table(value, "Setup readiness")
    if set(probe) - {"command", "timeout_seconds"}:
        raise ValueError("Unknown setup readiness field")
    argv = strings(probe.get("command"), "Setup readiness command")
    if not argv or not argv[0] or any("\0" in arg for arg in argv):
        raise ValueError("Setup readiness requires a nonempty argument-array command")
    timeout = probe.get("timeout_seconds", 30)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError("Setup readiness timeout_seconds must be between 1 and 300")
    return probe


def check(root: Path, spec: Mapping[str, object], env: Mapping[str, str]) -> str | None:
    if "readiness" not in spec:
        return None
    probe = declaration(spec["readiness"])
    try:
        result = chainman.execute(
            root,
            text(spec["profile"], "Setup profile"),
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--probe",
                str(probe.get("timeout_seconds", 30)),
                *strings(probe["command"], "Setup readiness command"),
            ],
            env=dict(env, CHAINMAN_SETUP="error"),
            cwd=root / text(spec["directory"], "Setup directory"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "readiness check timed out"
    except OSError as error:
        return f"readiness check could not start: {error}"
    if result.returncode:
        # Package-manager diagnostics can contain control characters and secrets;
        # don't persist them in setup stamps. Print bounded, plain text only.
        diagnostic = " ".join((result.stderr or result.stdout).split())
        diagnostic = "".join(c for c in diagnostic if c.isprintable())[:2000]
        return f"readiness check exited {result.returncode}: {diagnostic}"
    return None


def policy(env: Mapping[str, str]) -> str:
    selected = env.get("CHAINMAN_SETUP", "prompt")
    if selected not in {"prompt", "auto", "error"}:
        raise ValueError("CHAINMAN_SETUP must be prompt, auto or error")
    return selected


def authorize(
    repairs: Mapping[str, str],
    env: Mapping[str, str],
    *,
    recovery: list[str] | None = None,
) -> None:
    selected = policy(env)
    if selected == "auto":
        return
    command = shlex.join(recovery or ["just", "chainman", "setup", *repairs])
    details = "; ".join(f"{key}: {reason}" for key, reason in repairs.items())
    message = f"Setup needs repair ({details}). Run `{command}` and continue? [Y/n] "
    if selected == "prompt":
        channel = env.get("CHAINMAN_SETUP_CHANNEL")
        if channel:
            # Container entry supplies two private FIFOs. Command stdin remains
            # untouched, including Git's pre-push ref stream.
            with open(Path(channel) / "request", "w") as request:
                request.write(message + "\n")
            with open(Path(channel) / "response") as response:
                answer = response.readline()
            if answer.strip().lower() == "yes":
                return
        else:
            try:
                with (
                    open("/dev/tty", "r") as answers,
                    open("/dev/tty", "w") as terminal,
                ):
                    terminal.write(message)
                    terminal.flush()
                    answer = answers.readline()
                    if answer and answer.strip().lower() in {"", "y", "yes"}:
                        return
            except OSError:
                pass
    raise ValueError(
        f"Setup is not ready: {details}. Run `{command}` (or `just setup` for "
        "the whole project). For explicitly authorized unattended repair, "
        "set CHAINMAN_SETUP=auto. No task commands were started."
    )


def run_probe(timeout: int, argv: list[str]) -> int:
    """Bound the check and its process group without a native controller."""
    options: tc.ProcessOptions = {
        "stdin": subprocess.DEVNULL,
        "start_new_session": True,
    }
    child = subprocess.Popen(argv, **tc.managed_options(options))

    def interrupted(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous = {
        sig: signal.signal(sig, interrupted)
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    }
    try:
        status = child.wait(timeout=timeout)
        return status if status >= 0 else 128 - status
    except subprocess.TimeoutExpired:
        print("readiness check timed out", file=sys.stderr)
        return 124
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "--probe":
        raise SystemExit("Internal setup probe requires a timeout and command")
    raise SystemExit(run_probe(int(sys.argv[2]), sys.argv[3:]))
