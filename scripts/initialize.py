"""Resolve an explicit Git identity, then run that revision's starter generator."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import subprocess

from adapter_data import table, text
import chainman_updates
import git_runtime
import registry
import toolchain as tc


def initialize(destination: Path, ref: str) -> dict[str, str | int]:
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("Initialization destination must not contain symlinks")
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("Choose a new or empty project directory")
    selected = None
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        revision = ref
    elif re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", ref):
        tag = "v" + ref.removeprefix("v")
        releases = registry.github_releases("chainmandev/chainman")
        selected = next(
            (release for release in releases if release.version == tag), None
        )
        if selected is None:
            raise ValueError("Choose a published stable Chainman release")
        revision = chainman_updates.published_revision(
            selected, {"minimum_age_days": 0}, datetime.now(timezone.utc)
        )
    else:
        raise ValueError(
            "Choose a numeric version, vVERSION, or full lowercase commit SHA; moving selectors are not supported"
        )
    with tc.nix_temporary_directory("chainman-initialize-") as temporary:
        runtime = git_runtime.store(revision, gc_root=Path(temporary) / "runtime")
        version = (
            selected.version if selected else (runtime / "VERSION").read_text().strip()
        )
        chainman_updates.validate_runtime(runtime, version)
        if (
            selected
            and registry.github_commit(
                "chainmandev/chainman", selected.version, fresh=True
            )
            != revision
        ):
            raise ValueError("Release tag changed during initialization")
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
    parser.add_argument("ref")
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
