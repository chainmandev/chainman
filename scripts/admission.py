"""Side-effect-free admission of declared execution requirements."""

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import platform

from adapter_data import Table, strings, table, text
import toolchain as tc


CHOICES = {
    "allowed_modes": {"host", "host-nix", "container-nix"},
    "allowed_platforms": {"Linux", "Darwin"},
}


def declaration(spec: Mapping[str, object]) -> None:
    for key, choices in CHOICES.items():
        if key in spec:
            values = strings(spec[key], key)
            if not values or len(set(values)) != len(values) or set(values) - choices:
                raise ValueError(f"{key} requires unique values from {sorted(choices)}")


def check(
    spec: Mapping[str, object], label: str, env: Mapping[str, str] | None = None
) -> None:
    declaration(spec)
    env = os.environ if env is None else env
    selected = {
        "allowed_modes": env.get("CHAINMAN_MODE", "host-nix"),
        "allowed_platforms": env.get("CHAINMAN_HOST_PLATFORM", platform.system()),
    }
    for key, value in selected.items():
        if key in spec and value not in strings(spec[key], key):
            raise ValueError(
                f"{label} requires {key}={spec[key]}; selected {value}. "
                "No project setup or command was started."
            )


def profile(
    cfg: Mapping[str, object], name: str, *, env: Mapping[str, str] | None = None
) -> Table:
    spec = table(table(cfg.get("profiles", {}), "Profiles").get(name, {}), "Profile")
    check(spec, f"Profile {name}", env)
    return spec


def graph(
    root: Path,
    cfg: Mapping[str, object],
    names: Sequence[str],
    *,
    groups: Sequence[str] = (),
) -> dict[str, list[str]]:
    import workflows

    tasks = workflows.declarations(cfg, "tasks")
    services = workflows.declarations(cfg, "services")
    setup = workflows.declarations(cfg, "setup")
    selected_tasks = workflows.order(tasks, list(names))
    selected_services: list[str] = []
    # Watch tasks can introduce further services; expand to a fixed point.
    while True:
        selected_services = workflows.order(
            services,
            [
                service
                for name in selected_tasks
                for service in workflows.names(tasks[name].get("services", []))
            ],
        )
        expanded = workflows.order(
            tasks,
            [
                *selected_tasks,
                *[
                    text(table(services[name]["watch"], "Watch")["task"], "Watch task")
                    for name in selected_services
                    if "watch" in services[name]
                ],
            ],
        )
        if expanded == selected_tasks:
            break
        selected_tasks = expanded
    selected_groups = workflows.order(
        setup,
        [
            *groups,
            *[
                group
                for spec in [
                    *[tasks[name] for name in selected_tasks],
                    *[services[name] for name in selected_services],
                ]
                for group in workflows.names(spec.get("setup", []))
            ],
        ],
    )
    for section, entries, selected in (
        ("Task", tasks, selected_tasks),
        ("Service", services, selected_services),
        ("Setup", setup, selected_groups),
    ):
        for name in selected:
            spec = entries[name]
            check(spec, f"{section} {name}")
            if "container" not in spec:
                profile(
                    cfg,
                    text(
                        spec.get("profile", workflows.default_profile(cfg)), "Profile"
                    ),
                )
    if tc.host_mode():
        import host_execution

        host_execution.validate_tasks(tasks, selected_tasks)
        if selected_services:
            raise ValueError("Managed services require host-nix or container-nix")
    return dict(tasks=selected_tasks, services=selected_services, setup=selected_groups)


def entry(root: Path, cfg: Mapping[str, object], name: str) -> list[str]:
    groups = strings(profile(cfg, name).get("entry_setup", []), "Profile entry_setup")
    return graph(root, cfg, [], groups=groups)["setup"]
