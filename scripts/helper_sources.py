"""Reconcile source-controlled optional helper dependency hashes."""

from pathlib import Path

import toolchain as tc


def reconcile(root: Path, selected: list[str]) -> None:
    if "attest" not in selected:
        return
    # Refuse absent/indirect inputs before invoking a mutation in the candidate.
    for path in (
        "nix/attest/package.json",
        "nix/attest/pnpm-lock.yaml",
        "nix/attest.nix",
    ):
        tc.regular_input(root, path)
    tc.managed_run(
        [
            *tc.entry_command(root, "updates"),
            "sh",
            "-eu",
            "-c",
            'cd "$1"; shift; exec "$@"',
            "sh",
            str(tc.contained(root, "nix")),
            "nix-update",
            "--flake",
            "--version=skip",
            "--override-filename",
            "attest.nix",
            "attest",
        ],
        cwd=root,
        env=tc.environment(root),
        check=True,
    )
