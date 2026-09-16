"""Standard recipe dispatch from project-owned task bindings."""

from collections.abc import Mapping
from pathlib import Path
import re
import shlex
import tomllib

import toolchain as tc
from adapter_data import Table, strings, table


BINDINGS = {
    "setup",
    "generate",
    "format-write",
    "format-check",
    "format-staged",
    "format-hygiene",
    "verify",
    "verify-lite",
    "doctor",
    "clean",
}
BUILTINS = {
    "exec": ["exec", "--"],
    "shell": ["shell"],
    "format": ["format"],
    "format-staged": ["format", "--staged"],
    "deps-update": ["deps-update"],
    "chainman-update": ["chainman-update"],
    "cache-status": ["cache-status"],
    "cache-prune": ["cache-prune"],
    "services-status": ["services-status"],
    "stop": ["services-stop"],
    "setup-status": ["setup-status"],
    "config": ["config"],
    "explain": ["explain"],
    "deps-check": ["deps-check"],
    "deps-coverage": ["deps-coverage"],
    "deps-policy-report": ["deps-policy-report"],
    "deps-audit": ["deps-audit"],
}


def bindings(cfg: Mapping[str, object]) -> dict[str, list[str]]:
    declared = table(cfg.get("recipes", {}), "Standard recipe bindings")
    if set(declared) - BINDINGS:
        raise ValueError("Unknown standard recipe binding")
    available = table(cfg.get("tasks", {}), "Project tasks")
    result = {}
    for name, value in declared.items():
        tasks = strings(value, f"Recipe {name}")
        if any(task not in available for task in tasks):
            raise ValueError(f"Recipe {name} requires declared task names")
        if len(tasks) != len(set(tasks)):
            raise ValueError(f"Recipe {name} repeats a task")
        result[name] = tasks
    return result


def verification(cfg: Mapping[str, object]) -> list[str]:
    declared = bindings(cfg).get("verify")
    updates = table(cfg.get("updates", {}), "Project updates")
    selected = strings(
        updates.get(
            "verify_tasks", [updates["verify_task"]] if "verify_task" in updates else []
        ),
        "Update verification tasks",
    )
    if declared is not None and selected and declared != selected:
        raise ValueError(
            "Recipe verify and update verification must select the same gate"
        )
    return declared if declared is not None else selected


def actions(cfg: Mapping[str, object]) -> dict[str, list[list[str]]]:
    declared = bindings(cfg)
    actions = {name: [argv] for name, argv in BUILTINS.items()}
    for name in BINDINGS - {"format-hygiene"}:
        tasks = verification(cfg) if name == "verify" else declared.get(name, [])
        if name == "setup":
            actions[name] = [["setup"], *[["run", task] for task in tasks]]
        elif tasks:
            actions[name] = [["run", task] for task in tasks]
        elif name == "format-staged":
            continue
        elif name in {
            "generate",
            "format-check",
            "format-write",
            "format-staged",
            "verify",
            "verify-lite",
        }:
            actions[name] = [["_recipe-required", name]]
    actions["doctor"] = [["doctor"], *actions.get("doctor", [])]
    actions["clean"] = [["services-stop"], *actions.get("clean", []), ["clean"]]
    updates = table(cfg.get("updates", {}), "Project updates")
    for target in sorted(
        table(updates.get("adapters", {}), "Update adapters").keys()
        | table(updates.get("target_groups", {}), "Update target groups").keys()
    ):
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", target):
            actions["deps-update-" + target] = [["deps-update", "targets=" + target]]
    return actions


def plan(cfg: Mapping[str, object], name: str) -> str:
    commands = actions(cfg).get(name)
    if commands is None:
        raise ValueError(f"Unknown standard recipe: {name}")
    lines = ["entry=$1", "shift"]
    for index, command in enumerate(commands):
        last = index == len(commands) - 1
        lines.append(
            ("exec " if last else "")
            + '"$entry" '
            + shlex.join(command)
            + (' "$@"' if last else "")
        )
    return "\n".join(lines)


def roots(root: Path) -> list[Path]:
    cfg = tc.config(root)
    runtime = table(cfg.get("runtime", {}), "Project runtime")
    return [
        path
        for path in [
            root,
            *[
                tc.contained(root, name)
                for name in strings(runtime.get("copies", []), "Runtime copies")
            ],
        ]
        if ((path / "chainman.toml").is_file() or (path / "chainman.toml.j2").is_file())
        and "recipes" in config(path)
    ]


def config(root: Path) -> Table:
    if (root / "chainman.toml").is_file():
        return tc.config(root)
    # Templates keep recipe declarations valid TOML; quoted placeholders may
    # appear in unrelated fields such as the project name.
    import configuration

    return configuration.compile(
        tomllib.loads(tc.regular_input(root, "chainman.toml.j2").decode())
    )[0]


def options(arguments: list[str]) -> list[str]:
    """Normalize public options without interpreting values as options or code."""
    main: list[str | None] = []
    resolver: list[str] = []
    pending: list[str | None] | list[str] | None = None
    selected: dict[str, int] = {}
    passthrough = False
    for argument in arguments:
        if pending is not None:
            pending.append(argument)
            pending = None
            continue
        if passthrough:
            resolver.append(argument)
            continue
        if argument == "--":
            passthrough = True
            continue
        if argument in {"--targets", "--policy", "--target-policy"}:
            resolver.append(argument)
            pending = resolver
            continue
        if argument == "--message":
            main.append(argument)
            pending = main
            continue
        key, separator, value = argument.partition("=")
        if not separator:
            main.append(argument)
        elif key in {"--targets", "--policy", "--target-policy"}:
            resolver.append(argument)
        elif key == "--message":
            main.append(argument)
        elif key == "commit" and value in {"auto", "off"}:
            if key in selected:
                main[selected[key]] = None
            selected[key] = len(main)
            main.append("--no-commit" if value == "off" else None)
        elif key == "mode" and value in {"apply", "dry-run"}:
            if key in selected:
                main[selected[key]] = None
            selected[key] = len(main)
            main.append("--preview" if value == "dry-run" else None)
        elif key in {"targets", "policy"}:
            resolver += ["--" + key, value]
        elif key.endswith("_policy") and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*", key[:-7]
        ):
            resolver += ["--target-policy", key[:-7] + "=" + value]
        elif key == "message":
            main += ["--message", value]
        else:
            raise ValueError(f"Unsupported recipe option: {key}")
    if pending is not None:
        raise ValueError("Option requires a value")
    return [item for item in main if item is not None] + (
        ["--", *resolver] if resolver else []
    )


def selection_options(arguments: list[str]) -> list[str]:
    normalized = options(arguments)
    if normalized[:1] == ["--"]:
        return normalized[1:]
    if normalized:
        raise ValueError("Inspection accepts targets and policy options only")
    return []
