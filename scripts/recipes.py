"""Standard recipe dispatch from project-owned task bindings."""

from collections.abc import Mapping
from pathlib import Path
import re
import shlex
import hashlib
import json
import os
import subprocess

import toolchain as tc
from adapter_data import Table, strings, table


BINDINGS = {
    "setup",
    "generate",
    "format-write",
    "format-check",
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
    "format-staged": ["format-staged"],
    "hooks": ["hooks"],
    "trojan-source": ["trojan-source"],
    "deps-update": ["deps-update"],
    "candidate-check": ["candidate-check"],
    "chainman-update": ["chainman-update"],
    "cache-status": ["cache-status"],
    "cache-prune": ["cache-prune"],
    "services-status": ["services-status"],
    "logs": ["services-logs"],
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
            actions[name] = [["setup"]]
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


def verification_identity(root: Path) -> dict[str, str]:
    """Hash source bytes, never expose their contents in diagnostic reports."""

    def git(directory: Path, *arguments: str) -> str:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") or key == "GIT_SSL_CAINFO"
        }
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_OPTIONAL_LOCKS="0",
            GIT_NO_REPLACE_OBJECTS="1",
        )
        return subprocess.check_output(
            [
                "git",
                "--literal-pathspecs",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-C",
                str(directory),
                *arguments,
            ],
            env=env,
        ).decode()

    def snapshot(directory: Path) -> dict[str, str]:
        state = {
            "@index": hashlib.sha256(
                git(directory, "ls-files", "--stage", "-z").encode()
            ).hexdigest()
        }
        for name in sorted(
            set(
                git(
                    directory,
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ).split("\0")
            )
            - {""}
        ):
            path = directory / name
            digest = hashlib.sha256()
            if path.is_symlink():
                digest.update(b"symlink\0" + os.readlink(path).encode())
            elif path.is_file():
                digest.update(str(path.stat().st_mode & 0o777).encode() + b"\0")
                with path.open("rb") as stream:
                    while block := stream.read(65536):
                        digest.update(block)
            elif path.is_dir():
                nested_root = Path(
                    git(path, "rev-parse", "--show-toplevel").strip()
                ).resolve()
                if nested_root == path.resolve():
                    digest.update(git(path, "rev-parse", "HEAD").encode())
                    digest.update(json.dumps(snapshot(path), sort_keys=True).encode())
                elif any(path.iterdir()):
                    raise ValueError(
                        "Uninitialized source submodule contains unexpected files"
                    )
                else:
                    digest.update(b"uninitialized-submodule")
            elif not path.exists():
                continue
            else:
                raise ValueError("Unexpected verification source entry")
            state[name] = digest.hexdigest()
        return state

    return {
        "commit": git(root, "rev-parse", "HEAD").strip(),
        "source_fingerprint": hashlib.sha256(
            json.dumps(snapshot(root), sort_keys=True).encode()
        ).hexdigest(),
        "runtime_pin": tc.regular_input(root, "chainman.lock").decode().strip(),
    }


def verification_plan(
    root: Path,
    cfg: Mapping[str, object],
    commands: list[list[str]],
    launcher: str,
    identity_launcher: str,
) -> Table:
    """Describe existing finite task entries, without changing their execution."""
    import admission
    import workflows

    tasks = workflows.declarations(cfg, "tasks")
    selected = [argv[1] for argv in commands if argv[:1] == ["run"]]
    admission.graph(root, cfg, selected)
    dependencies = {
        task: [name for name in workflows.order(tasks, [task]) if name != task]
        for task in selected
    }
    positions = {task: index for index, task in enumerate(selected)}
    for task, required in dependencies.items():
        if any(
            name in positions and positions[name] >= positions[task]
            for name in required
        ):
            raise ValueError("Verification prerequisites must precede their gates")
    if not commands or any(
        len(argv) != 2 or argv[0] not in {"run", "_update-verify"} for argv in commands
    ):
        raise ValueError("Keep-going requires finite verification entries")
    if any(
        tasks[name].get("wait_for_services", False)
        for name in workflows.order(tasks, selected)
    ):
        raise ValueError("Keep-going requires finite verification tasks")
    return {
        "schema": 1,
        "root": str(root),
        "identity_command": [identity_launcher, "_verification-identity"],
        "health_command": [launcher, "services-status"],
        "identity": verification_identity(root),
        "gates": [
            {
                "name": argv[1],
                "argv": [launcher, *argv],
                "depends_on": [
                    name for name in dependencies.get(argv[1], []) if name in positions
                ],
            }
            for argv in commands
        ],
    }


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
    import configuration_files

    return configuration.compile(
        configuration_files.read(root, "chainman.toml.j2").data
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
