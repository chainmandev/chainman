"""Optional finite-command ownership inside the selected execution environment."""

from contextlib import contextmanager
from pathlib import Path
import platform

import chainman
import toolchain as tc


@contextmanager
def command(root, commands, spec):
    target = (
        platform.system().lower()
        + "-"
        + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    )
    with tc.nix_temporary_directory("chainman-task-") as directory:
        package = tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                f"path:{chainman.RUNTIME / 'nix'}#task-{target}",
                "--out-link",
                str(Path(directory) / "runtime"),
                "--print-out-paths",
                "--no-write-lock-file",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not package.startswith("/nix/store/") or "\n" in package:
            raise ValueError("Invalid native task store output")
        executable = Path(package) / "bin/chainman-control"
        path = Path(directory) / "commands.json"
        tc.atomic_json(
            path,
            {
                "commands": [
                    {
                        "argv": argv,
                        "directory": str(
                            tc.contained(root, spec.get("directory", "."))
                        ),
                    }
                    for argv in commands
                ],
                "timeout_seconds": spec.get("timeout_seconds", 0),
                "shutdown_seconds": spec.get("shutdown_seconds", 10),
            },
        )
        yield [str(executable), "command", str(path)]
