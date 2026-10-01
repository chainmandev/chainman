"""Source development uses the same isolated acceptance and Git transaction.

The source repository builds the runtime itself, so its host-Nix entry replaces
the installed consumer's lock/bootstrap entry; candidate ownership is shared.
"""

import os
from pathlib import Path
import sys

import chainman_updates
import dependency_api
import toolchain as tc
import update_staging as staging
import updates
import update_cache
from adapter_data import strings


def format_source(root: Path, *, check: bool = False, staged: bool = False) -> None:
    commands = []
    if check or not staged:
        commands.append(
            ["python3", "scripts/generate.py", *(["--check"] if check else [])]
        )
    commands.append(["python3", "scripts/format.py", *(["--check"] if check else [])])
    for argv in commands:
        tc.managed_run(
            [*tc.entry_command(root, "core"), *argv],
            cwd=root,
            env=dict(
                tc.environment(root), CHAINMAN_UPDATE_ACTIVE="1", CHAINMAN_SETUP="auto"
            ),
            check=True,
        )


def run(root: Path, action: str, arguments: list[str]) -> None:
    if action not in {"format", "deps-update"}:
        raise ValueError("Expected format or deps-update")
    if os.environ.get("CHAINMAN_UPDATE_ACTIVE"):
        raise ValueError("An update hook must not recursively start an update")
    if action == "format" and arguments == ["commit=off"]:
        format_source(root)
        return
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "chainman/updates"
    )
    resumed = len(arguments) == 1 and arguments[0].startswith("resume=")
    legacy = resumed and Path(arguments[0].split("=", 1)[1]).parent == cache
    if not os.environ.get("CHAINMAN_UPDATE_TRANSACTION") and not legacy:
        # Validate before exporting tooling or allocating a disposable workspace.
        if not resumed:
            chainman_updates.options(
                (["--format"] if action == "format" else []) + arguments
            )
        update_cache.run(root, action, arguments)
        return
    if resumed:
        destination = staging.directory(Path(arguments[0].split("=", 1)[1]))
        if destination.parent not in {
            cache.resolve(),
            cache.resolve() / "v1",
        } or not destination.name.startswith("candidate."):
            raise ValueError("Resume must select a retained update transaction")
    else:
        # Parse before allocating a transaction, including --help.
        arguments = (["--format"] if action == "format" else []) + arguments
        opts = chainman_updates.options(arguments)
        if opts.extra:
            selected = dependency_api.selection_arguments(opts.extra)
            if selected.targets != "all" or selected.policy or selected.target_policy:
                raise ValueError(
                    "Source updates accept only targets=all without adapter policies; "
                    "target selection is supported by consumer adapters"
                )
        if opts.only_chainman:
            raise ValueError(
                "The Chainman source repository does not pin its own runtime"
            )
        destination = staging.directory(Path(os.environ["CHAINMAN_UPDATE_TRANSACTION"]))
        for name in ("candidate", "control"):
            (destination / name).mkdir()
    try:
        if resumed:
            state, _ = staging.read_state(root, destination)
            if not state.source:
                raise ValueError("This transaction belongs to an installed consumer")
            staging.resume(root, destination)
            arguments = (
                (destination / "control/resume-arguments").read_text().splitlines()
            )
        else:
            staging.prepare(root, destination, arguments, source=True)
        state, candidate = staging.read_state(root, destination)
        opts = chainman_updates.options(arguments)
        with updates.preview_git_environment(), updates.operation(candidate):
            if opts.format:
                format_source(candidate, staged=opts.staged)
            elif resumed:
                staging.reaudit(candidate, state.at.isoformat(), arguments)
            else:
                updates.perform(
                    candidate,
                    state.at,
                    strings(tc.config(candidate)["modules"], "Modules"),
                )
        staging.inspect(root, destination)
        state, candidate = staging.read_state(root, destination)
        if state.require_inspection().paths:
            with updates.preview_git_environment(), updates.operation(candidate):
                if opts.format:
                    format_source(candidate, check=True, staged=opts.staged)
                else:
                    updates.verify(
                        candidate, strings(tc.config(candidate)["modules"], "Modules")
                    )
        staging.finalize(root, destination)
    except BaseException:
        if legacy:
            print(
                f"Chainman: legacy temporary candidate at {destination}/candidate; not a backup; resume with: just {action} resume={destination}",
                file=sys.stderr,
            )
        raise
    else:
        if legacy:
            import shutil

            shutil.rmtree(destination)


if __name__ == "__main__":
    try:
        run(tc.ROOT, sys.argv[1], sys.argv[2:])
    except (OSError, ValueError) as error:
        print(f"Chainman source workflow: {error}", file=sys.stderr)
        raise SystemExit(1) from None
