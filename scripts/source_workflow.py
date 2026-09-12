"""Source development uses the same isolated acceptance and Git transaction.

The source repository builds the runtime itself, so its host-Nix entry replaces
the installed consumer's lock/bootstrap entry; candidate ownership is shared.
"""

from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile

import chainman_updates
import toolchain as tc
import update_staging as staging
import updates


def format_source(root, *, check=False, staged=False):
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
            env=dict(tc.environment(root), CHAINMAN_UPDATE_ACTIVE="1"),
            check=True,
        )


def run(root, action, arguments):
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
    cache.mkdir(parents=True, exist_ok=True)
    resumed = len(arguments) == 1 and arguments[0].startswith("resume=")
    if resumed:
        destination = staging.directory(Path(arguments[0].split("=", 1)[1]))
        if destination.parent != cache.resolve() or not destination.name.startswith(
            "candidate."
        ):
            raise ValueError("Resume must select a retained update transaction")
    else:
        # Parse before allocating a transaction, including --help.
        arguments = (["--format"] if action == "format" else []) + arguments
        opts = chainman_updates.options(arguments)
        if opts.extra:
            raise ValueError(
                "Source module updates currently resolve their complete declared toolchain; target selection is supported by consumer adapters"
            )
        if opts.only_chainman:
            raise ValueError(
                "The Chainman source repository does not pin its own runtime"
            )
        destination = Path(tempfile.mkdtemp(prefix="candidate.", dir=cache)).resolve()
        for name in ("candidate", "control"):
            (destination / name).mkdir()
    try:
        if resumed:
            state, _ = staging.read_state(root, destination)
            if not state.get("source"):
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
                staging.reaudit(candidate, state["at"], arguments)
            else:
                updates.perform(
                    candidate,
                    datetime.fromisoformat(state["at"]),
                    tc.config(candidate)["modules"],
                )
        staging.inspect(root, destination)
        state, candidate = staging.read_state(root, destination)
        if state["paths"]:
            with updates.preview_git_environment(), updates.operation(candidate):
                if opts.format:
                    format_source(candidate, check=True, staged=opts.staged)
                else:
                    updates.verify(candidate, tc.config(candidate)["modules"])
        staging.finalize(root, destination)
    except BaseException:
        print(
            f"Chainman: candidate preserved at {destination}/candidate; resume with: just {action} resume={destination}",
            file=sys.stderr,
        )
        raise
    else:
        import shutil

        shutil.rmtree(destination)


if __name__ == "__main__":
    try:
        run(tc.ROOT, sys.argv[1], sys.argv[2:])
    except (OSError, ValueError) as error:
        print(f"Chainman source workflow: {error}", file=sys.stderr)
        raise SystemExit(1)
