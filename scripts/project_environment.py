"""Literal project configuration; no shell evaluation or implicit secret discovery."""

import fnmatch
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re

import toolchain as tc
from adapter_data import Table, array, string_map, strings, table, text

VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REFERENCE = re.compile(
    r"\{(root|cache|work|host|bind|env:[A-Za-z_][A-Za-z0-9_]*|service:[^{}]*)\}"
)


def variable(name: object) -> str:
    if not isinstance(name, str) or not VARIABLE.fullmatch(name):
        raise ValueError("Environment entries require variable names")
    if name == "CHAINMAN_TEMP_BASE":
        raise ValueError("Configure TMPDIR instead of internal temporary routing")
    if name == "CHAINMAN_RUNTIME_NIX_BIN":
        raise ValueError("Cannot override the internal runtime Nix binding")
    if name in {
        "NIX_CONFIG",
        "NIX_REMOTE",
        "NIX_STATE_DIR",
        "NIX_STORE_DIR",
        "NIX_DAEMON_SOCKET_PATH",
    }:
        raise ValueError("Cannot override the managed Nix store connection")
    if name.startswith(("CHAINMAN_", "TOOLCHAIN_")) or name in {
        "SCCACHE_SERVER_UDS",
        "RUSTC_WRAPPER",
    }:
        raise ValueError("Cannot override managed runtime or compiler ownership")
    return name


def transport(spec: object) -> None:
    if not isinstance(spec, dict) or set(spec) - {
        "ports",
        "mounts",
        "host_access",
        "display",
    }:
        raise ValueError(
            "Transport supports only ports, mounts, host_access and display"
        )
    if type(spec.get("host_access", False)) is not bool:
        raise ValueError("Transport host_access must be boolean")
    if "display" in spec and spec["display"] != "x11":
        raise ValueError("Transport display supports only x11")
    ports = spec.get("ports", [])
    if not isinstance(ports, list) or any(
        not isinstance(port, str)
        or not port.startswith("127.0.0.1:")
        or "\n" in port
        or "\r" in port
        for port in ports
    ):
        raise ValueError("Transport ports must explicitly bind loopback")
    mounts = spec.get("mounts", [])
    if not isinstance(mounts, list):
        raise ValueError("Transport mounts must be an array")
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) - {
            "source",
            "source_env",
            "target",
            "read_only",
            "optional",
        }:
            raise ValueError(
                "Transport mounts require a source or source_env and optional target/read_only"
            )
        if ("source" in mount) == ("source_env" in mount):
            raise ValueError("A mount requires exactly one source or source_env")
        if "source_env" in mount:
            variable(mount["source_env"])
        elif not isinstance(mount["source"], str) or not mount["source"]:
            raise ValueError("Mount source must be a nonempty path")
        if "target" not in mount and "source_env" not in mount:
            raise ValueError("Literal mounts require an explicit target")
        if "target" in mount and (
            not isinstance(mount["target"], str) or not mount["target"].startswith("/")
        ):
            raise ValueError("Mount target must be absolute")
        if type(mount.get("optional", False)) is not bool:
            raise ValueError("Mount optional must be boolean")
        if type(mount.get("read_only", True)) is not bool:
            raise ValueError("Mount read_only must be boolean")


def values(spec: object) -> None:
    if not isinstance(spec, dict):
        raise ValueError("Environment values must be a table")
    for key, value in spec.items():
        variable(key)
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Environment values must be strings without NUL")


def validate(root: Path, spec: object) -> None:
    """Validate structure without reading files, secrets or live endpoints."""
    if not isinstance(spec, dict) or set(spec) - {
        "files",
        "pass",
        "unset",
        "defaults",
        "values",
        "modes",
    }:
        raise ValueError("Unknown environment configuration fields")
    for key in ("defaults", "values"):
        values(spec.get(key, {}))
    modes = spec.get("modes", {})
    if not isinstance(modes, dict) or set(modes) - {
        "host",
        "host-nix",
        "container-nix",
    }:
        raise ValueError("Environment modes must be host, host-nix or container-nix")
    for mode in modes.values():
        if not isinstance(mode, dict) or set(mode) - {"defaults", "values"}:
            raise ValueError("Environment modes support defaults and values")
        for key in ("defaults", "values"):
            values(mode.get(key, {}))
    for key in ("pass", "unset"):
        entries = spec.get(key, [])
        if not isinstance(entries, list):
            raise ValueError(f"Environment {key} must be an array")
        for entry in entries:
            if key == "unset":
                variable(entry)
            elif not isinstance(entry, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_*?]*", entry
            ):
                raise ValueError("Invalid environment forwarding pattern")
    entries = spec.get("files", [])
    if not isinstance(entries, list):
        raise ValueError("Environment files must be an array")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or "path" not in entry
            or set(entry) - {"path", "required", "override", "when"}
        ):
            raise ValueError("Invalid environment file declaration")
        tc.contained(root, text(entry["path"], "Environment file path"))
        if any(
            type(entry.get(key, False)) is not bool for key in ("required", "override")
        ):
            raise ValueError("Environment file flags must be boolean")
        values(entry.get("when", {}))
        if "when" in entry and not entry["when"]:
            raise ValueError("Environment file when must be nonempty")


def files(
    root: Path,
    spec: Mapping[str, object],
    inherited: Mapping[str, str] | None = None,
) -> list[tuple[Table, bytes | None, dict[str, str]]]:
    result: list[tuple[Table, bytes | None, dict[str, str]]] = []
    env = dict(inherited or {})
    selectors: dict[str, str | None] = {}
    for raw in array(spec.get("files", []), "Environment files"):
        entry = table(raw, "Environment file")
        if set(entry) - {
            "path",
            "required",
            "override",
            "when",
        }:
            raise ValueError(
                "Environment files require path, optional required/override booleans and a when table"
            )
        if any(
            type(entry.get(key, False)) is not bool for key in ("required", "override")
        ):
            raise ValueError("Environment file flags must be boolean")
        path = tc.contained(root, text(entry["path"], "Environment file path"))
        condition = entry.get("when", {})
        if not isinstance(condition, dict) or ("when" in entry and not condition):
            raise ValueError("Environment file when must be a nonempty literal table")
        condition = string_map(condition, "Environment file condition")
        for key, value in condition.items():
            variable(key)
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("Environment file conditions require literal strings")
            selectors[key] = env.get(key)
        if any(env.get(key) != value for key, value in condition.items()):
            result.append((entry, None, {}))
            continue
        if not path.exists() and not entry.get("required", False):
            result.append((entry, None, {}))
            continue
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError("Environment input must be a regular file under 1 MiB")
        data = path.read_bytes()
        values = {}
        for number, line in enumerate(data.decode().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, equals, value = line.partition("=")
            if not equals or "\0" in value:
                raise ValueError(
                    f"Invalid environment file line {entry['path']}:{number}"
                )
            variable(key)
            if key in values:
                raise ValueError(
                    f"Duplicate environment variable in {entry['path']}:{number}"
                )
            values[key] = value
        for key, value in values.items():
            if entry.get("override", False) or key not in env:
                if key in selectors and selectors[key] != value:
                    raise ValueError(
                        "Environment files cannot change an earlier file condition"
                    )
                env[key] = value
        result.append((entry, data, values))
    return result


def file_fingerprint(
    root: Path, spec: Mapping[str, object], env: Mapping[str, str] | None = None
) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                [entry, hashlib.sha256(data).hexdigest() if data is not None else None]
                for entry, data, _ in files(root, spec, env)
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def expand(values: object, root: Path, env: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ValueError("Environment values must be a table")
    literals = string_map(values, "Environment values")
    for key, value in values.items():
        variable(key)
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Environment values must be strings without NUL")
    container = (
        env.get("CHAINMAN_MODE", "host-nix") == "container-nix"
        and env.get("CHAINMAN_CONTAINER_NETWORK_MODE", "bridge") != "host"
    )
    paths = {
        "root": str(root),
        "cache": env.get(
            "TOOLCHAIN_DOWNLOAD_CACHE",
            str(
                Path(env.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
                / "nix-just-downloads"
            ),
        ),
        "work": env.get(
            "TOOLCHAIN_WORK", str(root / ".cache/toolchain/work" / tc.context_id())
        ),
        "host": "host.docker.internal" if container else "127.0.0.1",
        "bind": "0.0.0.0" if container else "127.0.0.1",
    }
    resolved: dict[str, str] = {}

    def resolve(key: str, visiting: set[str]) -> str:
        if key in resolved:
            return resolved[key]
        if key in visiting or len(visiting) >= 32:
            raise ValueError("Cyclic environment references")

        def replace(match: re.Match[str]) -> str:
            name = match[1]
            if name.startswith("service:"):
                import service_endpoints

                parts = name.split(":")
                if len(parts) != 3:
                    raise ValueError("Service endpoints require {service:NAME:PORT}")
                _, service, port = parts
                return service_endpoints.address(root, service, port, container)
            if not name.startswith("env:"):
                return paths[name]
            name = name[4:]
            if name in literals:
                return resolve(name, visiting | {key})
            if name not in env:
                raise ValueError(f"Environment reference is unset: {name}")
            return env[name]

        resolved[key] = REFERENCE.sub(replace, literals[key])
        return resolved[key]

    for key in literals:
        resolve(key, set())
    return resolved


def apply(
    root: Path, spec: Mapping[str, object], inherited: Mapping[str, str]
) -> dict[str, str]:
    validate(root, spec)
    env = dict(inherited)
    for entry, _, values in files(root, spec, env):
        # File values are literal, including quotes, dollars and braces.
        applied = {}
        for key, value in values.items():
            if entry.get("override", False) or key not in env:
                env[key] = value
                applied[key] = value
        tc.pnpm_environment(env, applied)
    mode = env.get("CHAINMAN_MODE", "host-nix")
    modes = table(spec.get("modes", {}), "Environment modes")
    if set(modes) - {"host", "host-nix", "container-nix"}:
        raise ValueError("Environment modes must be host, host-nix or container-nix")
    selected = table(modes.get(mode, {}), "Selected environment mode")
    for index, values in enumerate(
        (
            {
                **string_map(spec.get("defaults", {}), "Environment defaults"),
                **string_map(selected.get("defaults", {}), "Mode defaults"),
            },
            string_map(spec.get("values", {}), "Environment values"),
            string_map(selected.get("values", {}), "Mode values"),
        )
    ):
        if index == 0:
            values = {key: value for key, value in values.items() if key not in env}
        expanded = expand(values, root, env)
        env.update(expanded)
        tc.pnpm_environment(env, expanded)
    return env


def host_inputs(destination: Path, spec: Mapping[str, object]) -> dict[str, str]:
    path = destination / "host-environment"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Controller planning requires bounded host environment data")
    patterns = strings(spec.get("pass", []), "Environment forwarding patterns")
    inherited = {}
    for record in path.read_bytes().split(b"\0"):
        if not record:
            continue
        name, equals, value = os.fsdecode(record).partition("=")
        if (
            equals
            and VARIABLE.fullmatch(name)
            and any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
        ):
            inherited[name] = value
    return inherited
