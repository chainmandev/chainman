"""Read-only public configuration validation and task explanation."""

from copy import deepcopy
import json
import tomllib

import chainman
import configuration
import services
import toolchain as tc
import workflows


def validated(root):
    cfg = tc.config(root)
    if cfg["schema"] in (2, 3):
        cfg = workflows.configuration(root)
        services.declarations(root, cfg)
    for profile in cfg.get("profiles", {}):
        chainman.profile(root, profile, cfg=cfg)
    return cfg


def redacted(value, key=""):
    if key in {"environment", "context_environment"} and isinstance(value, dict):
        return {
            name: (
                item
                if name in {"pass", "unset"} and isinstance(item, list)
                else {variable: "<redacted>" for variable in item}
                if isinstance(item, dict)
                else "<redacted>"
            )
            for name, item in value.items()
        }
    if isinstance(value, dict):
        return {k: redacted(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


def document(root, action, arguments):
    cfg = validated(root)
    filename = (
        "chainman.toml" if (root / "chainman.toml").exists() else "toolchain.toml"
    )
    _, origins = configuration.compile(
        tomllib.loads(tc.regular_input(root, filename).decode())
    )
    result = {"schema": 1, "configuration_schema": cfg["schema"]}
    if action == "config":
        if arguments == ["validate"]:
            return dict(result, valid=True)
        if arguments != ["show", "--json"]:
            raise ValueError("Use config validate or config show --json")
        return dict(result, configuration=redacted(cfg), origins=origins)
    if len(arguments) not in (1, 2) or (
        len(arguments) == 2 and arguments[1] != "--json"
    ):
        raise ValueError("Use explain TASK [--json]")
    selected = workflows.name(arguments[0])
    tasks = workflows.order(cfg.get("tasks", {}), [selected])
    service_names = workflows.order(
        cfg.get("services", {}),
        [
            service
            for task in tasks
            for service in cfg["tasks"][task].get("services", [])
        ],
    )
    watch_tasks = workflows.order(
        cfg.get("tasks", {}),
        [
            cfg["services"][service]["watch"]["task"]
            for service in service_names
            if "watch" in cfg["services"][service]
        ],
    )
    groups = workflows.order(
        cfg.get("setup", {}),
        [
            group
            for spec in [
                *[cfg["tasks"][task] for task in dict.fromkeys([*tasks, *watch_tasks])],
                *[cfg["services"][service] for service in service_names],
            ]
            for group in spec.get("setup", [])
        ],
    )
    declarations = {
        kind: {name: deepcopy(cfg[kind][name]) for name in names}
        for kind, names in (
            ("tasks", tasks),
            ("services", service_names),
            ("setup", groups),
        )
    }
    if watch_tasks:
        declarations["watch_tasks"] = {
            name: deepcopy(cfg["tasks"][name]) for name in watch_tasks
        }
    for service in declarations["services"].values():
        service.setdefault("scope", "worktree")
        service.setdefault("restart", "no")
        service.setdefault("shutdown_seconds", 10)
        for volume in service.get("container", {}).get("volumes", []):
            volume.setdefault("policy", "preserve")
    profile_names = sorted(
        {
            spec.get(
                "profile", cfg.get("project", {}).get("default_profile", "default")
            )
            for entries in declarations.values()
            for spec in entries.values()
            if "container" not in spec
        }
    )
    declarations["profiles"] = {
        name: cfg.get("profiles", {}).get(
            name, {"runtime_profile": "core" if name == "default" else name}
        )
        for name in profile_names
    }
    return dict(
        result,
        task=selected,
        order={
            "tasks": tasks,
            "services": service_names,
            "setup": groups,
            **({"watch_tasks": watch_tasks} if watch_tasks else {}),
        },
        declarations=redacted(declarations),
        environment=redacted(cfg.get("environment", {}), "environment"),
        origins={
            key: value
            for key, value in origins.items()
            if any(
                key == f"{'tasks' if kind == 'watch_tasks' else kind}.{name}"
                for kind, entries in declarations.items()
                for name in entries
            )
        },
    )


def run(root, action, arguments):
    print(json.dumps(document(root, action, arguments), indent=2, sort_keys=True))
