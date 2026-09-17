"""Load explicit, bounded TOML modules without executing project code."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import tomllib

import configuration
import toolchain as tc


@dataclass(frozen=True)
class Source:
    data: configuration.Table
    documents: dict[str, bytes]
    origins: dict[str, str]


def read(root: Path, filename: str = "chainman.toml") -> Source:
    """Merge disjoint tables; never replace a setting because of file order.

    Include paths and all configuration paths remain relative to the project
    root. Explicit repeats, cycles, escapes and symlinks are errors. The same
    document inventory is used for fingerprints and frozen update authority.
    """
    documents: dict[str, bytes] = {}
    active: list[str] = []
    total = 0

    def mark(value: Mapping[str, object], prefix: str, owner: str) -> dict[str, str]:
        result = {}
        for key, item in value.items():
            path = prefix + key
            result[path] = owner
            if isinstance(item, dict):
                result.update(mark(configuration.table(item, path), path + ".", owner))
        return result

    def merge(
        target: configuration.Table,
        origins: dict[str, str],
        incoming: Mapping[str, object],
        owners: Mapping[str, str],
        prefix: str = "",
    ) -> None:
        for key, value in incoming.items():
            location = prefix + key
            if key not in target:
                target[key] = deepcopy(value)
                origins.update(
                    {
                        field: owner
                        for field, owner in owners.items()
                        if field == location or field.startswith(location + ".")
                    }
                )
            elif isinstance(target[key], dict) and isinstance(value, dict):
                child = configuration.table(target[key], location)
                merge(
                    child,
                    origins,
                    configuration.table(value, location),
                    owners,
                    location + ".",
                )
                target[key] = child
            else:
                raise ValueError(
                    f"Duplicate configuration setting {location}: {origins[location]} and {owners[location]}"
                )

    def visit(path: str) -> tuple[configuration.Table, dict[str, str]]:
        nonlocal total
        if path in active:
            raise ValueError(
                "Configuration include cycle: " + " -> ".join([*active, path])
            )
        if path in documents:
            raise ValueError(f"Configuration file included more than once: {path}")
        if len(active) >= 16 or len(documents) >= 128:
            raise ValueError("Configuration includes exceed 16 levels or 128 files")
        selected = tc.contained(root, path)
        if selected.stat().st_size > 4 * 1024 * 1024:
            raise ValueError(f"Configuration file exceeds 4 MiB: {path}")
        body = tc.regular_input(root, path)
        total += len(body)
        if total > 4 * 1024 * 1024:
            raise ValueError("Configuration files exceed 4 MiB in total")
        documents[path] = body
        try:
            data = configuration.table(tomllib.loads(body.decode()), path)
        except (UnicodeError, tomllib.TOMLDecodeError) as error:
            raise ValueError(f"Invalid configuration file {path}: {error}") from error
        if path != filename and "schema" in data:
            raise ValueError(f"Only {filename} may declare schema (found in {path})")
        has_includes = "include" in data
        includes = data.pop("include", [])
        if not isinstance(includes, list) or any(
            not isinstance(item, str) for item in includes
        ):
            raise ValueError(f"{path}: include must be an array of explicit file paths")
        if path == filename and has_includes and data.get("schema") != 3:
            raise ValueError("Configuration includes require schema=3")
        active.append(path)
        # Each declaration retains its physical source, including nested imports.
        result: configuration.Table = {}
        origins: dict[str, str] = {}
        for item in includes:
            if not isinstance(item, str):
                raise ValueError("Include paths must be strings")
            if (
                not item
                or PurePosixPath(item).as_posix() != item
                or "\\" in item
                or any(c in item for c in "\0\n\r*?[]")
                or not item.endswith((".toml", ".toml.j2"))
            ):
                raise ValueError(
                    f"Invalid configuration include path in {path}: {item!r}"
                )
            imported, imported_origins = visit(item)
            merge(result, origins, imported, imported_origins)
        merge(result, origins, data, mark(data, "", path))
        active.pop()
        return result, origins

    data, origins = visit(filename)
    return Source(data, documents, origins)
