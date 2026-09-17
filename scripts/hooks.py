"""Worktree-owned Git bridges and a pinned, composable lefthook preset."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import chainman
import staged_format
import toolchain as tc
from adapter_data import Table, table, text

EVENTS = ("pre-commit", "pre-push")
MARKER = "# chainman Git hook bridge v1\n"


def declaration(cfg: Mapping[str, object]) -> Table:
    spec = table(cfg.get("hooks", {}), "Hooks")
    if set(spec) - {"enabled", "config", "trojan_source"}:
        raise ValueError("Unknown hooks setting")
    if type(spec.get("enabled", False)) is not bool:
        raise ValueError("hooks.enabled must be boolean")
    if "config" in spec:
        text(spec["config"], "Hook configuration")
    return spec


def git(root: Path, *args: str, check: bool = True) -> str:
    return os.fsdecode(staged_format.git(root, *args, check=check).stdout).rstrip("\n")


def repository(root: Path) -> bool:
    actual = git(root, "rev-parse", "--show-toplevel", check=False)
    return bool(actual) and Path(actual).resolve() == root


def directory(root: Path) -> Path:
    path = Path(git(root, "rev-parse", "--absolute-git-dir")) / "chainman-hooks"
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Hook administration must not contain symlinks")
    return path


def bridge(event: str) -> bytes:
    return (
        "#!/bin/sh\n" + MARKER + "set -eu\n"
        "root=$(git rev-parse --show-toplevel)\n"
        'exec just --justfile "$root/justfile" --working-directory "$root" chainman hooks run '
        + event
        + ' "$@"\n'
    ).encode()


def current_path(root: Path) -> str:
    # Read effective system/global configuration too. Installation must detect
    # an external manager rather than silently overriding its hook path.
    result = subprocess.run(
        ["git", "-C", str(root), "config", "--path", "--get", "core.hooksPath"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise ValueError("Cannot read effective Git hook configuration")
    return result.stdout.rstrip("\n")


def status(root: Path) -> dict[str, object]:
    if not repository(root):
        return {"applicable": False, "reason": "not a Git project root"}
    target = directory(root)
    selected = current_path(root)
    intact = all(
        (target / event).is_file()
        and not (target / event).is_symlink()
        and (target / event).read_bytes() == bridge(event)
        and os.access(target / event, os.X_OK)
        for event in EVENTS
    )
    return {
        "applicable": True,
        "installed": selected == str(target) and intact,
        "path": str(target),
        "selected_path": selected,
    }


def check_installation(root: Path) -> None:
    if not repository(root):
        return
    target = directory(root)
    selected = current_path(root)
    if selected and selected != str(target):
        raise ValueError(
            f"Git hooks are managed at {selected!r}. Review and remove that core.hooksPath setting before `just hooks install`; existing hooks were preserved. For disposable CI use `just setup --no-hooks`."
        )
    default = (
        Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        / "hooks"
    )
    if (
        not selected
        and default.is_dir()
        and any(
            p.is_file() and not p.name.endswith(".sample") for p in default.iterdir()
        )
    ):
        raise ValueError(
            f"Existing Git hooks at {default}; preserve or explicitly move them before `just hooks install`"
        )
    for event in EVENTS:
        path = target / event
        if path.exists() or path.is_symlink():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.read_bytes() != bridge(event)
            ):
                raise ValueError(f"Refusing to replace modified hook: {path}")


def install(root: Path) -> None:
    check_installation(root)
    if not repository(root):
        print("Git hooks: not applicable outside a Git project root")
        return
    target = directory(root)
    target.mkdir(mode=0o700, exist_ok=True)
    # Worktree config is additive; never erase common core.hooksPath policy.
    git(root, "config", "--local", "extensions.worktreeConfig", "true")
    for event in EVENTS:
        tc.atomic_bytes(target / event, bridge(event), 0o755)
    git(root, "config", "--worktree", "core.hooksPath", str(target))
    if not status(root).get("installed"):
        raise ValueError("Git did not select the installed hook bridges")
    print("Git hooks installed (format staged content; scan outgoing commits)")


def uninstall(root: Path) -> None:
    if not repository(root):
        return
    target = directory(root)
    if current_path(root) != str(target):
        raise ValueError(
            "The selected hook path is not owned by chainman; nothing removed"
        )
    for event in EVENTS:
        path = target / event
        if (
            path.is_symlink()
            or not path.is_file()
            or path.read_bytes() != bridge(event)
        ):
            raise ValueError(f"Modified hook preserved: {path}")
    git(root, "config", "--worktree", "--unset", "core.hooksPath")
    for event in EVENTS:
        (target / event).unlink()
    print("Owned hook bridges removed; other Git configuration preserved")


def effective(root: Path, target: Path) -> Path:
    spec = declaration(tc.config(root))
    command = '"$CHAINMAN_HOOK_ENTRY" '
    config: dict[str, object] = {
        "no_auto_install": True,
        "pre-commit": {
            "parallel": False,
            "commands": {"format-staged": {"run": command + "format-staged"}},
        },
        "pre-push": {
            "parallel": False,
            "commands": {"trojan-source": {"run": command + "trojan-source"}},
        },
    }
    if "config" in spec:
        path = text(spec["config"], "Hook config")
        tc.regular_input(root, path)
        config["extends"] = [str(root / path)]
    output = target / "lefthook.json"
    tc.atomic_bytes(output, (json.dumps(config, indent=2) + "\n").encode())
    return output


def execute(root: Path, arguments: list[str]) -> int:
    if arguments == ["status"]:
        print(json.dumps(status(root), indent=2))
        return 0
    if arguments == ["uninstall"]:
        uninstall(root)
        return 0
    if arguments not in (["install"], ["config"]) and (
        len(arguments) < 2 or arguments[:1] != ["run"] or arguments[1] not in EVENTS
    ):
        raise ValueError(
            "Use hooks install|status|uninstall|config or hooks run pre-commit|pre-push"
        )
    if not declaration(tc.config(root)).get("enabled", False):
        raise ValueError(
            "Declare [hooks] enabled=true before installing or running the preset"
        )
    if arguments == ["install"] and not repository(root):
        install(root)
        return 0
    target = tc.contained(root, ".cache/toolchain/hooks")
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=target) as temporary:
        work = Path(temporary)
        config = effective(root, work)
        env = dict(
            os.environ,
            LEFTHOOK_CONFIG=str(config),
            CHAINMAN_HOOK_ENTRY=str(chainman.RUNTIME / "bootstrap/hook-task.sh"),
            CHAINMAN_RUNTIME_PYTHON=sys.executable,
            CHAINMAN_RUNTIME=str(chainman.RUNTIME),
            CHAINMAN_ROOT=str(root),
        )
        if arguments[0] == "run" and arguments[1] == "pre-push":
            # Git's ref stream is immutable and replayed independently to every
            # check. Lefthook and setup consent must not consume it.
            path = work / "pre-push-input"
            path.write_bytes(sys.stdin.buffer.read())
            env["CHAINMAN_HOOK_INPUT"] = str(path)
        command = (
            ["lefthook", "dump"]
            if arguments == ["config"]
            else ["lefthook", "validate"]
            if arguments == ["install"]
            else ["lefthook", "run", "--no-auto-install", *arguments[1:]]
        )
        result = chainman.execute(
            root, "hooks", command, env=env, stdin=subprocess.DEVNULL, check=False
        )
        if result.returncode:
            return result.returncode
        if arguments == ["install"]:
            install(root)
        return 0
