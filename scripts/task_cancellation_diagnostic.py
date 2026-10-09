"""Repeat the unchanged stopped-task assertion in the owning control profile."""

import os
from pathlib import Path
import platform
import subprocess
import sys
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
        print(f"Cancellation diagnostic: building control-{target}", flush=True)
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
        print("Cancellation diagnostic: control package ready", flush=True)
        environment = dict(
            os.environ,
            CHAINMAN_TEST_CONTROL=package + "/bin/chainman-control",
            CHAINMAN_TEST_PROCESS_COMPOSE=package + "/bin/process-compose",
            CHAINMAN_TEST_WATCHEXEC=package + "/bin/watchexec",
        )
        for iteration in range(1, 65):
            traced = (
                iteration <= 32
                and environment.get("CHAINMAN_TEST_TASK_SIGNAL_TRACE") == "1"
            )
            case_environment = dict(
                environment, CHAINMAN_TEST_TASK_SIGNAL_TRACE="1" if traced else "0"
            )
            print(
                f"Stopped-task cancellation diagnostic iteration {iteration}/64 "
                f"(signal trace: {traced})",
                flush=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    "import faulthandler,runpy; "
                    "faulthandler.dump_traceback_later(30,repeat=True); "
                    "runpy.run_module('unittest',run_name='__main__')",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_services_control.py",
                    "-k",
                    "test_stopped_task_owner_handles_cancellation",
                    "-v",
                ],
                cwd=ROOT,
                env=case_environment,
                check=True,
            )


if __name__ == "__main__":
    main()
