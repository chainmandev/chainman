"""Source-side entry to the shared native temporary-transaction supervisor."""

import os
from pathlib import Path
import platform
import sys
import tempfile

import hook_worker
import toolchain as tc


def run(root: Path, action: str, arguments: list[str]) -> None:
    target = (
        platform.system().lower()
        + "-"
        + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    )
    base = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "chainman/updates"
    )
    with tempfile.TemporaryDirectory(prefix="chainman-update-export-") as output:
        helper = Path(output) / "chainman-control"
        hook_worker.export_binary(Path(output), target, "task", "chainman-control")
        if action in {"status", "prune"}:
            argv = [str(helper), "update-cache", action, str(base), *arguments]
        else:
            resume = (
                arguments[0].split("=", 1)[1]
                if len(arguments) == 1 and arguments[0].startswith("resume=")
                else "-"
            )
            argv = [
                str(helper),
                "update-cache",
                "run",
                str(base),
                resume,
                action,
                sys.executable,
                str(tc.RUNTIME / "scripts/source_workflow.py"),
                action,
                *arguments,
            ]
        result = tc.managed_run(
            argv,
            cwd=root,
            env=dict(os.environ, CHAINMAN_UPDATE_HELPER=str(helper)),
            check=False,
        )
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    run(tc.ROOT, sys.argv[1], sys.argv[2:])
