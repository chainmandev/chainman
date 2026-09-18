"""Worktree-owned Git bridges and a pinned, composable lefthook preset."""

from collections.abc import Mapping
import contextlib
from collections.abc import Iterator
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import stat
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
        "installed": bool(selected)
        and (root / selected).resolve() == target
        and intact,
        "path": str(target),
        "selected_path": selected,
    }


def check_installation(root: Path) -> None:
    if not repository(root):
        return
    target = directory(root)
    selected = current_path(root)
    recorded = None
    record = target / "ownership.json"
    if record.exists():
        original = staged_format.identity(record)
        assert original is not None
        recorded = table(json.loads(original[0]), "Hook ownership").get("setting")
    relocated = selected == recorded and all(
        staged_format.identity(target / event) is not None
        and (target / event).read_bytes() == bridge(event)
        for event in EVENTS
    )
    if selected and (root / selected).resolve() != target and not relocated:
        raise ValueError(
            f"Git hooks are managed at {selected!r}. Review and remove that core.hooksPath setting before `just hooks install`; existing hooks were preserved. For a legacy relocated chainman installation, inspect `git config --show-origin --get core.hooksPath`, then remove only its owned worktree setting with `git config --worktree --unset core.hooksPath`. For disposable CI use `just setup --no-hooks`."
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


def check_container_repair(root: Path) -> None:
    """Admit only an intact, recorded relocation past container visibility checks."""
    target = directory(root)
    selected = current_path(root)
    if not selected or (root / selected).resolve() == target:
        raise ValueError("Unavailable Git hooks require host-nix or an explicit mount")
    # A different selected path is accepted here only with the ownership record
    # and intact bridges. No project command has run before this decision.
    check_installation(root)


@contextlib.contextmanager
def administration(root: Path) -> Iterator[None]:
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    path = common / "chainman-hooks.lock"
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Hook administration must not contain symlinks")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError("Hook administration lock must be a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Git hook installation or removal is active; retry when it finishes"
            ) from None
        yield


def install(root: Path) -> None:
    if not repository(root):
        print("Git hooks: not applicable outside a Git project root")
        return
    with administration(root):
        install_locked(root)


def install_locked(root: Path) -> None:
    check_installation(root)
    if not repository(root):
        print("Git hooks: not applicable outside a Git project root")
        return
    target = directory(root)
    target.mkdir(mode=0o700, exist_ok=True)
    common = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    shared = common / "config"
    primary = common / "config.worktree"
    selected = target.parent / "config.worktree"
    siblings = sorted((common / "worktrees").glob("*/config.worktree"))
    configs = list(dict.fromkeys([primary, selected, *siblings, shared]))
    # Honor Git's configuration locks across siblings. Prepare complete copies
    # first: enabling the extension is the LAST write, so common core.bare and
    # core.worktree never temporarily change the meaning of a linked checkout.
    with contextlib.ExitStack() as cleanup:
        for path in sorted(configs):
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("Git hook configuration must not contain symlinks")
            lock = path.with_name(path.name + ".lock")
            try:
                descriptor = os.open(
                    lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                )
            except FileExistsError:
                raise ValueError(
                    f"Git configuration is in use: {lock}; retry hook installation"
                ) from None
            os.close(descriptor)
            cleanup.callback(lock.unlink)
        check_installation(root)
        originals = {path: staged_format.identity(path) for path in configs}
        temporary = Path(cleanup.enter_context(tempfile.TemporaryDirectory(dir=target)))
        copies = {}
        for number, path in enumerate(configs):
            copy = temporary / str(number)
            original = originals[path]
            copy.write_bytes(original[0] if original is not None else b"")
            copies[path] = copy
        enabled = git(
            root,
            "config",
            "--local",
            "--includes",
            "--type=bool",
            "--get",
            "extensions.worktreeConfig",
            check=False,
        )
        if enabled != "true":
            for path in configs:
                if path == shared or originals[path] is None:
                    continue
                dormant = staged_format.git(
                    root,
                    "config",
                    "--file",
                    str(path),
                    "--includes",
                    "--get-regexp",
                    r"^(core\.(bare|worktree|hookspath)|include.*\.path)$",
                    check=False,
                )
                if dormant.returncode != 1:
                    raise ValueError(
                        f"Dormant worktree configuration may change Git identity or hooks: {path}; review it before enabling worktree configuration"
                    )
            for key in ("core.bare", "core.worktree"):
                kind = ["--type=bool"] if key == "core.bare" else []
                value = staged_format.git(
                    root,
                    "config",
                    "--file",
                    str(copies[shared]),
                    *kind,
                    "--get",
                    key,
                    check=False,
                )
                effective = staged_format.git(
                    root,
                    "config",
                    "--local",
                    "--includes",
                    *kind,
                    "--get",
                    key,
                    check=False,
                )
                origins = staged_format.git(
                    root,
                    "config",
                    "--local",
                    "--includes",
                    "--show-origin",
                    "--null",
                    "--get-all",
                    key,
                    check=False,
                )
                if value.returncode not in (0, 1) or effective.returncode not in (0, 1):
                    raise ValueError(f"Cannot read shared Git setting {key}")
                sources = (
                    origins.stdout.removesuffix(b"\0").split(b"\0")[::2]
                    if origins.stdout
                    else []
                )
                if value.stdout != effective.stdout or any(
                    not source.startswith(b"file:")
                    or (root / os.fsdecode(source[5:])).resolve() != shared.resolve()
                    for source in sources
                ):
                    raise ValueError(
                        f"Shared {key} comes from included configuration; configure Git worktree settings explicitly before installing hooks"
                    )
                if value.returncode == 0:
                    git(
                        root,
                        "config",
                        "--file",
                        str(copies[primary]),
                        "--replace-all",
                        key,
                        os.fsdecode(value.stdout.removesuffix(b"\n")),
                    )
                    git(
                        root,
                        "config",
                        "--file",
                        str(copies[shared]),
                        "--unset-all",
                        key,
                    )
            git(
                root,
                "config",
                "--file",
                str(copies[shared]),
                "--replace-all",
                "extensions.worktreeConfig",
                "true",
            )
        setting = (
            os.path.relpath(target, root)
            if target.parent == root / ".git"
            else str(target)
        )
        git(
            root,
            "config",
            "--file",
            str(copies[selected]),
            "--replace-all",
            "core.hooksPath",
            setting,
        )
        replacements = {target / event: bridge(event) for event in EVENTS}
        ownership = target / "ownership.json"
        replacements[ownership] = (json.dumps({"setting": setting}) + "\n").encode()
        replacements.update({path: copies[path].read_bytes() for path in configs})
        originals.update(
            {target / event: staged_format.identity(target / event) for event in EVENTS}
        )
        originals[ownership] = staged_format.identity(ownership)
        published = []
        try:
            for path, body in replacements.items():
                original = originals[path]
                if original is None and not body:
                    continue
                mode = (
                    (original[1] if original is not None else 0o600)
                    if path in copies or path == ownership
                    else 0o755
                )
                if original == (body, mode):
                    continue
                published.append(path)
                tc.atomic_bytes(path, body, mode)
            if not status(root).get("installed"):
                raise ValueError("Git did not select the installed hook bridges")
        except BaseException:
            # Undo activation first, then restore the previously inactive files.
            # SIGKILL before activation also leaves the old config usable; all
            # activated configurations already have both complete hook bridges.
            for path in reversed(published):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    tc.atomic_bytes(path, original[0], original[1])
            raise
    print("Git hooks installed (format staged content; scan outgoing commits)")


def uninstall(root: Path) -> None:
    if not repository(root):
        return
    with administration(root):
        uninstall_locked(root)


def uninstall_locked(root: Path) -> None:
    target = directory(root)
    selected = current_path(root)
    if not selected or (root / selected).resolve() != target:
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
    config = target.parent / "config.worktree"
    lock = config.with_name(config.name + ".lock")
    descriptor = os.open(
        lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.close(descriptor)
    try:
        paths = [
            config,
            *(target / event for event in EVENTS),
            target / "ownership.json",
        ]
        originals = {path: staged_format.identity(path) for path in paths}
        original = originals[config]
        if original is None:
            raise ValueError("Owned worktree hook configuration is missing")
        with tempfile.TemporaryDirectory(dir=target) as temporary:
            copy = Path(temporary) / "config"
            copy.write_bytes(original[0])
            git(root, "config", "--file", str(copy), "--unset", "core.hooksPath")
            published = []
            try:
                published.append(config)
                tc.atomic_bytes(config, copy.read_bytes(), original[1])
                for path in paths[1:]:
                    if originals[path] is not None:
                        if staged_format.identity(path) != originals[path]:
                            raise ValueError(f"Hook changed during removal: {path}")
                        published.append(path)
                        path.unlink()
            except BaseException:
                for path in reversed(published):
                    saved = originals[path]
                    if saved is not None:
                        tc.atomic_bytes(path, saved[0], saved[1])
                raise
    finally:
        lock.unlink()
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
            if len(arguments) != 4:
                raise ValueError(
                    "pre-push requires the Git remote name and destination URL"
                )
            env["CHAINMAN_HOOK_REMOTE_NAME"] = arguments[2]
            env["CHAINMAN_HOOK_REMOTE_URL"] = arguments[3]
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
