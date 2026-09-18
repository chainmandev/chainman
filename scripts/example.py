"""Generate the minimal starter using templates from this selected Git revision."""

import argparse
import json
from pathlib import Path
import shutil

import git_runtime

ROOT = Path(__file__).resolve().parents[1]


def create(destination: Path, revision: str) -> dict[str, str | int]:
    git_runtime.pin((revision + "\n").encode())
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("Starter destination must not contain symlinks")
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("Choose a new or empty project directory")
    shutil.copytree(ROOT / "template", destination, dirs_exist_ok=True)
    # Nix store permissions describe immutable inputs, not project-owned output.
    destination.chmod(0o755)
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o100 else 0o644)
    (destination / "chainman.lock").write_text(revision + "\n")
    (destination / ".gitattributes").write_text("chainman.lock text eol=lf\n")
    (destination / "justfile").write_bytes(
        (ROOT / "bootstrap/chainman.just").read_bytes()
        + b"\n"
        + (ROOT / "template/justfile").read_bytes()
    )
    shutil.copyfile(ROOT / "nix/flake.lock", destination / "flake.lock")
    (destination / ".gitignore").write_text(
        ".chainman/\n.cache/\n__pycache__/\nresult\n"
    )
    return {
        "directory": str(destination),
        "revision": revision,
        "version": (ROOT / "VERSION").read_text().strip(),
        "files": sum(path.is_file() for path in destination.rglob("*")),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    print(json.dumps(create(args.destination.absolute(), args.revision), indent=2))
