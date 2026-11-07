"""Real immutable source and held bootstrap closure for service-entry fixtures."""

from pathlib import Path
import shutil
import subprocess
import tempfile

import toolchain as tc

SOURCE = Path(__file__).resolve().parents[1]
NIX = shutil.which("nix")


def immutable_runtime(add_cleanup):
    if not NIX:
        raise RuntimeError("The managed fixture requires Nix")
    temporary = tempfile.TemporaryDirectory(prefix="chainman internal runtime ")
    add_cleanup(temporary.cleanup)
    base = Path(temporary.name).resolve()
    exported = base / "source"
    inventory = subprocess.check_output(
        [
            "git",
            "-C",
            str(SOURCE),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        text=True,
    ).splitlines()
    for name in inventory:
        if (SOURCE / name).is_file():
            target = exported / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, target)
    runtime = Path(
        subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "store",
                "add-path",
                "--name",
                "chainman-source",
                str(exported),
            ],
            text=True,
            timeout=60,
        ).strip()
    )
    subprocess.run(
        [
            str(Path(NIX).with_name("nix-store")),
            "--realise",
            str(runtime),
            "--add-root",
            str(base / "runtime-root"),
            "--indirect",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=30,
    )
    subprocess.run(
        [
            NIX,
            "--extra-experimental-features",
            "nix-command flakes",
            "print-dev-env",
            f"path:{runtime}/nix#bootstrap",
            "--no-write-lock-file",
            "--profile",
            str(base / "bootstrap"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        timeout=120,
    )
    bootstrap_python = str(tc.bootstrap_interpreter(base / "bootstrap"))
    return base, runtime, bootstrap_python
