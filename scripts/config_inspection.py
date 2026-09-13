"""Read-only public configuration validation and task explanation."""

from copy import deepcopy
import json
import tomllib
from pathlib import Path

import chainman
import configuration
import services
import toolchain as tc
import workflows
import project_environment
import resources
from adapter_data import Table, array, table, text


def validated(root: Path) -> dict[str, object]:
    cfg = tc.config(root)
    project_environment.validate(root, cfg.get("environment", {}))
    project_environment.transport(cfg.get("container", {}))
    resources.validate(cfg.get("resources", {}))
    if cfg["schema"] in (2, 3):
        cfg = workflows.configuration(root)
        services.declarations(root, cfg)
    for profile in table(cfg.get("profiles", {}), "Profiles"):
        _, spec = chainman.profile(root, profile, cfg=cfg)
        project_environment.values(spec.get("environment", {}))
        resources.validate(
            {
                **table(cfg.get("resources", {}), "Project resources"),
                **table(spec.get("resources", {}), "Profile resources"),
            }
        )
    if "recipes" in cfg:
        import recipes

        recipes.bindings(cfg)
        recipes.verification(cfg)
    return configuration.table(cfg, "Validated configuration")


def redacted(value: object, key: str = "") -> object:
    if key in {"environment", "context_environment"} and isinstance(value, dict):
        return {
            name: (
                item
                if name in {"pass", "unset"} and isinstance(item, list)
                else {variable: "<redacted>" for variable in item}
                if isinstance(item, dict)
                else "<redacted>"
            )
            for name, item in table(value, "Environment").items()
        }
    if isinstance(value, dict):
        return {k: redacted(v, k) for k, v in table(value, "Configuration").items()}
    if isinstance(value, list):
        return [redacted(v) for v in value]
    return value


def document(root: Path, action: str, arguments: list[str]) -> Table:
    cfg = validated(root)
    filename = (
        "chainman.toml" if (root / "chainman.toml").exists() else "toolchain.toml"
    )
    _, origins = configuration.compile(
        tomllib.loads(tc.regular_input(tc.configuration_root(root), filename).decode())
    )
    result: Table = {"schema": 1, "configuration_schema": cfg["schema"]}
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
    entries = {
        kind: {
            key: table(value, f"{kind}.{key}")
            for key, value in table(cfg.get(kind, {}), kind).items()
        }
        for kind in configuration.FIELDS
    }
    tasks = workflows.order(entries["tasks"], [selected])
    service_names = workflows.order(
        entries["services"],
        [
            service
            for task in tasks
            for service in workflows.names(entries["tasks"][task].get("services", []))
        ],
    )
    watch_tasks = workflows.order(
        entries["tasks"],
        [
            workflows.name(
                table(entries["services"][service]["watch"], "Watch")["task"]
            )
            for service in service_names
            if "watch" in entries["services"][service]
        ],
    )
    groups = workflows.order(
        entries["setup"],
        [
            group
            for spec in [
                *[
                    entries["tasks"][task]
                    for task in dict.fromkeys([*tasks, *watch_tasks])
                ],
                *[entries["services"][service] for service in service_names],
            ]
            for group in workflows.names(spec.get("setup", []))
        ],
    )
    declarations = {
        kind: {name: deepcopy(entries[kind][name]) for name in names}
        for kind, names in (
            ("tasks", tasks),
            ("services", service_names),
            ("setup", groups),
        )
    }
    if watch_tasks:
        declarations["watch_tasks"] = {
            name: deepcopy(entries["tasks"][name]) for name in watch_tasks
        }
    for service in declarations["services"].values():
        service.setdefault("scope", "worktree")
        service.setdefault("restart", "no")
        service.setdefault("shutdown_seconds", 10)
        if "container" in service:
            container = table(service["container"], "Service container")
            if "volumes" in container:
                volumes = [
                    table(raw, "Service volume")
                    for raw in array(container["volumes"], "Service volumes")
                ]
                for volume in volumes:
                    volume.setdefault("policy", "preserve")
                container["volumes"] = volumes
            service["container"] = container
    default_profile = table(cfg.get("project", {}), "Project").get(
        "default_profile", "default"
    )
    profile_names = sorted(
        {
            text(spec.get("profile", default_profile), "Profile name")
            for declarations_by_name in declarations.values()
            for spec in declarations_by_name.values()
            if "container" not in spec
        }
    )
    declarations["profiles"] = {}
    for name in profile_names:
        ref, spec = chainman.profile(root, name, cfg=cfg)
        declarations["profiles"][name] = dict(
            spec, execution="host" if ref is None else "nix"
        )
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
                for kind, declarations_by_name in declarations.items()
                for name in declarations_by_name
            )
        },
    )


def run(root: Path, action: str, arguments: list[str]) -> None:
    print(json.dumps(document(root, action, arguments), indent=2, sort_keys=True))
