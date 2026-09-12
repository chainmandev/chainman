"""Literal project configuration; no shell evaluation or implicit secret discovery."""

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re

import toolchain as tc

VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REFERENCE = re.compile(
    r"\{(root|cache|work|host|bind|env:[A-Za-z_][A-Za-z0-9_]*|service:[^{}]*)\}"
)


def variable(name):
    if not isinstance(name, str) or not VARIABLE.fullmatch(name):
        raise ValueError("Environment entries require variable names")
    if name == "CHAINMAN_TEMP_BASE":
        raise ValueError("Configure TMPDIR instead of internal temporary routing")
    if name == "CHAINMAN_RUNTIME_NIX_BIN":
        raise ValueError("Cannot override the internal runtime Nix binding")
    if name.startswith(("CHAINMAN_", "TOOLCHAIN_")) or name in {
        "SCCACHE_SERVER_UDS",
        "RUSTC_WRAPPER",
    }:
        raise ValueError("Cannot override managed runtime or compiler ownership")
    return name


def transport(spec):
    if not isinstance(spec, dict) or set(spec) - {"ports", "mounts", "host_access"}:
        raise ValueError("Transport supports only ports, mounts and host_access")
    if type(spec.get("host_access", False)) is not bool:
        raise ValueError("Transport host_access must be boolean")
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
        if type(mount.get("read_only", True)) is not bool:
            raise ValueError("Mount read_only must be boolean")


def files(root, spec):
    result = []
    for entry in spec.get("files", []):
        if not isinstance(entry, dict) or set(entry) - {"path", "required", "override"}:
            raise ValueError(
                "Environment files require path, optional required/override booleans"
            )
        if any(
            type(entry.get(key, False)) is not bool for key in ("required", "override")
        ):
            raise ValueError("Environment file flags must be boolean")
        path = tc.contained(root, entry["path"])
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
        result.append((entry, data, values))
    return result


def file_fingerprint(root, spec):
    return hashlib.sha256(
        json.dumps(
            [
                [entry, hashlib.sha256(data).hexdigest() if data is not None else None]
                for entry, data, _ in files(root, spec)
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def expand(values, root, env):
    if not isinstance(values, dict):
        raise ValueError("Environment values must be a table")
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
    resolved = {}

    def resolve(key, visiting):
        if key in resolved:
            return resolved[key]
        if key in visiting or len(visiting) >= 32:
            raise ValueError("Cyclic environment references")

        def replace(match):
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
            if name in values:
                return resolve(name, visiting | {key})
            if name not in env:
                raise ValueError(f"Environment reference is unset: {name}")
            return env[name]

        resolved[key] = REFERENCE.sub(replace, values[key])
        return resolved[key]

    for key in values:
        resolve(key, set())
    return resolved


def apply(root, spec, inherited):
    env = dict(inherited)
    for entry, _, values in files(root, spec):
        # File values are literal, including quotes, dollars and braces.
        for key, value in values.items():
            if entry.get("override", False) or key not in env:
                env[key] = value
    mode = env.get("CHAINMAN_MODE", "host-nix")
    modes = spec.get("modes", {})
    if set(modes) - {"host-nix", "container-nix"}:
        raise ValueError("Environment modes must be host-nix or container-nix")
    selected = modes.get(mode, {})
    for index, values in enumerate(
        (
            dict(spec.get("defaults", {}), **selected.get("defaults", {})),
            spec.get("values", {}),
            selected.get("values", {}),
        )
    ):
        if index == 0:
            values = {key: value for key, value in values.items() if key not in env}
        expanded = expand(values, root, env)
        env.update(expanded)
        tc.pnpm_store_environment(env, expanded)
    return env


def host_inputs(destination, spec):
    path = destination / "host-environment"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Controller planning requires bounded host environment data")
    patterns = spec.get("pass", [])
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
