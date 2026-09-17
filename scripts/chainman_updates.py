"""Consumer hooks and runtime updates in one verified Git transaction."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import adapter_data as ad
import chainman
import git_runtime
import toolchain as tc
import updates
from transaction_state import Options

type FileState = tuple[bytes, int]


def managed_state(root: Path, name: str) -> FileState | None:
    path = tc.contained(root, name)
    if not path.exists():
        return None
    return tc.regular_input(root, name), stat.S_IMODE(path.stat().st_mode)


def managed_matches(first: FileState | None, second: FileState | None) -> bool:
    """Compare copies by bytes and Git execution identity, independent of umask.

    Non-permission mode flags must still match. Transaction snapshots continue to
    use managed_state directly so each file's complete original mode is retained.
    """
    if first is None or second is None:
        return False
    return (
        first[0] == second[0]
        and bool(first[1] & stat.S_IXUSR) == bool(second[1] & stat.S_IXUSR)
        and first[1] & ~0o777 == second[1] & ~0o777
    )


def managed_paths(root: Path) -> dict[str, str]:
    """Map declared identical runtime copies to their root-owned source files."""
    names = ["chainman.lock"]
    cfg = (
        tc.config(root).get("runtime", {})
        if (root / "chainman.toml").exists() or (root / "toolchain.toml").exists()
        else {}
    )
    if not isinstance(cfg, dict) or set(cfg) - {"copies"}:
        raise ValueError("Runtime declarations support only copies")
    copies = cfg.get("copies", [])
    if not isinstance(copies, list) or any(
        not isinstance(item, str) for item in copies
    ):
        raise ValueError("Runtime copies must be relative project directories")
    result = {name: name for name in names}
    directories = set()
    for value in copies:
        path = tc.contained(root, value)
        if path == root or not path.is_dir() or ".git" in Path(value).parts:
            raise ValueError(
                "Runtime copies require ordinary contained project directories"
            )
        relative = path.relative_to(root).as_posix()
        if relative in directories:
            raise ValueError("Runtime copy directories must be distinct")
        directories.add(relative)
        for source in names:
            target = f"{relative}/{source}"
            tc.contained(root, target)
            if target in result or any(
                target.startswith(existing + "/") or existing.startswith(target + "/")
                for existing in result
            ):
                raise ValueError("Runtime copies overlap managed files")
            result[target] = source
    return result


class ManagedFiles:
    """Restore only our still-identical managed outputs after a failed candidate."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.written: dict[str, tuple[FileState | None, FileState]] = {}

    def publish(self, name: str, before: FileState | None, after: FileState) -> None:
        if managed_state(self.root, name) != before:
            raise ValueError(
                f"Managed runtime input changed during preparation: {name}"
            )
        # Record before attempting the replacement: a failure can occur after
        # the atomic replacement, and recovery must still recognize its bytes.
        self.written[name] = (before, after)
        tc.atomic_bytes(tc.contained(self.root, name), after[0], after[1])

    def restore(self) -> None:
        preserved = []
        for name, (before, candidate) in reversed(list(self.written.items())):
            try:
                current = managed_state(self.root, name)
                if current != candidate:
                    if current != before:
                        preserved.append(name)
                    continue
                path = tc.contained(self.root, name)
                if before is None:
                    path.unlink()
                else:
                    tc.atomic_bytes(path, before[0], before[1])
            except (OSError, ValueError):
                preserved.append(name)
        self.written.clear()
        if preserved:
            print(
                "Chainman: concurrent or inaccessible managed changes were preserved: "
                + ", ".join(preserved),
                file=sys.stderr,
            )


def fetch_source(root: Path, *, gc_root: Path) -> Path:
    return git_runtime.store(
        git_runtime.pin(tc.regular_input(root, "chainman.lock")), gc_root=gc_root
    )


def validate_runtime(runtime: Path) -> None:
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("Candidate runtime must be a real directory")

    def unreadable(error: OSError) -> None:
        raise ValueError("Candidate runtime tree could not be inspected") from error

    for directory, folders, files in os.walk(
        runtime, followlinks=False, onerror=unreadable
    ):
        for name in folders + files:
            kind = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISREG(kind) or stat.S_ISDIR(kind)):
                raise ValueError(
                    "Candidate runtime must contain only regular files and directories"
                )
    for name in (
        "VERSION",
        "bootstrap/git-entry.sh",
        "bootstrap/chainman.sh",
        "bootstrap/fetch.nix",
        "nix/flake.nix",
        "nix/flake.lock",
        "scripts/chainman.py",
        "scripts/chainman_updates.py",
    ):
        path = runtime / name
        if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(
                f"Candidate runtime is missing a required regular file: {name}"
            )
    actual = (runtime / "VERSION").read_text().strip()
    if not actual or len(actual.splitlines()) != 1:
        raise ValueError(
            "Candidate VERSION must contain descriptive single-line metadata"
        )


def runtime_candidate(
    root: Path,
    policy: Mapping[str, object],
    now: datetime,
    managed: ManagedFiles | None = None,
    *,
    gc_root: Path,
    revision: str | None = None,
) -> Path:
    lock_before = managed_state(root, "chainman.lock")
    if lock_before is None:
        raise ValueError("Runtime pin disappeared during preparation")
    old = git_runtime.pin(lock_before[0])
    copies = {}
    for target in managed_paths(root):
        before = managed_state(root, target)
        if not managed_matches(before, lock_before):
            raise ValueError(
                f"Managed runtime copy was locally modified; reconcile it explicitly: {target}"
            )
        copies[target] = before
    revision = git_runtime.default_revision() if revision is None else revision
    git_runtime.pin((revision + "\n").encode())
    if revision == old:
        return chainman.RUNTIME
    runtime = git_runtime.store(revision, gc_root=gc_root)
    validate_runtime(runtime)
    after = ((revision + "\n").encode(), 0o644)
    publication = managed if managed is not None else ManagedFiles(root)
    try:
        for target, before in copies.items():
            publication.publish(target, before, after)
    except BaseException:
        publication.restore()
        raise
    return runtime


def perform(
    root: Path,
    policy: Mapping[str, object],
    now: datetime,
    extra: list[str],
    *,
    only_runtime: bool = False,
    skip_runtime: bool = True,
    managed: ManagedFiles | None = None,
) -> Path:
    with tc.nix_temporary_directory("chainman-runtime-update-") as directory:
        runtime = (
            chainman.RUNTIME
            if skip_runtime and not only_runtime
            else runtime_candidate(
                root, policy, now, managed, gc_root=Path(directory) / "runtime"
            )
        )
        if only_runtime:
            return runtime
        if runtime != chainman.RUNTIME:
            tc.managed_run(
                [
                    tc.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "develop",
                    f"path:{quote(str(runtime / 'nix'), safe='/')}#updates",
                    "--no-write-lock-file",
                    "--command",
                    "python3",
                    str(runtime / "scripts/chainman_updates.py"),
                    "--resolve-root",
                    str(root),
                    now.isoformat(),
                    json.dumps(extra),
                ],
                cwd=root,
                env=dict(
                    os.environ,
                    CHAINMAN_ROOT=str(root),
                    CHAINMAN_PROJECT_ROOT=str(root),
                    TOOLCHAIN_FRESH="1",
                    CHAINMAN_UPDATE_ACTIVE="1",
                    CHAINMAN_SETUP="auto",
                ),
                check=True,
            )
        else:
            resolve_current(root, policy, now, extra)
        return runtime


def resolve_current(
    root: Path, policy: Mapping[str, object], now: datetime, extra: list[str]
) -> None:
    import dependency_api

    policy = dependency_api.policy(root)
    env = tc.environment(root)
    if policy.get("adapters") or policy.get("steps"):
        dependency_api.plan_steps(root, policy, extra)
    env.update(
        TOOLCHAIN_FRESH="1",
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(chainman.RUNTIME),
        CHAINMAN_UPDATE_ACTIVE="1",
        CHAINMAN_SETUP="auto",
        CHAINMAN_UPDATE_AT=now.isoformat(),
    )
    if policy.get("steps"):
        if policy.get("resolver"):
            raise ValueError("Use ordered steps or a legacy resolver, not both")
        dependency_api.run_steps(root, policy, now, extra)
    elif policy.get("resolver"):
        if policy.get("eligibility") != "resolver":
            raise ValueError(
                "Custom resolvers must explicitly own eligibility with updates.eligibility='resolver'"
            )
        env["CHAINMAN_MINIMUM_RELEASE_AGE_DAYS"] = str(
            policy.get("minimum_age_days", 30)
        )
        # Hooks perform selection/generation only. The surrounding transaction owns Git.
        chainman.run_hook(
            root,
            policy["resolver"],
            name=ad.text(
                policy.get(
                    "profile",
                    ad.table(tc.config(root).get("project", {}), "Project").get(
                        "default_profile", "default"
                    ),
                ),
                "Resolver profile",
            ),
            extra=extra,
            env=env,
        )
    else:
        if extra:
            selection = dependency_api.selection_arguments(extra)
            if (
                selection.targets != "all"
                or selection.policy
                or selection.target_policy
            ):
                raise ValueError(
                    "Built-in module updates do not accept target selection or policy overrides"
                )
        selected = ad.strings(tc.config(root)["modules"], "Modules")
        updates.perform(root, now, selected)


def verify(root: Path, policy: Mapping[str, object], runtime: Path) -> None:
    # Re-enter the candidate runtime even when only the runtime pin changed.
    env = dict(
        os.environ,
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(runtime),
        TOOLCHAIN_FRESH="1",
        CHAINMAN_UPDATE_ACTIVE="1",
        CHAINMAN_SETUP="auto",
    )
    tc.managed_run(
        [
            tc.nix_command(),
            "--extra-experimental-features",
            "nix-command flakes",
            "develop",
            f"path:{quote(str(runtime / 'nix'), safe='/')}#updates",
            "--no-write-lock-file",
            "--command",
            "python3",
            str(runtime / "scripts/chainman_updates.py"),
            "--verify-root",
            str(root),
        ],
        cwd=root,
        env=env,
        check=True,
    )


def verify_current(root: Path) -> None:
    import dependency_api

    policy = dependency_api.policy(root)
    env = tc.environment(root)
    env.update(TOOLCHAIN_FRESH="1", CHAINMAN_UPDATE_ACTIVE="1", CHAINMAN_SETUP="auto")
    if policy.get("verify"):
        chainman.run_hook(
            root,
            policy["verify"],
            name=ad.text(
                policy.get(
                    "profile",
                    ad.table(tc.config(root).get("project", {}), "Project").get(
                        "default_profile", "default"
                    ),
                ),
                "Verification profile",
            ),
            env=env,
        )
    elif ad.table(tc.config(root).get("commands", {}), "Commands").get("verify"):
        chainman.run_project(root, "verify", [])
    else:
        updates.verify(root, ad.strings(tc.config(root)["modules"], "Modules"))


def options(args: list[str]) -> Options:
    import recipes

    args = recipes.options(args)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write one schema-1 JSON result; send command output to stderr",
    )
    parser.add_argument("--message", default="chore: update dependencies")
    runtime_flags = parser.add_mutually_exclusive_group()
    runtime_flags.add_argument(
        "--only-chainman", action="store_true", help=argparse.SUPPRESS
    )
    runtime_flags.add_argument(
        "--skip-chainman", action="store_true", help="Retain the Chainman runtime pin"
    )
    runtime_flags.add_argument(
        "--include-chainman", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    opts = parser.parse_args(args)
    if opts.staged:
        raise ValueError(
            "Use just format-staged (or just chainman format-staged); update transactions cannot format the index"
        )
    if opts.format:
        if opts.only_chainman or opts.include_chainman or opts.extra:
            raise ValueError("Format does not select dependency targets")
        if opts.message == "chore: update dependencies":
            opts.message = "chore: format"
    if opts.extra[:1] == ["--"]:
        opts.extra = opts.extra[1:]
    if not opts.message.strip() or "\0" in opts.message:
        raise ValueError("Commit message must be nonempty text without NUL")
    if opts.only_chainman and opts.extra:
        raise ValueError("Runtime updates do not accept dependency resolver arguments")
    import dependency_api

    if opts.only_chainman:
        opts.runtime = "only"
    elif opts.skip_chainman or opts.format:
        opts.runtime = "exclude"
    elif opts.include_chainman:
        opts.runtime = "include"
    else:
        selection = dependency_api.selection_arguments(opts.extra, allow_extra=True)
        opts.runtime = "include" if "all" in selection.targets.split(",") else "exclude"
    if os.environ.get("CHAINMAN_UPDATE_ACTIVE"):
        raise ValueError("An update hook must not recursively start another update")
    del opts.only_chainman
    del opts.skip_chainman
    del opts.include_chainman
    return Options.decode(vars(opts))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-root":
        verify_current(Path(sys.argv[2]).resolve())
    elif len(sys.argv) == 5 and sys.argv[1] == "--resolve-root":
        root = Path(sys.argv[2]).resolve()
        resolve_current(
            root,
            ad.table(tc.config(root)["updates"], "Updates"),
            datetime.fromisoformat(sys.argv[3]),
            json.loads(sys.argv[4]),
        )
    else:
        raise SystemExit("Use the Chainman launcher")
