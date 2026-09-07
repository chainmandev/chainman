"""Create an independent consumer from a verified local Chainman release."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import tomlkit


def read_archive(body: bytes, version: str) -> dict:
    result = {}
    prefix = f"chainman-{version}/"
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for member in archive:
            if (
                not member.name.startswith(prefix)
                or not member.isfile()
                or member.mode not in {0o644, 0o755}
            ):
                raise ValueError(
                    "Release archive must contain only regular source files"
                )
            name = member.name[len(prefix) :]
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or str(path) != name
                or any(p in {"..", ".git"} for p in path.parts)
                or name in result
            ):
                raise ValueError("Unsafe or duplicate archive path")
            result[name] = (archive.extractfile(member).read(), member.mode)
    return result


def create(destination: Path, metadata_path: Path):
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("Example destination must not contain symlinks")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Choose a new or empty example directory")
    metadata = json.loads(metadata_path.read_text())
    if metadata.get("schema") != 1:
        raise ValueError("Unsupported release metadata")
    body = (
        metadata_path.parent / f"chainman-{metadata['version']}.tar.gz"
    ).read_bytes()
    if hashlib.sha256(body).hexdigest() != metadata["archive_sha256"]:
        raise ValueError("Release archive checksum mismatch")
    files = read_archive(body, metadata["version"])
    with tempfile.TemporaryDirectory(prefix="chainman-example-") as directory:
        tree = Path(directory)
        for name, (data, mode) in files.items():
            path = tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(mode)
        actual = subprocess.check_output(
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
        if actual != metadata["narHash"]:
            raise ValueError("Release unpacked NAR hash mismatch")
    selected = {
        name: item
        for name, item in files.items()
        if name.startswith(("examples/", "modules/", "nix/", "docs/"))
        or name in {".gitignore", "dependencies.toml", "sdk-versions.toml", "LICENSE"}
    }
    selected.update(
        {
            name[len("template/") :]: item
            for name, item in files.items()
            if name.startswith("template/")
        }
    )
    for name, (data, mode) in selected.items():
        if name.endswith(".md"):
            selected[name] = (
                data.replace(b"TOOLCHAIN_MODE", b"CHAINMAN_MODE")
                .replace(b"TOOLCHAIN_CONTAINER_ENGINE", b"CHAINMAN_CONTAINER_ENGINE")
                .replace(b"toolchain.toml", b"chainman.toml"),
                mode,
            )
    selected["scripts/chainman.sh"] = files["bootstrap/chainman.sh"]
    selected["scripts/chainman-fetch.nix"] = files["bootstrap/fetch.nix"]
    selected["vendor/chainman/chainman.tar.gz"] = body, 0o644
    # Container image updates belong to the managed bootstrap/runtime release.
    dependency_body, mode = selected["dependencies.toml"]
    dependencies = tomlkit.parse(dependency_body.decode())
    dependencies["docker"]["enabled"] = False
    dependencies["pins"] = [
        pin
        for pin in dependencies.get("pins", [])
        if not pin["file"].startswith("template/")
    ]
    selected["dependencies.toml"] = tomlkit.dumps(dependencies).encode(), mode
    lock = {
        key: metadata[key]
        for key in ("schema", "version", "revision", "url", "narHash")
    }
    lock["bundled_archive"] = "vendor/chainman/chainman.tar.gz"
    selected["chainman.lock"] = (json.dumps(lock, indent=2) + "\n").encode(), 0o644
    for name, (data, mode) in sorted(selected.items()):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    return {
        "directory": str(destination),
        "files": len(selected),
        "version": metadata["version"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--release",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "dist/release/chainman-release.json",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            create(args.destination.absolute(), args.release.resolve()), indent=2
        )
    )
