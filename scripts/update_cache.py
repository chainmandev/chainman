"""Source-side entry to the shared native temporary-transaction supervisor."""

import os
from pathlib import Path
import platform
import sys
import tempfile
from contextlib import ExitStack
import json

import hook_worker
import toolchain as tc


def run(root: Path, action: str, arguments: list[str]) -> None:
    import candidate_export
    import git_runtime
    import updates

    export_target, _ = candidate_export.split_arguments(arguments)
    target = (
        platform.system().lower()
        + "-"
        + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    )
    base = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "chainman/updates"
    )
    with ExitStack() as lifetime:
        output = lifetime.enter_context(
            tempfile.TemporaryDirectory(prefix="chainman-update-export-")
        )
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
            worker = [sys.executable, str(tc.RUNTIME / "scripts/source_workflow.py")]
            env = dict(os.environ, CHAINMAN_UPDATE_HELPER=str(helper))
            if export_target is not None:
                revision = updates.repository(root, clean=True)[1]
                rooted = lifetime.enter_context(
                    tc.nix_temporary_directory("chainman-source-export-")
                )
                runtime = git_runtime.store(
                    revision, gc_root=Path(rooted) / "runtime", repository=root
                )
                worker = [
                    sys.executable,
                    "-I",
                    "-B",
                    str(runtime / "scripts/isolated.py"),
                    "source_workflow.py",
                ]
                env.update(
                    CHAINMAN_ROOT=str(root),
                    CHAINMAN_SOURCE_EXPORT_RUNTIME=str(runtime),
                    CHAINMAN_SOURCE_EXPORT_REVISION=revision,
                )
                env.pop("CHAINMAN_ENTRY_AUTHORITY", None)
            argv = [
                str(helper),
                "update-cache",
                "run",
                str(base),
                resume,
                action,
                *worker,
                action,
                *arguments,
            ]
        if action in {"status", "prune"}:
            env = dict(os.environ, CHAINMAN_UPDATE_HELPER=str(helper))
        result = tc.managed_run(
            argv,
            cwd=root,
            env=env,
            stdout=sys.stderr if export_target is not None else None,
            check=False,
        )
        if (
            export_target is not None
            and (Path(export_target) / "result.json").is_file()
        ):
            print(
                json.dumps(candidate_export.check(Path(export_target)), sort_keys=True)
            )
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    run(tc.ROOT, sys.argv[1], sys.argv[2:])
