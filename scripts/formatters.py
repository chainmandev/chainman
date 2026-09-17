"""Explicit file formatters: no task graphs, lint fixes or generation."""

from collections.abc import Callable, Mapping
import fnmatch
from pathlib import Path
import os
import subprocess

import chainman
import toolchain as tc
import workflows
from adapter_data import Table, strings, table, text


def declarations(cfg: Mapping[str, object]) -> dict[str, Table]:
    result = {}
    for name, raw in table(cfg.get("formatters", {}), "Formatters").items():
        workflows.name(name)
        spec = table(raw, f"Formatter {name}")
        if set(spec) - {
            "paths",
            "exclude",
            "profile",
            "setup",
            "write",
            "check",
            "stdin",
        }:
            raise ValueError(f"Unknown field in formatter {name}")
        if not strings(spec.get("paths"), "Formatter paths"):
            raise ValueError(f"Formatter {name} needs paths")
        strings(spec.get("exclude", []), "Formatter exclusions")
        for action in ("write", "check"):
            argv = strings(spec.get(action), f"Formatter {action}")
            if not argv or not argv[0] or any("\0" in value for value in argv):
                raise ValueError(f"Formatter {name} needs {action} arguments")
        if type(spec.get("stdin", False)) is not bool:
            raise ValueError("Formatter stdin must be boolean")
        text(spec.get("profile", "core"), "Formatter profile")
        groups = workflows.names(spec.get("setup", []))
        workflows.order(workflows.declarations(cfg, "setup"), groups)
        result[name] = spec
    return result


def matches(path: str, patterns: list[str]) -> bool:
    # Like Git's familiar **/ forms, a leading **/ also matches the root.
    return any(
        fnmatch.fnmatchcase(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:]))
        for pattern in patterns
    )


def selected(spec: Mapping[str, object], paths: list[str]) -> list[str]:
    return [
        path
        for path in paths
        if matches(path, strings(spec["paths"], "Formatter paths"))
        and not matches(path, strings(spec.get("exclude", []), "Formatter exclusions"))
    ]


def execute(
    root: Path,
    cfg: Mapping[str, object],
    paths: list[str],
    *,
    check: bool = False,
    prepared: Callable[[], None] | None = None,
) -> None:
    """Run only matching formatters; paths are literal argv, never shell text.

    The caller owns isolation and checks the output inventory. Prefixing ./ also
    protects tools without a conventional -- separator from leading dashes.
    """
    plan = [(spec, selected(spec, paths)) for spec in declarations(cfg).values()]
    plan = [(spec, files) for spec, files in plan if files]
    if not plan:
        return
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    with tc.operation(root, exclusive=False, new_execution=True, automatic_prune=False):
        env.update(tc.environment(root))
        # tc.environment includes the caller environment; don't route a candidate
        # formatter back to the original index, even when called from commit -a.
        for key in list(env):
            if key.startswith("GIT_"):
                env.pop(key)
        env.update(
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_SYSTEM="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
        )
        groups = list(
            dict.fromkeys(
                group
                for spec, _ in plan
                for group in workflows.names(spec.get("setup", []))
            )
        )
        # Formatting owns this isolated candidate and authorizes only its declared
        # formatter groups, just as an isolated update/format transaction does.
        with workflows.setup_use(root, cfg, groups, env, explicit=True) as descriptors:
            if prepared is not None:
                prepared()
            for spec, files in plan:
                argv = strings(spec["check" if check else "write"], "Formatter command")
                if spec.get("stdin", False):
                    for path in files:
                        result = chainman.execute(
                            root,
                            text(spec.get("profile", "core"), "Formatter profile"),
                            argv,
                            env=env,
                            pass_fds=descriptors,
                            input=tc.regular_input(root, path),
                            stdout=subprocess.PIPE,
                        )
                        if not check:
                            # Keep the staged executable mode; isolation/inventory
                            # validation remains the transaction caller's job.
                            tc.contained(root, path).write_bytes(result.stdout)
                    continue
                for start in range(0, len(files), 64):
                    chainman.execute(
                        root,
                        text(spec.get("profile", "core"), "Formatter profile"),
                        [*argv, *("./" + path for path in files[start : start + 64])],
                        env=env,
                        pass_fds=descriptors,
                        stdin=subprocess.DEVNULL,
                    )
