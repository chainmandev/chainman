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


def command(argv, root, environment=None):
    workflows.commands([argv])
    return {"argv": argv, "directory": str(root), "environment": environment or {}}


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
        }
        if set(spec) - allowed:
            raise ValueError(f"Unknown service fields: {sorted(set(spec) - allowed)}")
        if ("command" in spec) == ("container" in spec):
            raise ValueError(
                "A service requires exactly one command or container declaration"
            )
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
    for spec in cfg.get("tasks", {}).values():
        workflows.order(entries, workflows.names(spec.get("services", [])))
    return entries


def literal_environment(values, root):
    import re

    if not isinstance(values, dict):
        raise ValueError("Service environment must be a table")
    result = {}
    for key, value in values.items():
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            or key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
            or not isinstance(value, str)
            or "\0" in value
        ):
            raise ValueError("Invalid service environment variable")
        result[key] = value.replace("{root}", str(root))
    return result


def config_fingerprint(root, cfg):
    declared = cfg.get("services", {})
    build_tasks = workflows.order(
        cfg.get("tasks", {}),
        [spec["watch"]["task"] for spec in declared.values() if "watch" in spec],
    )
    workflow_specs = [*declared.values(), *(cfg["tasks"][task] for task in build_tasks)]
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
        declared,
        cfg.get("environment", {}),
        cfg.get("container", {}),
        cfg.get("tasks", {}),
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
        workflows.fingerprint(root, workflows.group_spec(cfg, name)) for name in groups
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


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
            "--no-link",
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
    key = hashlib.sha256((str(root) + "\0" + mode).encode()).hexdigest()[:24]
    state = str(Path(host_state) / key)
    tc.atomic_bytes(destination / "state", (state + "\n").encode())
    if action in {"services-status", "services-stop"}:
        return 0
    cfg = workflows.configuration(root)
    declared = declarations(root, cfg)
    fingerprint = config_fingerprint(root, cfg)
    task = (
        extra[0]
        if action in {"run", "services-run", "services-up"} and extra
        else action
    )
    task_args = extra[1:] if action in {"run", "services-run", "services-up"} else extra
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
    prepared = {}
    for name, spec in declared.items():
        owner = secrets.token_hex(16)
        container_name = "chainman-" + key + "-" + name
        env = literal_environment(spec.get("environment", {}), root)
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
                item.get("environment", {}), root
            ).items():
                argv += ["--env", key_env + "=" + value]
            for volume in item.get("volumes", []):
                if not isinstance(volume, dict) or set(volume) != {"name", "target"}:
                    raise ValueError(
                        "Service volumes require a declared name and target"
                    )
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
                    f"type=volume,src=chainman-{key}-{volume['name']},dst={target_path}",
                ]
            argv += [item["image"], *item.get("command", [])]
            launch = command(argv, root, env)
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
            "depends_on": spec.get("depends_on", []),
            "restart": spec.get("restart", "no"),
            "shutdown_seconds": spec.get("shutdown_seconds", 10),
        }
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
                "command": command(probe_command, root, env),
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
        "fingerprint": fingerprint,
        "services": prepared,
        "requested": requested,
        "prepare": command([launcher, "_workflow-prepare", task, fingerprint], root),
        "task": command(
            [launcher, "_workflow-task", task, fingerprint, *task_args], root
        ),
    }
    if mode == "container-nix" and action != "services-up":
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
        tasks = workflows.order(cfg.get("tasks", {}), [name])
        requested = [
            service
            for task in tasks
            for service in cfg["tasks"][task].get("services", [])
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
        groups = [
            group for task in tasks for group in cfg["tasks"][task].get("setup", [])
        ]
        groups += [
            group for service in selected for group in entries[service].get("setup", [])
        ]
        return (
            workflows.run(root, "setup", list(dict.fromkeys(groups))) if groups else 0
        )
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
                    cwd=tc.contained(root, spec.get("directory", ".")),
                    pass_fds=descriptors,
                    check=False,
                ).returncode
