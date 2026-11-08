"""Explicit external source roots, shared by setup and managed executions.

The consumer retains ownership of profiles, leases, caches and service state.
Only the selected source tree is mounted, at its canonical host path, so HMR
and package-manager outputs refer to the same files in both Nix modes.
"""

from collections.abc import Mapping
import os
from pathlib import Path

import project_environment
import toolchain as tc
from adapter_data import text


def declaration(spec: Mapping[str, object]) -> None:
    if "source_root_env" in spec:
        project_environment.variable(spec["source_root_env"])
        relative = text(spec.get("directory", "."), "Source directory")
        part = Path(relative)
        if relative != "." and (
            part.is_absolute()
            or not part.parts
            or any(p in ("..", ".git") for p in part.parts)
        ):
            raise ValueError(
                "External source directory must be a contained relative path"
            )


def root(
    consumer: Path, spec: Mapping[str, object], env: Mapping[str, str] | None = None
) -> Path:
    declaration(spec)
    key = spec.get("source_root_env")
    if key is None:
        return consumer
    value = (os.environ if env is None else env).get(str(key), "")
    selected = Path(value)
    if not value or not selected.is_absolute() or any(c in value for c in "\0\n\r,"):
        raise ValueError(f"External source root {key} requires an absolute directory")
    selected = selected.resolve(strict=True)
    if (
        not selected.is_dir()
        or selected == Path("/")
        or selected == Path.home()
        or consumer.is_relative_to(selected)
    ):
        raise ValueError(
            f"External source root {key} must be a narrow source directory"
        )
    return selected


def directory(
    consumer: Path, spec: Mapping[str, object], env: Mapping[str, str] | None = None
) -> Path:
    return tc.contained(
        root(consumer, spec, env), text(spec.get("directory", "."), "Source directory")
    )


def identity(
    consumer: Path, spec: Mapping[str, object], env: Mapping[str, str]
) -> str | None:
    """Resolve selected roots for fingerprints without enabling absent groups."""
    key = spec.get("source_root_env")
    if key is None or not env.get(str(key)):
        return None
    return str(root(consumer, spec, env))


def mounts(
    consumer: Path, specs: list[Mapping[str, object]], env: Mapping[str, str]
) -> list[object]:
    selected: dict[str, object] = {}
    for spec in specs:
        if "source_root_env" not in spec:
            continue
        path = str(root(consumer, spec, env))
        selected[path] = {"source": path, "target": path, "read_only": False}
    return list(selected.values())
