"""Prepare data for the native host ownership adapter, without running project code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
from urllib.parse import urlsplit
from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict
from adapter_data import Table, array, strings, table, text

import chainman
import toolchain as tc
import workflows
import project_environment


class Command(TypedDict):
    argv: list[str]
    directory: str
    environment: dict[str, str]


class Container(TypedDict):
    engine: str
    name: str
    token: str


class Volume(TypedDict):
    engine: str
    name: str
    scope: str
    compatibility: str
    policy: str
    services: list[str]


class HTTPProbe(TypedDict):
    port: int
    path: str
    status_code: int


class Probe(TypedDict):
    command: NotRequired[Command]
    http_get: NotRequired[HTTPProbe]
    period_seconds: int
    timeout_seconds: int
    failure_threshold: int


class Watch(TypedDict):
    build: Command
    paths: list[str]
    ignore: list[str]
    debounce_ms: int
    startup_seconds: int
    container: NotRequired[Container]


class Service(TypedDict):
    command: Command
    depends_on: list[str]
    restart: str
    shutdown_seconds: int
    network_service: NotRequired[str]
    container: NotRequired[Container]
    readiness: NotRequired[Probe]
    watch: NotRequired[Watch]


class Bridge(TypedDict):
    engine: str
    name: str
    scope: str


class Plan(TypedDict):
    schema: Literal[1]
    root: str
    state: str
    backend: str
    licenses: dict[str, str]
    fingerprint: str
    services: dict[str, Service]
    requested: list[str]
    watcher: NotRequired[str]
    volumes: NotRequired[list[Volume]]
    prepare: NotRequired[Command]
    task: NotRequired[Command]
    wait_for_services: NotRequired[bool]
    exclusive_services: NotRequired[bool]
    own_task: NotRequired[bool]
    task_shutdown_seconds: NotRequired[int]
    resources: NotRequired[list[Plan]]
    bridge: NotRequired[Bridge]
    task_network_service: NotRequired[str]
    task_container: NotRequired[Container]


def integer(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def http_readiness(value: object) -> HTTPProbe:
    if not isinstance(value, dict) or set(value) - {"port", "path", "status_code"}:
        raise ValueError("Invalid HTTP readiness declaration")
    port, path, status = (
        value.get("port"),
        value.get("path", "/"),
        value.get("status_code", 200),
    )
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or type(status) is not int
        or not 200 <= status <= 299
        or not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or any(ord(c) <= 32 or ord(c) == 127 or c == "#" for c in path)
        or urlsplit(path).netloc
    ):
        raise ValueError(
            "HTTP readiness requires a loopback port, absolute path and 2xx status"
        )
    return {"port": port, "path": path, "status_code": status}


def volume_compatibility(root: Path, volume: object) -> str:
    allowed = {"name", "target", "policy", "format", "inputs"}
    if not isinstance(volume, dict) or set(volume) - allowed:
        raise ValueError("Invalid service volume declaration")
    if volume.get("policy", "preserve") not in {"preserve", "disposable"}:
        raise ValueError("Volume policy must be preserve or disposable")
    if not isinstance(volume.get("format"), str) or not volume["format"]:
        raise ValueError("Persistent service volumes require an explicit data format")
    inputs = volume.get("inputs", [])
    if not isinstance(inputs, list) or any(
        not isinstance(item, str) for item in inputs
    ):
        raise ValueError("Volume compatibility inputs must be path patterns")
    digest = hashlib.sha256(json.dumps([1, volume["format"]]).encode())
    selected: set[Path] = set()
    for pattern in inputs:
        tc.contained(root, pattern)
        matches = list(root.glob(pattern))
        if not matches:
            raise ValueError(f"Volume compatibility input matched no files: {pattern}")
        for path in matches:
            tc.contained(root, path.relative_to(root).as_posix())
            selected.update(path.rglob("*") if path.is_dir() else [path])
    files = 0
    for path in sorted(selected):
        relative = path.relative_to(root).as_posix()
        tc.contained(root, relative)
        if path.is_file():
            files += 1
            digest.update(relative.encode() + b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    if inputs and not files:
        raise ValueError("Volume compatibility inputs matched no regular files")
    return digest.hexdigest()


def command(
    argv: list[str], root: Path, environment: Mapping[str, str] | None = None
) -> Command:
    workflows.commands([argv])
    selected = dict(environment or {})
    # Preserve architecture when a saved controller plan is restarted by a caller
    # whose environment differs. The bootstrap validates the selected platform.
    for key in (
        "CHAINMAN_CONTAINER_PLATFORM",
        "CHAINMAN_NIX_VOLUME",
        "CHAINMAN_ENTRY_AUTHORITY",
        "CHAINMAN_UPDATE_ACTIVE",
    ):
        if os.environ.get(key):
            selected[key] = os.environ[key]
    return {"argv": argv, "directory": str(root), "environment": selected}


def declarations(root: Path, cfg: Mapping[str, object]) -> dict[str, Table]:
    raw_entries = cfg.get("services", {})
    if not isinstance(raw_entries, dict):
        raise ValueError("services must contain named declarations")
    entries = {
        key: table(spec, f"services.{key}")
        for key, spec in table(raw_entries, "Services").items()
    }
    task_entries = {
        key: table(spec, f"tasks.{key}")
        for key, spec in table(cfg.get("tasks", {}), "Tasks").items()
    }
    setup_entries = table(cfg.get("setup", {}), "Setup groups")
    project = table(cfg.get("project", {}), "Project")
    global_transport = table(cfg.get("container", {}), "Global transport")
    checked_profiles: set[str] = set()
    for key, spec in entries.items():
        workflows.name(key)
        if not isinstance(spec, dict):
            raise ValueError(f"services.{key} must be a declaration")
        from configuration import FIELDS

        allowed = FIELDS["services"]
        if set(spec) - allowed:
            raise ValueError(f"Unknown service fields: {sorted(set(spec) - allowed)}")
        if ("command" in spec) == ("container" in spec):
            raise ValueError(
                "A service requires exactly one command or container declaration"
            )
        project_environment.transport(spec.get("transport", {}))
        if "container" in spec and spec.get("transport"):
            raise ValueError(
                "Data containers declare ports and volumes in their container table"
            )
        scope = spec.get("scope", "worktree")
        if scope not in {"worktree", "repository"}:
            raise ValueError("Service scope must be worktree or repository")
        if scope == "repository":
            if "container" not in spec or any(
                spec.get(field)
                for field in ("setup", "watch", "environment", "directory")
            ):
                raise ValueError(
                    "Repository services require self-contained data containers"
                )
            if any(
                binding in json.dumps(spec)
                for binding in ("{root}", "{work}", "{cache}", "{host}", "{bind}")
            ):
                raise ValueError("Repository services cannot bind a worktree path")
        if "command" in spec:
            workflows.commands([spec["command"]])
            profile = text(
                spec.get("profile", project.get("default_profile", "default")),
                "Service profile",
            )
            if profile not in checked_profiles:
                chainman.profile(root, profile, cfg=cfg)
                checked_profiles.add(profile)
        if "container" in spec:
            item = spec["container"]
            if not isinstance(item, dict) or set(item) - {
                "image",
                "command",
                "ports",
                "environment",
                "volumes",
                "read_only",
                "user",
            }:
                raise ValueError("Invalid container service declaration")
            image = item.get("image", "")
            import re

            if not isinstance(image, str) or not re.fullmatch(
                r"[^\s@]+@sha256:[a-f0-9]{64}", image
            ):
                raise ValueError("Service container images require a SHA256 digest")
            if item.get("command"):
                workflows.commands([item["command"]])
        tc.contained(root, text(spec.get("directory", "."), "Service directory"))
        workflows.names(spec.get("depends_on", []))
        workflows.order(setup_entries, workflows.names(spec.get("setup", [])))
        if spec.get("restart", "no") not in {"no", "always", "on_failure"}:
            raise ValueError("Invalid service restart policy")
        timeout = spec.get("shutdown_seconds", 10)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("Service shutdown_seconds must be between 1 and 300")
        if "readiness" in spec:
            probe = spec["readiness"]
            if not isinstance(probe, dict) or set(probe) - {
                "command",
                "http_get",
                "period_seconds",
                "timeout_seconds",
                "failure_threshold",
            }:
                raise ValueError("Invalid service readiness declaration")
            if ("command" in probe) == ("http_get" in probe):
                raise ValueError(
                    "Readiness requires exactly one of command or http_get"
                )
            if "http_get" in probe:
                http_readiness(probe["http_get"])
            else:
                workflows.commands([probe["command"]])
            values = [
                probe.get("period_seconds", 1),
                probe.get("timeout_seconds", 2),
                probe.get("failure_threshold", 30),
            ]
            if (
                any(type(value) is not int or value < 1 for value in values)
                or values[0] * values[2] + values[1] > 600
            ):
                raise ValueError("Readiness requires positive, bounded timeouts")
        if "watch" in spec:
            watch = spec["watch"]
            if (
                "container" in spec
                or not isinstance(watch, dict)
                or set(watch)
                - {"task", "paths", "ignore", "debounce_ms", "startup_seconds"}
            ):
                raise ValueError("Invalid service watch declaration")
            tasks = workflows.order(task_entries, [workflows.name(watch.get("task"))])
            if any(task_entries[task].get("services") for task in tasks):
                raise ValueError(
                    "A watched build task cannot acquire services recursively"
                )
            if not isinstance(watch.get("paths"), list) or not watch["paths"]:
                raise ValueError("Watch requires explicit paths")
            for path in watch["paths"]:
                tc.contained(root, path)
            if not isinstance(watch.get("ignore", []), list) or any(
                not isinstance(p, str) or "\0" in p for p in watch.get("ignore", [])
            ):
                raise ValueError("Watch ignores require string patterns")
            for key, default, maximum in (
                ("debounce_ms", 100, 60000),
                ("startup_seconds", 300, 599),
            ):
                value = watch.get(key, default)
                if type(value) is not int or not 1 <= value <= maximum:
                    raise ValueError(f"Invalid watch {key}")
    workflows.order(entries, list(entries))
    for spec in entries.values():
        if spec.get("scope") == "repository" and any(
            entries[dependency].get("scope") != "repository"
            for dependency in workflows.names(spec.get("depends_on", []))
        ):
            raise ValueError("Repository services cannot depend on worktree services")
    for spec in task_entries.values():
        workflows.order(entries, workflows.names(spec.get("services", [])))
    for section in (entries, task_entries):
        for key, spec in section.items():
            peer = spec.get("network_service")
            if peer is None:
                continue
            peer = workflows.name(peer)
            selected = workflows.order(
                entries,
                workflows.names(spec.get("depends_on", []))
                if section is entries
                else [
                    service
                    for task in workflows.order(task_entries, [key])
                    for service in workflows.names(
                        task_entries[task].get("services", [])
                    )
                ],
            )
            if peer not in selected:
                raise ValueError(
                    "network_service must be a declared service dependency"
                )
            provider = entries[peer]
            if provider.get("network_service") or provider.get("restart", "no") != "no":
                raise ValueError(
                    "Network owners cannot borrow or automatically restart"
                )
            transport = table(
                spec.get("container", spec.get("transport", {})), "Service transport"
            )
            if transport.get("ports") or table(
                spec.get("transport", {}), "Transport"
            ).get("host_access"):
                raise ValueError("A borrowed network publishes ports only on its owner")
            if global_transport.get("ports") or global_transport.get("host_access"):
                raise ValueError(
                    "Borrowed networks cannot combine global ports or host aliases"
                )
    return entries


def literal_environment(
    values: object, root: Path, env: Mapping[str, str] | None = None
) -> dict[str, str]:
    return project_environment.expand(values, root, os.environ if env is None else env)


def config_fingerprint(
    root: Path,
    cfg: Mapping[str, object],
    *,
    include_volume_inputs: bool = True,
    env: Mapping[str, str] | None = None,
) -> str:
    env = os.environ if env is None else env
    declared = workflows.declarations(cfg, "services")
    tasks = workflows.declarations(cfg, "tasks")
    workflow_specs = [*declared.values(), *tasks.values()]
    profiles = sorted(
        {
            text(
                spec.get("profile", workflows.default_profile(cfg)), "Workflow profile"
            )
            for spec in workflow_specs
            if "command" in spec or "commands" in spec
        }
    )
    material: list[object] = [
        str(chainman.RUNTIME),
        tc.context_id(),
        os.environ.get("CHAINMAN_CONTAINER_PLATFORM", ""),
        os.environ.get("CHAINMAN_NIX_VOLUME", ""),
        declared,
        cfg.get("environment", {}),
        cfg.get("container", {}),
        cfg.get("tasks", {}),
        project_environment.file_fingerprint(
            root, table(cfg.get("environment", {}), "Project environment"), env
        ),
    ]
    material += [
        chainman.profile_fingerprint(root, name, chainman.profile(root, name)[0])
        for name in profiles
    ]
    groups = workflows.order(
        workflows.declarations(cfg, "setup"),
        [
            group
            for spec in workflow_specs
            for group in workflows.names(spec.get("setup", []))
        ],
    )
    material += [
        workflows.fingerprint(root, workflows.group_spec(cfg, name), env)
        for name in groups
    ]
    if include_volume_inputs:
        material += [
            volume_compatibility(root, volume)
            for spec in declared.values()
            for volume in array(
                table(spec.get("container", {}), "Container").get("volumes", []),
                "Service volumes",
            )
        ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def scope_key(host_state: str | Path, root: Path, mode: str) -> str:
    # The host's private cache domain is stable across host/container UID mapping
    # and keeps distinct OS users from claiming the same rootful engine names.
    return hashlib.sha256(
        json.dumps([str(host_state), str(root), mode]).encode()
    ).hexdigest()[:24]


def repository_scope(
    root: Path, host_state: str | Path, declared: Mapping[str, Table]
) -> tuple[Path, str, str] | None:
    shared = {
        name: spec
        for name, spec in declared.items()
        if spec.get("scope") == "repository"
    }
    if not shared:
        return None
    common = Path(
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve(strict=True)
    if not common.is_dir():
        raise ValueError("Repository services require a canonical Git common directory")
    material = [
        str(chainman.RUNTIME),
        shared,
        [
            volume_compatibility(root, volume)
            for spec in shared.values()
            for volume in array(
                table(spec["container"], "Container").get("volumes", []),
                "Service volumes",
            )
        ],
    ]
    return (
        common,
        scope_key(host_state, common, "repository"),
        hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(),
    )


def bridge_scope(root: Path, host_state: str | Path) -> tuple[Path, str]:
    # Only an adopted Git root may share resources with its linked worktrees.
    # An embedded standalone consumer must not borrow an enclosing repository.
    common = root.resolve()
    top = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if top.returncode == 0 and Path(top.stdout.strip()).resolve() == common:
        common = Path(
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-common-dir",
                ],
                text=True,
            ).strip()
        ).resolve(strict=True)
    return common, scope_key(host_state, common, "network")


def export(root: Path, arguments: list[str]) -> int:
    if len(arguments) < 6:
        raise ValueError("Invalid internal controller export request")
    output, target, host_state, engine, launcher, action, *extra = arguments
    destination = Path(output)
    if (
        not destination.is_absolute()
        or destination.is_symlink()
        or not destination.is_dir()
    ):
        raise ValueError("Controller export requires a private directory")
    if target not in {"linux-arm64", "linux-amd64", "darwin-arm64", "darwin-amd64"}:
        raise ValueError("Unsupported host controller platform")
    # Only the verified runtime's flake is evaluated here. Consumer flakes, setup
    # commands, hooks and service commands are deferred to explicit execution.
    with tc.nix_temporary_directory("chainman-export-") as directory:
        package_output = subprocess.run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                tc.nix_path_reference(chainman.RUNTIME / "nix", f"control-{target}"),
                "--out-link",
                str(Path(directory) / "nix-package"),
                "--print-out-paths",
                "--no-write-lock-file",
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        package = Path(package_output)
        if not package.is_absolute() or not str(package).startswith("/nix/store/"):
            raise ValueError("Invalid native controller store output")
        for name in ("chainman-control", "process-compose", "watchexec"):
            source = package / "bin" / name
            if source.is_symlink() or not source.is_file():
                raise ValueError(
                    "Native controller output must be a regular executable"
                )
            tc.atomic_bytes(destination / name, source.read_bytes(), mode=0o700)
        licenses = {
            path.parent.name.replace(".", "-"): path.read_text()
            for path in (package / "share/licenses").glob("*/LICENSE")
        }
    mode = os.environ.get("CHAINMAN_MODE", "host-nix")
    key = scope_key(host_state, root, mode)
    state = str(Path(host_state) / key)
    tc.atomic_bytes(destination / "state", (state + "\n").encode())
    if action in {"services-status", "services-stop", "services-logs"}:
        return 0
    cfg = workflows.configuration(root)
    task = (
        extra[0]
        if action in {"run", "services-run", "services-up", "services-reset"} and extra
        else action
    )
    input_env = project_environment.host_inputs(
        destination, table(cfg.get("environment", {}), "Project environment")
    )
    planning_env = dict(input_env, CHAINMAN_MODE=mode)
    for name in (
        "TOOLCHAIN_DOWNLOAD_CACHE",
        "XDG_CACHE_HOME",
        "CHAINMAN_CONTAINER_NETWORK_MODE",
    ):
        if name in os.environ:
            planning_env[name] = os.environ[name]
    planning_env = workflows.context_environment(root, cfg, task, planning_env)
    compatibility_env = {
        name: value
        for name, value in planning_env.items()
        if not name.startswith(("CHAINMAN_", "TOOLCHAIN_")) and name != "XDG_CACHE_HOME"
    }
    forwarded = {
        name: value
        for name, value in input_env.items()
        if not name.startswith(("CHAINMAN_", "TOOLCHAIN_")) and name != "XDG_CACHE_HOME"
    }
    # Only caller-owned host inputs enter host launcher/engine environments.
    # Project files and expanded declarations remain data until execution inside
    # the selected project lane. Carry the requesting task, not its environment,
    # so services and watched builds can reconstruct the same context there.
    forwarded["CHAINMAN_CONTEXT_TASK"] = task
    declared = declarations(root, cfg)
    fingerprint = config_fingerprint(root, cfg, env=dict(os.environ, **planning_env))
    task_args = (
        extra[1:]
        if action in {"run", "services-run", "services-up", "services-reset"}
        else extra
    )
    if task_args[:1] == ["--"]:
        task_args = task_args[1:]
    tasks_by_name = workflows.declarations(cfg, "tasks")
    task_order = workflows.order(tasks_by_name, [task])
    requested = list(
        dict.fromkeys(
            service
            for name in task_order
            for service in workflows.names(tasks_by_name[name].get("services", []))
        )
    )
    if not requested:
        raise ValueError(f"Task {task} has no declared services")
    closure = workflows.order(declared, requested)
    import service_endpoints

    bridge_root, bridge_key = bridge_scope(root, host_state)
    bridge_name = "chainman-" + bridge_key
    needs_bridge = mode == "container-nix" or any(
        "container" in declared[name] for name in closure
    )
    shared_names = {
        name for name, spec in declared.items() if spec.get("scope") == "repository"
    }
    shared_requested = [name for name in closure if name in shared_names]
    shared_scope = (
        repository_scope(root, host_state, declared) if shared_requested else None
    )
    prepared: dict[str, Service] = {}
    volumes: dict[str, Volume] = {}
    for name, spec in declared.items():
        if name in shared_names and not shared_requested:
            continue
        if name in shared_names:
            assert shared_scope is not None
            service_root, service_key = shared_scope[0], shared_scope[1]
        else:
            service_root, service_key = root, key
        owner = secrets.token_hex(16)
        container_name = "chainman-" + service_key + "-" + name
        env = dict(forwarded) if name not in shared_names else {}
        if "container" in spec:
            if not engine:
                raise ValueError(
                    "Container services require a host Docker or Podman executable"
                )
            item = table(spec["container"], "Service container")
            argv = [
                engine,
                "run",
                "--rm",
                "--init",
                "--name",
                container_name,
                "--label",
                "dev.chainman.owner=" + owner,
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
            ]
            if not spec.get("network_service"):
                argv += [
                    "--network",
                    bridge_name,
                    "--network-alias",
                    service_endpoints.alias(root, name),
                ]
            if item.get("read_only", False):
                argv.append("--read-only")
            if "user" in item:
                argv += ["--user", text(item["user"], "Container user")]
            for port in strings(item.get("ports", []), "Container ports"):
                if not isinstance(port, str) or not port.startswith("127.0.0.1:"):
                    raise ValueError("Service ports must explicitly bind loopback")
                argv += ["--publish", port]
            for key_env, environment_value in literal_environment(
                item.get("environment", {}), root, planning_env
            ).items():
                argv += ["--env", key_env + "=" + environment_value]
            for raw_volume in array(item.get("volumes", []), "Service volumes"):
                volume = table(raw_volume, "Service volume")
                compatibility = volume_compatibility(root, volume)
                workflows.name(volume["name"])
                target_path = volume["target"]
                if (
                    not isinstance(target_path, str)
                    or not target_path.startswith("/")
                    or any(part in {".", ".."} for part in target_path.split("/"))
                    or "," in target_path
                ):
                    raise ValueError("Invalid service volume target")
                argv += [
                    "--mount",
                    f"type=volume,src=chainman-{service_key}-{volume['name']},dst={target_path}",
                ]
                declared_volume = {
                    "engine": engine,
                    "name": f"chainman-{service_key}-{volume['name']}",
                    "scope": service_key,
                    "compatibility": compatibility,
                    "policy": text(volume.get("policy", "preserve"), "Volume policy"),
                }
                previous = volumes.get(declared_volume["name"])
                if (
                    previous
                    and {
                        key: value
                        for key, value in previous.items()
                        if key != "services"
                    }
                    != declared_volume
                ):
                    raise ValueError(
                        "Conflicting declarations for a shared service volume"
                    )
                if previous:
                    previous["services"].append(name)
                else:
                    volumes[declared_volume["name"]] = {
                        "engine": declared_volume["engine"],
                        "name": declared_volume["name"],
                        "scope": declared_volume["scope"],
                        "compatibility": declared_volume["compatibility"],
                        "policy": declared_volume["policy"],
                        "services": [name],
                    }
            argv += [
                text(item["image"], "Container image"),
                *strings(item.get("command", []), "Container command"),
            ]
            launch = command(argv, service_root, env)
            ownership: Container | None = {
                "engine": engine,
                "name": container_name,
                "token": owner,
            }
        else:
            launch = command(
                [launcher, "_workflow-service", name, fingerprint], root, env
            )
            ownership = None
            if mode == "container-nix":
                if not engine:
                    raise ValueError("Container mode requires a host engine")
                launch["environment"].update(
                    CHAINMAN_CONTAINER_NAME=container_name,
                    CHAINMAN_CONTAINER_OWNER=owner,
                    CHAINMAN_CONTAINER_BRIDGE=bridge_name,
                    CHAINMAN_CONTAINER_ALIAS=service_endpoints.alias(root, name),
                )
                ownership = {"engine": engine, "name": container_name, "token": owner}
        value: Service = {
            "command": launch,
            "depends_on": [
                dependency
                for dependency in workflows.names(spec.get("depends_on", []))
                if (dependency in shared_names) == (name in shared_names)
            ],
            "restart": text(spec.get("restart", "no"), "Service restart"),
            "shutdown_seconds": workflows.task_seconds(spec, "shutdown_seconds"),
        }
        if spec.get("network_service") and ownership:
            peer = declared[workflows.name(spec["network_service"])]
            if mode != "container-nix" and "container" not in peer:
                raise ValueError(
                    "A data container cannot borrow a host command's network"
                )
            value["network_service"] = text(spec["network_service"], "Network service")
        if ownership:
            value["container"] = ownership
        if "readiness" in spec:
            probe = table(spec["readiness"], "Service readiness")
            prepared_probe: Probe = {
                "period_seconds": integer(
                    probe.get("period_seconds", 1), "Probe period"
                ),
                "timeout_seconds": integer(
                    probe.get("timeout_seconds", 2), "Probe timeout"
                ),
                "failure_threshold": integer(
                    probe.get("failure_threshold", 30), "Probe failures"
                ),
            }
            if "http_get" in probe:
                prepared_probe["http_get"] = http_readiness(probe["http_get"])
            elif "container" in spec:
                probe_command = [
                    engine,
                    "exec",
                    container_name,
                    *strings(probe["command"], "Readiness command"),
                ]
            elif mode == "container-nix":
                probe_command = [
                    engine,
                    "exec",
                    container_name,
                    launcher,
                    "_workflow-probe",
                    name,
                    fingerprint,
                ]
            else:
                probe_command = [
                    launcher,
                    "_workflow-probe",
                    name,
                    fingerprint,
                ]
            if "http_get" not in probe:
                prepared_probe["command"] = command(probe_command, service_root, env)
            value["readiness"] = prepared_probe
        prepared[name] = value
        if "watch" in spec:
            watch = table(spec["watch"], "Service watch")
            build = command(
                [
                    launcher,
                    "_workflow-task",
                    workflows.name(watch["task"]),
                    fingerprint,
                ],
                root,
                forwarded,
            )
            value["watch"] = {
                "build": build,
                "paths": [
                    str(tc.contained(root, path))
                    for path in strings(watch["paths"], "Watch paths")
                ],
                "ignore": strings(watch.get("ignore", []), "Watch ignores"),
                "debounce_ms": integer(watch.get("debounce_ms", 100), "Watch debounce"),
                "startup_seconds": integer(
                    watch.get("startup_seconds", 300), "Watch startup"
                ),
            }
            if mode == "container-nix":
                build_owner = secrets.token_hex(16)
                build_name = container_name + "-build"
                build["environment"].update(
                    CHAINMAN_CONTAINER_NAME=build_name,
                    CHAINMAN_CONTAINER_OWNER=build_owner,
                    CHAINMAN_CONTAINER_BRIDGE=bridge_name,
                )
                value["watch"]["container"] = {
                    "engine": engine,
                    "name": build_name,
                    "token": build_owner,
                }
    plan: Plan = {
        "schema": 1,
        "root": str(root),
        "state": state,
        "backend": str(destination / "process-compose"),
        "watcher": str(destination / "watchexec"),
        "licenses": licenses,
        "fingerprint": hashlib.sha256(
            json.dumps([fingerprint, compatibility_env], sort_keys=True).encode()
        ).hexdigest(),
        "services": {
            name: spec for name, spec in prepared.items() if name not in shared_names
        },
        "volumes": [volume for volume in volumes.values() if volume["scope"] == key],
        "requested": [name for name in closure if name not in shared_names],
        "prepare": command(
            [launcher, "_workflow-prepare", task, fingerprint], root, forwarded
        ),
        "task": command(
            [launcher, "_workflow-task", task, fingerprint, *task_args], root, forwarded
        ),
        "wait_for_services": any(
            tasks_by_name[name].get("wait_for_services", False) for name in task_order
        ),
        "exclusive_services": any(
            tasks_by_name[name].get("exclusive_services", False) for name in task_order
        ),
        "own_task": True,
        "task_shutdown_seconds": max(
            workflows.task_seconds(tasks_by_name[name], "shutdown_seconds")
            for name in task_order
        ),
    }
    if needs_bridge:
        if not engine:
            raise ValueError(
                "Container services require a host Docker or Podman executable"
            )
        plan["resources"] = [
            {
                "schema": 1,
                "root": str(bridge_root),
                "state": str(Path(host_state) / bridge_key),
                "backend": plan["backend"],
                "licenses": plan["licenses"],
                "fingerprint": hashlib.sha256(
                    str(chainman.RUNTIME).encode()
                ).hexdigest(),
                "services": {},
                "requested": [],
                "bridge": {"engine": engine, "name": bridge_name, "scope": bridge_key},
            }
        ]
    if shared_scope:
        common, shared_key, shared_fingerprint = shared_scope
        plan.setdefault("resources", []).extend(
            [
                {
                    "schema": 1,
                    "root": str(common),
                    "state": str(Path(host_state) / shared_key),
                    "backend": plan["backend"],
                    "licenses": plan["licenses"],
                    "fingerprint": hashlib.sha256(
                        json.dumps(
                            [
                                shared_fingerprint,
                                {
                                    name: literal_environment(
                                        table(
                                            declared[name]["container"],
                                            "Service container",
                                        ).get("environment", {}),
                                        root,
                                        planning_env,
                                    )
                                    for name in shared_names
                                },
                            ],
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                    "services": {
                        name: spec
                        for name, spec in prepared.items()
                        if name in shared_names
                    },
                    "volumes": [
                        volume
                        for volume in volumes.values()
                        if volume["scope"] == shared_key
                    ],
                    "requested": shared_requested,
                    "exclusive_services": plan["exclusive_services"],
                }
            ]
        )
    if mode == "container-nix" and action != "services-up":
        networks = {
            text(tasks_by_name[name]["network_service"], "Task network service")
            for name in task_order
            if tasks_by_name[name].get("network_service")
        }
        if len(networks) > 1:
            raise ValueError(
                "Tasks in one execution must select the same network service"
            )
        if networks:
            plan["task_network_service"] = networks.pop()
        owner = secrets.token_hex(16)
        task_container: Container = {
            "engine": engine,
            "name": "chainman-" + key + "-task-" + owner[:8],
            "token": owner,
        }
        plan["task_container"] = task_container
        plan["task"]["environment"].update(
            CHAINMAN_CONTAINER_NAME=task_container["name"],
            CHAINMAN_CONTAINER_OWNER=owner,
            CHAINMAN_CONTAINER_BRIDGE=bridge_name,
        )
    tc.atomic_json(destination / "plan.json", plan)
    return 0


def prepare_requested(root: Path, arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise ValueError("Service preparation requires one task")
    cfg = workflows.configuration(root)
    entries = declarations(root, cfg)
    import admission

    admission.graph(root, cfg, arguments)
    env = workflows.context_environment(
        root, cfg, arguments[0], tc.environment(root, create=False)
    )
    expected = config_fingerprint(root, cfg, include_volume_inputs=False, env=env)
    result = prepare_setup(root, cfg, entries, arguments[0])
    if config_fingerprint(root, cfg, include_volume_inputs=False, env=env) != expected:
        raise ValueError("Service configuration changed during preparation")
    return result


def prepare_setup(
    root: Path,
    cfg: Mapping[str, object],
    entries: Mapping[str, Table],
    name: str,
    *,
    context_task: str | None = None,
) -> int:
    import admission

    admission.graph(root, cfg, [name])
    task_entries = workflows.declarations(cfg, "tasks")
    tasks = workflows.order(task_entries, [name])
    requested = [
        service
        for task in tasks
        for service in workflows.names(task_entries[task].get("services", []))
    ]
    selected = workflows.order(entries, requested)
    tasks = workflows.order(
        task_entries,
        [
            *tasks,
            *(
                workflows.name(
                    table(entries[service]["watch"], "Service watch")["task"]
                )
                for service in selected
                if "watch" in entries[service]
            ),
        ],
    )
    groups = [
        group
        for task in tasks
        for group in workflows.names(task_entries[task].get("setup", []))
    ]
    groups += [
        group
        for service in selected
        for group in workflows.names(entries[service].get("setup", []))
    ]
    return (
        workflows.run(
            root,
            "setup",
            list(dict.fromkeys(groups)),
            context_task=context_task or name,
            setup_authorized=False,
        )
        if groups
        else 0
    )


def execute_internal(root: Path, action: str, extra: list[str]) -> int:
    if len(extra) < 2:
        raise ValueError("Internal workflow execution requires a name")
    name, expected, *arguments = extra
    cfg = workflows.configuration(root)
    entries = declarations(root, cfg)
    env = tc.environment(root, create=False)
    context_task = os.environ.get("CHAINMAN_CONTEXT_TASK")
    if context_task:
        env = workflows.context_environment(root, cfg, context_task, env)
    if config_fingerprint(root, cfg, env=env) != expected:
        raise ValueError(
            "Service inputs changed after planning; stop existing services before starting the updated workflow"
        )
    if action == "_workflow-task":
        return workflows.run(
            root, name, arguments, service_context=True, context_task=context_task
        )
    if action == "_workflow-prepare":
        result = prepare_setup(root, cfg, entries, name, context_task=context_task)
        if config_fingerprint(root, cfg, env=env) != expected:
            raise ValueError(
                "Service inputs changed during preparation; rerun the workflow"
            )
        return result
    if name not in entries or "command" not in entries[name] or arguments:
        raise ValueError("Invalid internal service execution")
    spec = entries[name]
    with tc.operation(root, exclusive=False, new_execution=True, automatic_prune=False):
        env = tc.environment(root)
        if context_task:
            env = workflows.context_environment(root, cfg, context_task, env)
        env.update(literal_environment(spec.get("environment", {}), root, env))
        with workflows.setup_use(
            root, cfg, workflows.names(spec.get("setup", [])), env
        ) as descriptors:
            profile = text(
                spec.get("profile", workflows.default_profile(cfg)),
                "Service profile",
            )
            directory = text(spec.get("directory", "."), "Service directory")
            if action == "_workflow-probe":
                readiness = table(spec.get("readiness", {}), "Service readiness")
                if "command" not in readiness:
                    raise ValueError("Service has no readiness command")
                # The application owns compiler lifetime. A readiness command
                # uses its declared profile without starting another compiler.
                return chainman.execute(
                    root,
                    profile,
                    strings(readiness["command"], "Readiness command"),
                    env=env,
                    overrides=spec.get("environment", {}),
                    cwd=tc.contained(root, directory),
                    pass_fds=descriptors,
                    check=False,
                ).returncode
            with tc.compiler_cache(profile, env, root) as selected:
                return chainman.execute(
                    root,
                    profile,
                    strings(spec["command"], "Service command"),
                    env=selected,
                    overrides=spec.get("environment", {}),
                    cwd=tc.contained(root, directory),
                    pass_fds=descriptors,
                    check=False,
                ).returncode
