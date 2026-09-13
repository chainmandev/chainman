"""Format or check the core tooling; optional modules own their language formatters."""

import argparse
from pathlib import Path
import subprocess
import yaml

from toolchain import contained


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    python = sorted(
        [
            p
            for directory in ("scripts", "tests", "examples/core", "template/scripts")
            for p in (root / directory).rglob("*.py")
        ]
        + list((root / "typings").glob("*.pyi"))
    )
    shell = sorted(
        [*(root / "scripts").glob("*.sh"), *(root / "bootstrap").glob("*.sh")]
    )
    nix = sorted([*(root / "nix").glob("*.nix"), *(root / "bootstrap").glob("*.nix")])
    for path in python + shell + nix:
        contained(root, path.relative_to(root).as_posix())
    commands = [
        [
            "ruff",
            "--isolated",
            "format",
            "--no-cache",
            *(["--check"] if args.check else []),
            *map(str, python),
        ],
        [
            "ruff",
            "--isolated",
            "check",
            "--no-cache",
            "--select",
            "E4,E7,E9,F,B,PLE",
            "--ignore",
            # Tests deliberately select local scripts before importing them.
            # B023 also flags synchronous loop-local callbacks. Their lifetime
            # is reviewed with the configuration, solver and native-path tests.
            "E402,B023",
            *map(str, python),
        ],
        [
            "shfmt",
            "-i",
            "4",
            "-bn",
            "-ci",
            "-sr",
            "-d" if args.check else "-w",
            *map(str, shell),
        ],
        ["shellcheck", *map(str, shell)],
        ["nixfmt", *(["--check"] if args.check else []), *map(str, nix)],
        [
            "just",
            "--unstable",
            "--fmt",
            *(["--check"] if args.check else []),
            "--justfile",
            str(root / "justfile"),
        ],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, check=True)
    for path in (root / ".github/workflows").glob("*.yml"):
        contained(root, path.relative_to(root).as_posix())
        yaml.safe_load(path.read_text())
    subprocess.run(["actionlint"], cwd=root, check=True)


if __name__ == "__main__":
    main()
