"""Project-owned asset generation, deterministic build and format extension."""

import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "build", "format"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.action == "generate":
        values = json.loads((ROOT / "examples/core/labels.json").read_text())
        body = "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
        target = ROOT / "examples/core/labels.txt"
        if args.check:
            if target.read_text() != body:
                raise SystemExit("Generated labels are stale; run just format")
        else:
            target.write_text(body)
    elif args.action == "build":
        output = ROOT / "dist"
        if output.is_symlink():
            raise SystemExit("dist must be a real directory")
        output.mkdir(exist_ok=True)
        with tarfile.open(output / "demo.tar", "w") as archive:
            for name in ("greeting.py", "labels.txt"):
                data = (ROOT / "examples/core" / name).read_bytes()
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                archive.addfile(info, io.BytesIO(data))
    else:
        python = [
            *map(str, sorted((ROOT / "examples/core").glob("*.py"))),
            str(Path(__file__)),
        ]
        subprocess.run(
            [
                "ruff",
                "--isolated",
                "format",
                "--no-cache",
                *(["--check"] if args.check else []),
                *python,
            ],
            check=True,
        )
        subprocess.run(
            ["ruff", "--isolated", "check", "--no-cache", *python], check=True
        )


if __name__ == "__main__":
    main()
