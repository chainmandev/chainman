"""Qualify the native ownership adapter and the exact pinned backend locally."""

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
    source = ROOT / "nix/control"
    formatted = subprocess.check_output(
        ["gofmt", "-l", *map(str, source.glob("*.go"))], text=True
    )
    if formatted:
        raise ValueError("Go files require gofmt:\n" + formatted)
    subprocess.run(["go", "vet", "-mod=readonly", "./..."], cwd=source, check=True)
    subprocess.run(
        ["go", "test", "-race", "-mod=readonly", "./..."],
        cwd=source,
        env=dict(os.environ, CGO_ENABLED="1"),
        check=True,
    )
    with tempfile.TemporaryDirectory(prefix="chainman-control-test-") as directory:
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
        env = dict(
            os.environ,
            CHAINMAN_TEST_CONTROL=package + "/bin/chainman-control",
            CHAINMAN_TEST_PROCESS_COMPOSE=package + "/bin/process-compose",
            CHAINMAN_TEST_WATCHEXEC=package + "/bin/watchexec",
        )
        subprocess.run(
            [
                "python3",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_services_control.py",
                "-v",
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
    with tempfile.TemporaryDirectory(prefix="chainman-cross-build-") as tmp:
        for system in ("linux", "darwin"):
            for arch in ("arm64", "amd64"):
                subprocess.run(
                    [
                        "go",
                        "build",
                        "-mod=readonly",
                        "-trimpath",
                        "-buildvcs=false",
                        "-o",
                        str(Path(tmp) / f"{system}-{arch}"),
                        ".",
                    ],
                    cwd=source,
                    env=dict(os.environ, CGO_ENABLED="0", GOOS=system, GOARCH=arch),
                    check=True,
                )


if __name__ == "__main__":
    main()
