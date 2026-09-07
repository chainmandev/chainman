"""Build a deterministic release from an explicit inventory in a clean Git commit."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def archive_bytes(files: dict[str, tuple[bytes, int]], version: str) -> bytes:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("A release requires a stable numeric version")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, (body, mode) in sorted(files.items()):
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or any(p in {"..", ".git"} for p in path.parts)
                or str(path) != name
            ):
                raise ValueError("Release paths must be normalized and relative")
            if mode not in {0o644, 0o755}:
                raise ValueError("Release files must be regular source files")
            info = tarfile.TarInfo(f"chainman-{version}/{name}")
            info.size, info.mode, info.mtime = len(body), mode, 0
            archive.addfile(info, io.BytesIO(body))
    return gzip.compress(raw.getvalue(), mtime=0)


def git(root, *args) -> bytes:
    return subprocess.check_output(
        ["git", "--literal-pathspecs", "-C", str(root), *args]
    )


def release(root: Path, output: Path) -> dict:
    if (
        Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        != root.resolve()
    ):
        raise ValueError("Release source must be its own repository")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Commit the intended release source first")
    revision = git(root, "rev-parse", "HEAD").decode().strip()
    inventory = json.loads(git(root, "show", f"{revision}:release-files.json"))
    if inventory.get("schema") != 1 or len(inventory["files"]) != len(
        set(inventory["files"])
    ):
        raise ValueError("Malformed release inventory")
    files = {}
    for name in inventory["files"]:
        entry = git(root, "ls-tree", revision, "--", name).decode().strip()
        meta, sep, actual = entry.partition("\t")
        if not sep or actual != name or meta.split()[0] not in {"100644", "100755"}:
            raise ValueError(f"Release input is missing or not regular: {name}")
        files[name] = (
            git(root, "show", f"{revision}:{name}"),
            int(meta.split()[0][-3:], 8),
        )
    version = files["VERSION"][0].decode().strip()
    body = archive_bytes(files, version)
    with tempfile.TemporaryDirectory(prefix="chainman-release-") as directory:
        tree = Path(directory)
        for name, (data, mode) in files.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        nar = subprocess.check_output(
            [
                "nix",
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "path",
                str(tree),
            ],
            text=True,
        ).strip()
    filename = f"chainman-{version}.tar.gz"
    metadata = {
        "schema": 1,
        "version": version,
        "revision": revision,
        "url": f"https://github.com/chainmandev/chainman/releases/download/v{version}/{filename}",
        "narHash": nar,
        "archive_sha256": hashlib.sha256(body).hexdigest(),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / filename).write_bytes(body)
    (output / "chainman-release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    names = (filename, "chainman-release.json")
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        )
    )
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/release")
    args = parser.parse_args()
    print(json.dumps(release(ROOT, args.output.resolve()), indent=2))
