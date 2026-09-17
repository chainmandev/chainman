"""Project entry projection from the same schema-3 compiler as execution.

Runs only from the hash-verified runtime, before any project command or mount.
The shell remains responsible for enforcing engine and mount ownership policy.
"""

import os
from pathlib import Path
import re
import sys

import config_inspection
import chainman
import project_environment
import execution_transport
import workflows
from adapter_data import array, strings, table, text


INTERNAL = {
    "_control-export",
    "_workflow-task",
    "_workflow-service",
    "_workflow-probe",
    "_workflow-prepare",
    "_service-prepare",
    "exec",
    "shell",
    "version",
    "doctor",
    "config",
    "explain",
    "preflight",
    "setup-status",
    "format-staged",
    "hooks",
    "trojan-source",
    "setup",
    "deps-query",
    "deps-resolve",
    "deps-update",
    "chainman-update",
    "deps-check",
    "clean",
    "cache-prune",
    "cache-status",
}
CONTROLLER = {
    "services-logs",
    "services-status",
    "services-stop",
    "services-run",
    "services-up",
    "services-reset",
}


def line(value: object) -> str:
    if not isinstance(value, str) or not value or any(c in value for c in "\n\r\0"):
        raise ValueError("Bootstrap options require nonempty single-line strings")
    return value


def transport(
    root: Path, value: object, env: dict[str, str] | None = None
) -> list[str]:
    project_environment.transport(value)
    spec = table(value, "Container transport")
    result: list[str] = []
    for raw in array(spec.get("mounts", []), "Container mounts"):
        mount = table(raw, "Container mount")
        target = text(mount.get("target", ""), "Mount target")
        if target:
            line(target)
        source = line(mount.get("source_env", mount.get("source")))
        if "," in source + target:
            raise ValueError("Container mount paths cannot contain commas")
        readonly = mount.get("read_only", True)
        if "source_env" in mount:
            result += [
                "--mount-env-optional" if mount.get("optional") else "--mount-env",
                f"{source}:{target}:{'ro' if readonly else 'rw'}",
            ]
        else:
            absolute = Path(source) if Path(source).is_absolute() else root / source
            result += [
                "--mount-optional" if mount.get("optional") else "--mount",
                f"type=bind,src={absolute},dst={target}{',readonly' if readonly else ''}",
            ]
    for port in strings(spec.get("ports", []), "Container ports"):
        result += ["--publish", project_environment.transport_port(port, env or {})]
    if spec.get("host_access", False):
        result += ["--add-host", "host.docker.internal:host-gateway"]
    if spec.get("display"):
        result += ["--display", "x11"]
    return result


def plan(root: Path, request: str, name: str) -> tuple[bool, list[str]]:
    # Recovery consumes saved ownership state, never current declarations. Keep
    # nested native-tool export available through the same read-only transport.
    if request in {"services-status", "services-stop", "services-logs"}:
        return True, ["--controller", "1"]
    if request == "_control-export":
        return False, []
    cfg = config_inspection.validated(root)
    tasks = table(cfg.get("tasks", {}), "Tasks")
    services = table(cfg.get("services", {}), "Services")
    task = name if request == "run" else request
    if task in tasks and (request == "run" or request not in INTERNAL):
        import admission

        admission.graph(root, cfg, [task])
    controller = request in CONTROLLER or (
        request not in INTERNAL
        and not request.startswith("_")
        and task in tasks
        and any(
            table(tasks[key], "Task").get("services")
            for key in workflows.order(tasks, [task])
        )
    )
    options = ["--controller", "1"] if controller else []
    patterns = strings(
        table(cfg.get("environment", {}), "Project environment").get("pass", []),
        "Environment pass patterns",
    )
    for raw in tasks.values():
        spec = table(raw, "Task")
        patterns += list(table(spec.get("context_environment", {}), "Task context"))
    for pattern in dict.fromkeys(patterns):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_*?]*", line(pattern)):
            raise ValueError("Invalid environment forwarding pattern")
        options += ["--env-pattern", pattern]
    if request in {"_workflow-service", "_workflow-probe"}:
        selected = table(services.get(name, {}), "Service")
    elif request in {"_workflow-task", "run"}:
        selected = table(tasks.get(name, {}), "Task")
    else:
        selected = table(tasks.get(request, {}), "Task")
    executable = (
        request
        in {
            "exec",
            "shell",
            "_workflow-task",
            "_workflow-service",
            "_workflow-probe",
            "run",
        }
        or request in tasks
    )
    if request in {"exec", "shell"} and name == "--reuse-operation":
        executable = False
    requested_profile = (
        os.environ.get("CHAINMAN_REQUEST_PROFILE")
        if request in {"exec", "shell"}
        else None
    )
    transports = (
        [execution_transport.effective(cfg, selected, profile=requested_profile)]
        if executable
        else [table(cfg.get("container", {}), "Container transport")]
        if request
        in {
            "setup",
            "_transport-prepare",
            "_service-prepare",
            "_workflow-prepare",
            "_workflow-task",
        }
        else []
    )
    # Admit graphical requirements across a graph before setup or services.
    if controller or request == "preflight":
        import admission

        graph = admission.graph(
            root,
            cfg,
            os.environ.get("CHAINMAN_PREFLIGHT_TASKS", name).splitlines()
            if request == "preflight"
            else [
                name
                if request in {"services-up", "services-run", "services-reset"}
                else task
            ],
        )
        if any(
            execution_transport.effective(cfg, table(entries[key], "Execution")).get(
                "display"
            )
            for section, entries in (("tasks", tasks), ("services", services))
            for key in graph[section]
            if "container" not in table(entries[key], "Execution")
        ):
            options += ["--display-check", "x11"]
    if (
        executable
        and task in tasks
        and os.environ.get("CHAINMAN_MODE", "container-nix") == "container-nix"
    ):
        for dependency in workflows.order(tasks, [task]):
            entry = table(tasks[dependency], "Task")
            if entry.get("commands") and not execution_transport.equivalent(
                execution_transport.effective(cfg, entry), transports[0]
            ):
                raise ValueError(
                    "Tasks in one container execution require identical transport; start differently scoped tasks separately from the host"
                )
    env = None
    if any(
        "{" in port
        for value in transports
        for port in strings(table(value, "Transport").get("ports", []), "Ports")
    ):
        inherited = dict(os.environ)
        if os.environ.get("CHAINMAN_BOOTSTRAP_INPUTS"):
            forwarded = project_environment.host_inputs(
                Path(os.environ["CHAINMAN_BOOTSTRAP_INPUTS"]),
                table(cfg.get("environment", {}), "Environment"),
            )
            inherited.update(
                {
                    key: value
                    for key, value in forwarded.items()
                    if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
                }
            )
        env = (
            workflows.context_environment(root, cfg, task, inherited)
            if task in tasks
            else inherited
        )
        _, profile = chainman.profile(
            root,
            requested_profile
            or text(selected.get("profile", workflows.default_profile(cfg)), "Profile"),
            cfg=cfg,
        )
        env = chainman.profile_environment(
            root, profile, env, selected.get("environment", {}), cfg=cfg
        )
    for value in transports:
        if any(value.get(key) for key in ("mounts", "ports", "host_access", "display")):
            import json

            options += ["--transport-declaration", json.dumps(value, sort_keys=True)]
            if executable:
                options += ["--transport-readiness", "error"]
            if request in {"exec", "shell", "run"} or request in tasks:
                options += [
                    "--transport-prepare",
                    requested_profile or workflows.default_profile(cfg),
                ]
        options += transport(root, value, env)
    return controller, options


def main() -> None:
    root, action = sys.argv[1:]
    controller, options = plan(
        Path(root),
        os.environ.get("CHAINMAN_REQUEST_ACTION", ""),
        os.environ.get("CHAINMAN_REQUEST_TASK", ""),
    )
    if action == "route":
        print("1" if controller else "0")
    elif action == "options":
        if options:
            print("\n".join(map(line, options)))
    else:
        raise ValueError("Unknown bootstrap projection")


if __name__ == "__main__":
    main()
