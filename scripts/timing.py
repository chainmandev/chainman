"""Opt-in timing records; never log argv, environment values or project output."""

from contextlib import contextmanager
from collections.abc import Iterator, Mapping
import json
import os
import re
import sys
import time
import uuid


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return (os.environ if env is None else env).get("CHAINMAN_TIMING") == "1"


def emit(phase: str, event: str, operation: str, **values: int | str) -> None:
    parent = os.environ.get("CHAINMAN_TIMING_PARENT", "")
    if re.fullmatch(r"[0-9a-f-]{1,64}", parent):
        values["parent"] = parent
    record = dict(
        schema=1,
        phase=phase,
        event=event,
        operation=operation,
        monotonic_ns=time.monotonic_ns(),
        **values,
    )
    try:
        print(
            "CHAINMAN_TIMING " + json.dumps(record, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        # Losing an optional diagnostic sink must not change task execution.
        pass


@contextmanager
def span(phase: str, env: Mapping[str, str] | None = None) -> Iterator[None]:
    if not enabled(env):
        yield
        return
    operation = uuid.uuid4().hex
    started = time.monotonic_ns()
    emit(phase, "start", operation)
    try:
        yield
    finally:
        emit(phase, "end", operation, elapsed_ns=time.monotonic_ns() - started)


def bootstrap() -> None:
    if not enabled():
        return
    started = os.environ.pop("CHAINMAN_TIMING_BOOTSTRAP_STARTED", "")
    operation = uuid.uuid4().hex
    if started.isdecimal():
        # The pre-runtime POSIX shell has only whole-second portable timestamps.
        emit(
            "bootstrap",
            "end",
            operation,
            elapsed_ns=max(0, time.time_ns() - int(started) * 1_000_000_000),
            resolution_ns=1_000_000_000,
        )
    os.environ["CHAINMAN_TIMING_PARENT"] = operation


def command() -> None:
    operation, *argv = sys.argv[1:]
    emit("profile_entry", "end", operation)
    emit("command", "start", operation)
    os.environ["CHAINMAN_TIMING_PARENT"] = operation
    os.execvpe(argv[0], argv, os.environ)


if __name__ == "__main__":
    command()
