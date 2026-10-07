"""Repeat the unchanged stopped-task assertion in the owning control profile."""

import os
from pathlib import Path
import platform
import subprocess
import tempfile

import toolchain as tc

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = (
        f"{platform.system().lower()}-"
        + {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    )
    with tempfile.TemporaryDirectory(
        prefix="chainman-cancellation-diagnostic-"
    ) as directory:
        package = subprocess.check_output(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                tc.nix_path_reference(ROOT / "nix", f"control-{target}"),
                "--out-link",
                str(Path(directory) / "runtime"),
                "--print-out-paths",
                "--no-write-lock-file",
            ],
            text=True,
        ).strip()
        environment = dict(
            os.environ,
            CHAINMAN_TEST_CONTROL=package + "/bin/chainman-control",
            CHAINMAN_TEST_PROCESS_COMPOSE=package + "/bin/process-compose",
            CHAINMAN_TEST_WATCHEXEC=package + "/bin/watchexec",
        )
        for iteration in range(1, 13):
            print(
                f"Stopped-task cancellation diagnostic iteration {iteration}/12",
                flush=True,
            )
            subprocess.run(
                [
                    "python3",
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_services_control.py",
                    "-k",
                    "test_stopped_task_owner_handles_cancellation_without_kill_timeout",
                    "-v",
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )


if __name__ == "__main__":
    main()
