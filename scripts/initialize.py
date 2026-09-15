"""Resolve a rolling or explicit Git identity, then run that revision's starter generator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import subprocess

from adapter_data import table, text
import chainman_updates
import git_runtime
import toolchain as tc


def initialize(destination: Path, ref: str | None = None) -> dict[str, str | int]:
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("Initialization destination must not contain symlinks")
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("Choose a new or empty project directory")
    if ref is None:
        revision = git_runtime.default_revision()
    elif re.fullmatch(r"[0-9a-f]{40}", ref):
        revision = ref
    else:
        raise ValueError(
            "Omit SHA for the public default branch, or supply a full lowercase commit SHA"
        )
    with tc.nix_temporary_directory("chainman-initialize-") as temporary:
        runtime = git_runtime.store(revision, gc_root=Path(temporary) / "runtime")
        chainman_updates.validate_runtime(runtime)
        # Use the chosen revision's generator and templates, not this checkout's.
        result = tc.managed_run(
            [
                sys.executable,
                str(runtime / "scripts/example.py"),
                str(destination),
                revision,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    generated = table(json.loads(result.stdout), "Generated starter")
    files = generated.get("files")
    if not isinstance(files, int) or isinstance(files, bool) or files < 0:
        raise ValueError("Starter generator returned an invalid file count")
    return {
        "directory": text(generated.get("directory"), "Starter directory"),
        "revision": text(generated.get("revision"), "Starter revision"),
        "version": text(generated.get("version"), "Starter version"),
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("ref", nargs="?", metavar="SHA")
    args = parser.parse_args()
    try:
        result = initialize(args.destination.absolute(), args.ref)
    except subprocess.CalledProcessError as error:
        detail = error.stderr or str(error)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        parser.exit(1, f"Chainman initialization: {detail.strip()}\n")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Chainman initialization: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
