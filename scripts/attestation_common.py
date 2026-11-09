"""Small validated JSON and process helpers for attestation workflows."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import subprocess
from typing import Never, Protocol

from adapter_data import array, nonempty, table
import toolchain as tc


def object_value(value: object) -> dict[str, object]:
    return table(value, "JSON value")


def string(value: object) -> str:
    return nonempty(value, "JSON value")


def records(value: object) -> list[dict[str, object]]:
    return [object_value(entry) for entry in array(value, "JSON records")]


def keys(value: Mapping[str, object], allowed: Sequence[str]) -> None:
    for name in value:
        if name not in allowed:
            raise ValueError(f"Unexpected field: {name}")


def _invalid_constant(value: str) -> Never:
    raise ValueError(f"Invalid JSON constant: {value}")


def parse(value: bytes | str) -> object:
    result: object = json.loads(value, parse_constant=_invalid_constant)
    return result


def sha512(value: bytes) -> str:
    return hashlib.sha512(value).hexdigest()


@dataclass(frozen=True)
class CommandResult:
    status: int
    stdout: str
    stderr: str


class Command(Protocol):
    def __call__(
        self,
        program: str,
        args: Sequence[str],
        interactive: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


def command(
    program: str,
    args: Sequence[str],
    interactive: bool = False,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    result = tc.managed_run(
        [program, *args],
        text=True,
        stdin=None if interactive else subprocess.DEVNULL,
        stdout=None if interactive else subprocess.PIPE,
        stderr=None if interactive else subprocess.PIPE,
        env=env,
        check=False,
    )
    return CommandResult(result.returncode, result.stdout or "", result.stderr or "")
