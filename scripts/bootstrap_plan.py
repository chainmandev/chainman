"""Project entry projection from the same schema-3 compiler as execution.

Runs only from the hash-verified runtime, before any project command or mount.
The shell remains responsible for enforcing engine and mount ownership policy.
"""

import os
from pathlib import Path
import re
import sys

import config_inspection
import project_environment
import workflows


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
    "setup-status",
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
    "services-status",
    "services-stop",
    "services-run",
    "services-up",
    "services-reset",
}


def line(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\n\r\0"):
        raise ValueError("Bootstrap options require nonempty single-line strings")
    return value


def transport(root, spec):
    project_environment.transport(spec)
    result = []
    for mount in spec.get("mounts", []):
        target = mount.get("target", "")
        if target:
            line(target)
        source = line(mount.get("source_env", mount.get("source")))
        if "," in source + target:
            raise ValueError("Container mount paths cannot contain commas")
        readonly = mount.get("read_only", True)
        if "source_env" in mount:
            result += ["--mount-env", f"{source}:{target}:{'ro' if readonly else 'rw'}"]
        else:
            absolute = Path(source) if Path(source).is_absolute() else root / source
            result += [
                "--mount",
                f"type=bind,src={absolute},dst={target}{',readonly' if readonly else ''}",
            ]
    for port in spec.get("ports", []):
        result += ["--publish", line(port)]
    if spec.get("host_access", False):
        result += ["--add-host", "host.docker.internal:host-gateway"]
    return result


def plan(root, request, name):
    # Recovery consumes saved ownership state, never current declarations. Keep
    # nested native-tool export available through the same read-only transport.
    if request in {"services-status", "services-stop"}:
        return True, ["--controller", "1"]
    if request == "_control-export":
        return False, []
    cfg = config_inspection.validated(root)
    task = name if request == "run" else request
    controller = request in CONTROLLER or (
        request not in INTERNAL
        and not request.startswith("_")
        and task in cfg.get("tasks", {})
        and any(
            cfg["tasks"][key].get("services")
            for key in workflows.order(cfg.get("tasks", {}), [task])
        )
    )
    options = ["--controller", "1"] if controller else []
    patterns = list(cfg.get("environment", {}).get("pass", []))
    for spec in cfg.get("tasks", {}).values():
        patterns += list(spec.get("context_environment", {}))
    for pattern in dict.fromkeys(patterns):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_*?]*", line(pattern)):
            raise ValueError("Invalid environment forwarding pattern")
        options += ["--env-pattern", pattern]
    if request in {"_workflow-service", "_workflow-probe"}:
        selected = cfg.get("services", {}).get(name, {})
    elif request in {"_workflow-task", "run"}:
        selected = cfg.get("tasks", {}).get(name, {})
    else:
        selected = cfg.get("tasks", {}).get(request, {})
    options += transport(root, cfg.get("container", {}))
    options += transport(root, selected.get("transport", {}))
    return controller, options


def main():
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
