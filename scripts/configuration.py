"""Pure, versioned expansion of explicit consumer declarations.

No environment expansion, project commands, Nix evaluation or filesystem writes
belong here. Execution and inspection share this compiler.
"""

from copy import deepcopy
from collections.abc import Iterator, Mapping, Set
import re
import math

Table = dict[str, object]
Origins = dict[str, str]


def table(value: object, location: str) -> Table:
    """Narrow decoded input at the boundary instead of propagating Any."""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{location} must be a table with string keys")
    return {key: item for key, item in value.items()}


FIELDS = {
    "tasks": set(
        "commands profile directory depends_on setup services cleanup_children timeout_seconds timeout_env shutdown_seconds wait_for_services environment context_environment transport exclusive exclusive_services serial_group network_service".split()
    ),
    "setup": set(
        "commands profile directory depends_on inputs exclude_inputs environment_inputs artifacts readiness".split()
    ),
    "services": set(
        "command profile directory environment depends_on readiness restart shutdown_seconds container setup watch scope transport network_service".split()
    ),
    "profiles": set(
        "flake runtime_profile compiler_cache environment resources inputs".split()
    ),
}
TABLES = {
    "readiness": set(
        "command http_get period_seconds timeout_seconds failure_threshold".split()
    ),
    "http_get": set("port path status_code".split()),
    "watch": set("task paths ignore debounce_ms startup_seconds".split()),
    "container": set("image command ports environment volumes read_only user".split()),
    "transport": set("ports mounts host_access".split()),
    "resources": set("max_jobs memory_per_job_gib job_variables".split()),
}
BOOLEANS = set(
    "compiler_cache cleanup_children wait_for_services exclusive exclusive_services read_only host_access".split()
)
ARRAYS = set(
    "commands command depends_on setup services inputs exclude_inputs environment_inputs artifacts paths ignore ports mounts volumes job_variables".split()
)
NUMBERS = set(
    "timeout_seconds shutdown_seconds period_seconds failure_threshold port status_code debounce_ms startup_seconds max_jobs memory_per_job_gib".split()
)


def name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", value
    ):
        raise ValueError(
            "Declaration names require letters, digits, underscores or hyphens"
        )
    return value


def fields(value: object, allowed: Set[str], location: str) -> Table:
    """Check partial declarations too, including unused template fields."""
    spec = table(value, location)
    unknown = set(spec) - allowed
    if unknown:
        raise ValueError(f"Unknown fields in {location}: {', '.join(sorted(unknown))}")
    for key, value in spec.items():
        path = f"{location}.{key}"
        if key in TABLES:
            fields(value, TABLES[key], path)
        elif key in {"environment", "context_environment"}:
            if not isinstance(value, dict) or any(
                not isinstance(v, str) or "\0" in v for v in value.values()
            ):
                raise ValueError(f"{path} requires string values")
        elif key in BOOLEANS:
            if type(value) is not bool:
                raise ValueError(f"{path} must be boolean")
        elif key in ARRAYS:
            if not isinstance(value, list):
                raise ValueError(f"{path} must be an array")
            if key == "commands":
                if any(
                    not isinstance(argv, list)
                    or not argv
                    or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
                    for argv in value
                ):
                    raise ValueError(f"{path} requires nonempty argument arrays")
            elif key not in {"artifacts", "ports", "mounts", "volumes"}:
                if any(not isinstance(v, str) or "\0" in v for v in value):
                    raise ValueError(f"{path} requires strings")
        elif key in NUMBERS:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{path} must be a nonnegative number")
        elif not isinstance(value, str) or "\0" in value:
            raise ValueError(f"{path} must be a string")
    return spec


def merge(base: Mapping[str, object], override: Mapping[str, object]) -> Table:
    result = deepcopy(dict(base))
    for key, value in override.items():
        inherited = result.get(key)
        if isinstance(value, dict) and isinstance(inherited, dict):
            result[key] = merge(table(inherited, key), table(value, key))
        else:
            result[key] = deepcopy(value)
    return result


def leaves(
    spec: Mapping[str, object], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    for key, value in spec.items():
        path = (*prefix, key)
        if isinstance(value, dict) and value:
            yield from leaves(table(value, ".".join(path)), path)
        else:
            yield path


def compile(data: Mapping[str, object]) -> tuple[Table, dict[str, Origins]]:
    """Return independent effective declarations and per-field source origins."""
    result = deepcopy(dict(data))
    templates = result.pop("templates", {})
    if data.get("schema") != 3:
        composition = "templates" in data
        for kind in FIELDS:
            entries = data.get(kind, {})
            if isinstance(entries, dict):
                composition |= any(
                    isinstance(spec, dict) and "extends" in spec
                    for spec in entries.values()
                )
        if composition:
            raise ValueError("Composition requires configuration schema=3")
        return result, {}
    templates = table(templates, "Templates")
    if set(templates) - set(FIELDS):
        raise ValueError("Templates must contain tasks, services, setup or profiles")
    origins: dict[str, Origins] = {}
    for kind, allowed in FIELDS.items():
        definitions = table(templates.get(kind, {}), f"templates.{kind}")
        entries = table(result.get(kind, {}), f"{kind} declarations")
        resolved: dict[str, tuple[Table, Origins]] = {}
        active: set[str] = set()

        def expand(value: object, location: str) -> tuple[Table, Origins]:
            spec = fields(value, allowed | {"extends"}, location)
            own = {k: v for k, v in spec.items() if k != "extends"}
            base: Table = {}
            sources: Origins = {}
            if "extends" in spec:
                base, sources = template(name(spec["extends"]))
            effective = merge(base, own)
            # Only retain origins of fields that survive array/scalar replacement.
            sources = dict(sources)
            for path in leaves(own):
                sources[".".join(path)] = location
            sources = {
                ".".join(path): sources[".".join(path)] for path in leaves(effective)
            }
            return effective, sources

        def template(key: str) -> tuple[Table, Origins]:
            if key in active:
                raise ValueError(f"Template inheritance cycle in {kind}: {key}")
            if key not in definitions:
                raise ValueError(f"Unknown {kind} template: {key}")
            if key not in resolved:
                active.add(key)
                resolved[key] = expand(definitions[key], f"templates.{kind}.{key}")
                active.remove(key)
            return resolved[key]

        for key in definitions:
            template(name(key))
        for key, spec in entries.items():
            location = f"{kind}.{name(key)}"
            effective, sources = expand(spec, location)
            entries[key] = effective
            origins[location] = sources
        if kind in result:
            result[kind] = entries
    return result, origins
