"""Prepare data for the native host ownership adapter, without running project code."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess

import chainman
import toolchain as tc
import workflows
import project_environment


def volume_compatibility(root, volume):
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
    selected = set()
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


def command(argv, root, environment=None):
    workflows.commands([argv])
    selected = dict(environment or {})
    # Preserve architecture when a saved controller plan is restarted by a caller
    # whose environment differs. The bootstrap validates the selected platform.
    for key in ("CHAINMAN_CONTAINER_PLATFORM", "CHAINMAN_NIX_VOLUME"):
        if os.environ.get(key):
            selected[key] = os.environ[key]
    return {"argv": argv, "directory": str(root), "environment": selected}


def declarations(root, cfg):
    entries = cfg.get("services", {})
    if not isinstance(entries, dict):
        raise ValueError("services must contain named declarations")
    for key, spec in entries.items():
        workflows.name(key)
        if not isinstance(spec, dict):
            raise ValueError(f"services.{key} must be a declaration")
        allowed = {
            "command",
            "profile",
            "directory",
            "environment",
            "depends_on",
            "readiness",
            "restart",
            "shutdown_seconds",
            "container",
            "setup",
            "watch",
            "scope",
            "transport",
            "network_service",
        }
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
            chainman.profile(
                root,
                spec.get(
                    "profile", cfg.get("project", {}).get("default_profile", "default")
                ),
            )
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
        tc.contained(root, spec.get("directory", "."))
        workflows.names(spec.get("depends_on", []))
        workflows.order(cfg.get("setup", {}), workflows.names(spec.get("setup", [])))
        if spec.get("restart", "no") not in {"no", "always", "on_failure"}:
            raise ValueError("Invalid service restart policy")
        timeout = spec.get("shutdown_seconds", 10)
        if type(timeout) is not int or not 1 <= timeout <= 300:
            raise ValueError("Service shutdown_seconds must be between 1 and 300")
        if "readiness" in spec:
            probe = spec["readiness"]
            if not isinstance(probe, dict) or set(probe) - {
                "command",
                "period_seconds",
                "timeout_seconds",
                "failure_threshold",
            }:
                raise ValueError("Invalid service readiness declaration")
            workflows.commands([probe.get("command")])
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
            tasks = workflows.order(cfg.get("tasks", {}), [watch.get("task")])
            if any(cfg["tasks"][task].get("services") for task in tasks):
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
            for dependency in spec.get("depends_on", [])
        ):
            raise ValueError("Repository services cannot depend on worktree services")
    for spec in cfg.get("tasks", {}).values():
        workflows.order(entries, workflows.names(spec.get("services", [])))
    for section in (entries, cfg.get("tasks", {})):
        for key, spec in section.items():
            peer = spec.get("network_service")
            if peer is None:
                continue
            workflows.name(peer)
            selected = workflows.order(
                entries,
                spec.get("depends_on", [])
                if section is entries
                else [
                    service
                    for task in workflows.order(cfg.get("tasks", {}), [key])
                    for service in cfg["tasks"][task].get("services", [])
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
            transport = spec.get("container", spec.get("transport", {}))
            if transport.get("ports") or spec.get("transport", {}).get("host_access"):
                raise ValueError("A borrowed network publishes ports only on its owner")
            if cfg.get("container", {}).get("ports") or cfg.get("container", {}).get(
                "host_access"
            ):
                raise ValueError(
                    "Borrowed networks cannot combine global ports or host aliases"
                )
    return entries


def literal_environment(values, root, env=None):
    return project_environment.expand(values, root, os.environ if env is None else env)


def config_fingerprint(root, cfg, *, include_volume_inputs=True, env=None):
    declared = cfg.get("services", {})
    workflow_specs = [*declared.values(), *cfg.get("tasks", {}).values()]
    profiles = sorted(
        {
            spec.get(
                "profile", cfg.get("project", {}).get("default_profile", "default")
            )
            for spec in workflow_specs
            if "command" in spec or "commands" in spec
        }
    )
    material = [
        str(chainman.RUNTIME),
        tc.context_id(),
        os.environ.get("CHAINMAN_CONTAINER_PLATFORM", ""),
        os.environ.get("CHAINMAN_NIX_VOLUME", ""),
        declared,
        cfg.get("environment", {}),
        cfg.get("container", {}),
        cfg.get("tasks", {}),
        project_environment.file_fingerprint(root, cfg.get("environment", {})),
    ]
    material += [
        chainman.profile_fingerprint(root, name, chainman.profile(root, name)[0])
        for name in profiles
    ]
    groups = workflows.order(
        cfg.get("setup", {}),
        [group for spec in workflow_specs for group in spec.get("setup", [])],
    )
    material += [
        workflows.fingerprint(root, workflows.group_spec(cfg, name), env)
        for name in groups
    ]
    if include_volume_inputs:
        material += [
            volume_compatibility(root, volume)
            for spec in declared.values()
            for volume in spec.get("container", {}).get("volumes", [])
        ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def scope_key(host_state, root, mode):
    # The host's private cache domain is stable across host/container UID mapping
    # and keeps distinct OS users from claiming the same rootful engine names.
    return hashlib.sha256(
        json.dumps([str(host_state), str(root), mode]).encode()
    ).hexdigest()[:24]


def repository_scope(root, host_state, declared):
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
            for volume in spec["container"].get("volumes", [])
        ],
    ]
    return (
        common,
        scope_key(host_state, common, "repository"),
        hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(),
    )


def export(root, arguments):
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
    package = subprocess.run(
        [
            tc.nix_command(),
            "--extra-experimental-features",
            "nix-command flakes",
            "build",
            f"path:{chainman.RUNTIME / 'nix'}#control-{target}",
            "--out-link",
            str(destination / "nix-package"),
            "--print-out-paths",
            "--no-write-lock-file",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    package = Path(package)
    if not package.is_absolute() or not str(package).startswith("/nix/store/"):
        raise ValueError("Invalid native controller store output")
    for name in ("chainman-control", "process-compose", "watchexec"):
        source = package / "bin" / name
        if source.is_symlink() or not source.is_file():
            raise ValueError("Native controller output must be a regular executable")
        tc.atomic_bytes(destination / name, source.read_bytes(), mode=0o700)
    mode = os.environ.get("CHAINMAN_MODE", "host-nix")
    key = scope_key(host_state, root, mode)
    state = str(Path(host_state) / key)
    tc.atomic_bytes(destination / "state", (state + "\n").encode())
    if action in {"services-status", "services-stop"}:
        return 0
    cfg = workflows.configuration(root)
    input_env = project_environment.host_inputs(destination, cfg.get("environment", {}))
    planning_env = dict(input_env, CHAINMAN_MODE=mode)
    for name in ("TOOLCHAIN_DOWNLOAD_CACHE", "XDG_CACHE_HOME"):
        if name in os.environ:
            planning_env[name] = os.environ[name]
    planning_env = project_environment.apply(
        root, cfg.get("environment", {}), planning_env
    )
    forwarded = {
        name: value
        for name, value in planning_env.items()
        if not name.startswith(("CHAINMAN_", "TOOLCHAIN_")) and name != "XDG_CACHE_HOME"
    }
    declared = declarations(root, cfg)
    fingerprint = config_fingerprint(root, cfg, env=dict(os.environ, **planning_env))
    task = (
        extra[0]
        if action in {"run", "services-run", "services-up", "services-reset"} and extra
        else action
    )
    task_args = (
        extra[1:]
        if action in {"run", "services-run", "services-up", "services-reset"}
        else extra
    )
    if task_args[:1] == ["--"]:
        task_args = task_args[1:]
    task_order = workflows.order(cfg.get("tasks", {}), [task])
    requested = list(
        dict.fromkeys(
            service
            for name in task_order
            for service in cfg["tasks"][name].get("services", [])
        )
    )
    if not requested:
        raise ValueError(f"Task {task} has no declared services")
    closure = workflows.order(declared, requested)
    shared_names = {
        name for name, spec in declared.items() if spec.get("scope") == "repository"
    }
    shared_requested = [name for name in closure if name in shared_names]
    shared_scope = (
        repository_scope(root, host_state, declared) if shared_requested else None
    )
    prepared = {}
    volumes = {}
    for name, spec in declared.items():
        if name in shared_names and not shared_requested:
            continue
        service_root, service_key = (
            (shared_scope[0], shared_scope[1]) if name in shared_names else (root, key)
        )
        owner = secrets.token_hex(16)
        container_name = "chainman-" + service_key + "-" + name
        env = (
            dict(
                forwarded,
                **literal_environment(spec.get("environment", {}), root, planning_env),
            )
            if name not in shared_names
            else {}
        )
        if "container" in spec:
            if not engine:
                raise ValueError(
                    "Container services require a host Docker or Podman executable"
                )
            item = spec["container"]
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
            if item.get("read_only", False):
                argv.append("--read-only")
            if "user" in item:
                argv += ["--user", item["user"]]
            for port in item.get("ports", []):
                if not isinstance(port, str) or not port.startswith("127.0.0.1:"):
                    raise ValueError("Service ports must explicitly bind loopback")
                argv += ["--publish", port]
            for key_env, value in literal_environment(
                item.get("environment", {}), root, planning_env
            ).items():
                argv += ["--env", key_env + "=" + value]
            for volume in item.get("volumes", []):
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
                    "policy": volume.get("policy", "preserve"),
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
                    volumes[declared_volume["name"]] = dict(
                        declared_volume, services=[name]
                    )
            argv += [item["image"], *item.get("command", [])]
            launch = command(argv, service_root, env)
            ownership = {"engine": engine, "name": container_name, "token": owner}
        else:
            profile = spec.get(
                "profile", cfg.get("project", {}).get("default_profile", "default")
            )
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
                )
                ownership = {"engine": engine, "name": container_name, "token": owner}
        value = {
            "command": launch,
            "depends_on": [
                dependency
                for dependency in spec.get("depends_on", [])
                if (dependency in shared_names) == (name in shared_names)
            ],
            "restart": spec.get("restart", "no"),
            "shutdown_seconds": spec.get("shutdown_seconds", 10),
        }
        if spec.get("network_service") and ownership:
            peer = declared[spec["network_service"]]
            if mode != "container-nix" and "container" not in peer:
                raise ValueError(
                    "A data container cannot borrow a host command's network"
                )
            value["network_service"] = spec["network_service"]
        if ownership:
            value["container"] = ownership
        if "readiness" in spec:
            probe = spec["readiness"]
            if "container" in spec:
                probe_command = [engine, "exec", container_name, *probe["command"]]
            elif mode == "container-nix":
                probe_command = [
                    engine,
                    "exec",
                    container_name,
                    launcher,
                    "exec",
                    "--profile",
                    spec.get(
                        "profile",
                        cfg.get("project", {}).get("default_profile", "default"),
                    ),
                    "--",
                    *probe["command"],
                ]
            else:
                probe_command = [
                    launcher,
                    "exec",
                    "--profile",
                    spec.get(
                        "profile",
                        cfg.get("project", {}).get("default_profile", "default"),
                    ),
                    "--",
                    *probe["command"],
                ]
            value["readiness"] = {
                "command": command(probe_command, service_root, env),
                "period_seconds": probe.get("period_seconds", 1),
                "timeout_seconds": probe.get("timeout_seconds", 2),
                "failure_threshold": probe.get("failure_threshold", 30),
            }
        prepared[name] = value
        if "watch" in spec:
            watch = spec["watch"]
            build = command(
                [launcher, "_workflow-task", watch["task"], fingerprint], root
            )
            value["watch"] = {
                "build": build,
                "paths": [str(tc.contained(root, path)) for path in watch["paths"]],
                "ignore": watch.get("ignore", []),
                "debounce_ms": watch.get("debounce_ms", 100),
                "startup_seconds": watch.get("startup_seconds", 300),
            }
            if mode == "container-nix":
                build_owner = secrets.token_hex(16)
                build_name = container_name + "-build"
                build["environment"].update(
                    CHAINMAN_CONTAINER_NAME=build_name,
                    CHAINMAN_CONTAINER_OWNER=build_owner,
                )
                value["watch"]["container"] = {
                    "engine": engine,
                    "name": build_name,
                    "token": build_owner,
                }
    plan = {
        "schema": 1,
        "root": str(root),
        "state": state,
        "backend": str(destination / "process-compose"),
        "watcher": str(destination / "watchexec"),
        "licenses": {
            path.parent.name.replace(".", "-"): path.read_text()
            for path in (package / "share/licenses").glob("*/LICENSE")
        },
        "fingerprint": hashlib.sha256(
            json.dumps([fingerprint, forwarded], sort_keys=True).encode()
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
            cfg["tasks"][name].get("wait_for_services", False) for name in task_order
        ),
        "exclusive_services": any(
            cfg["tasks"][name].get("exclusive_services", False) for name in task_order
        ),
        "own_task": True,
        "task_shutdown_seconds": max(
            cfg["tasks"][name].get("shutdown_seconds", 10) for name in task_order
        ),
    }
    if shared_scope:
        common, shared_key, shared_fingerprint = shared_scope
        plan["resources"] = [
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
                                    declared[name]["container"].get("environment", {}),
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
    if mode == "container-nix" and action != "services-up":
        networks = {
            cfg["tasks"][name]["network_service"]
            for name in task_order
            if cfg["tasks"][name].get("network_service")
        }
        if len(networks) > 1:
            raise ValueError(
                "Tasks in one execution must select the same network service"
            )
        if networks:
            plan["task_network_service"] = networks.pop()
        owner = secrets.token_hex(16)
        task_container = {
            "engine": engine,
            "name": "chainman-" + key + "-task-" + owner[:8],
            "token": owner,
        }
        plan["task_container"] = task_container
        plan["task"]["environment"].update(
            CHAINMAN_CONTAINER_NAME=task_container["name"],
            CHAINMAN_CONTAINER_OWNER=owner,
        )
    tc.atomic_json(destination / "plan.json", plan)
    return 0


def prepare_requested(root, arguments):
    if len(arguments) != 1:
        raise ValueError("Service preparation requires one task")
    cfg = workflows.configuration(root)
    entries = declarations(root, cfg)
    expected = config_fingerprint(root, cfg, include_volume_inputs=False)
    result = prepare_setup(root, cfg, entries, arguments[0])
    if config_fingerprint(root, cfg, include_volume_inputs=False) != expected:
        raise ValueError("Service configuration changed during preparation")
    return result


def prepare_setup(root, cfg, entries, name):
    tasks = workflows.order(cfg.get("tasks", {}), [name])
    requested = [
        service for task in tasks for service in cfg["tasks"][task].get("services", [])
    ]
    selected = workflows.order(entries, requested)
    tasks = workflows.order(
        cfg.get("tasks", {}),
        [
            *tasks,
            *(
                entries[service]["watch"]["task"]
                for service in selected
                if "watch" in entries[service]
            ),
        ],
    )
    groups = [group for task in tasks for group in cfg["tasks"][task].get("setup", [])]
    groups += [
        group for service in selected for group in entries[service].get("setup", [])
    ]
    return workflows.run(root, "setup", list(dict.fromkeys(groups))) if groups else 0


def execute_internal(root, action, extra):
    if len(extra) < 2:
        raise ValueError("Internal workflow execution requires a name")
    name, expected, *arguments = extra
    cfg = workflows.configuration(root)
    entries = declarations(root, cfg)
    if config_fingerprint(root, cfg) != expected:
        raise ValueError(
            "Service inputs changed after planning; stop existing services before starting the updated workflow"
        )
    if action == "_workflow-task":
        return workflows.run(root, name, arguments, service_context=True)
    if action == "_workflow-prepare":
        result = prepare_setup(root, cfg, entries, name)
        if config_fingerprint(root, cfg) != expected:
            raise ValueError(
                "Service inputs changed during preparation; rerun the workflow"
            )
        return result
    if name not in entries or "command" not in entries[name] or arguments:
        raise ValueError("Invalid internal service execution")
    spec = entries[name]
    with tc.operation(root, exclusive=False, new_execution=True, automatic_prune=False):
        env = tc.environment(root)
        env.update(literal_environment(spec.get("environment", {}), root))
        with workflows.setup_use(root, cfg, spec.get("setup", []), env) as descriptors:
            profile = spec.get(
                "profile", cfg.get("project", {}).get("default_profile", "default")
            )
            with tc.compiler_cache(profile, env, root) as selected:
                return chainman.execute(
                    root,
                    profile,
                    spec["command"],
                    env=selected,
                    overrides=spec.get("environment", {}),
                    cwd=tc.contained(root, spec.get("directory", ".")),
                    pass_fds=descriptors,
                    check=False,
                ).returncode
