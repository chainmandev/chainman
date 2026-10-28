"""Transfer a qualified runner's runtime closure within one exact-source CI run.

The expected archive digest comes from the preparation job's output, never from
the downloaded artifact. This is a same-run transfer, not a reusable binary cache.
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "runtime.nar"
MAX_BYTES = 16 * 1024**3
NIX = ["nix", "--extra-experimental-features", "nix-command flakes"]


def output(root: Path, *command: str) -> str:
    return subprocess.check_output(command, cwd=root, text=True).strip()


def check_source(root: Path, expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ValueError("expected an exact candidate SHA")
    if output(root, "git", "rev-parse", "HEAD") != expected:
        raise ValueError("runtime closure candidate does not match this checkout")
    if output(root, "git", "status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("runtime closure requires a clean candidate checkout")


def runtime_path(root: Path, *, build: bool) -> str:
    system = output(
        root, *NIX, "eval", "--impure", "--raw", "--expr", "builtins.currentSystem"
    )
    if system not in {
        "x86_64-darwin",
        "aarch64-darwin",
        "x86_64-linux",
        "aarch64-linux",
    }:
        raise ValueError("unsupported runtime closure platform")
    target = "path:./nix#devShells." + system + ".trust"
    if build:
        path = output(
            root,
            *NIX,
            "build",
            "--no-update-lock-file",
            "--max-jobs",
            "1",
            "--no-link",
            "--print-out-paths",
            target,
        )
    else:
        path = output(
            root, *NIX, "eval", "--no-update-lock-file", "--raw", target + ".outPath"
        )
    if not re.fullmatch(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+", path):
        raise ValueError("expected one runtime store path")
    return path


def archive_digest(archive: Path) -> str:
    metadata = archive.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_BYTES:
        raise ValueError(
            "runtime archive must be a nonempty regular file within the size limit"
        )
    with archive.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export_closure(root: Path, directory: Path, expected: str) -> str:
    check_source(root, expected)
    runtime = runtime_path(root, build=True)
    paths = output(root, "nix-store", "--query", "--requisites", runtime).splitlines()
    if runtime not in paths or any(
        not re.fullmatch(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9+._?=-]+", path)
        for path in paths
    ):
        raise ValueError("invalid runtime closure inventory")
    directory.mkdir(parents=True, exist_ok=False)
    archive = directory / ARCHIVE
    with archive.open("xb") as stream:
        subprocess.run(
            ["nix-store", "--export", *paths], cwd=root, stdout=stream, check=True
        )
    digest = archive_digest(archive)
    check_source(root, expected)
    return digest


def import_closure(root: Path, directory: Path, expected: str, digest: str) -> None:
    check_source(root, expected)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("expected the preparation job's exact archive SHA-256")
    archive = directory / ARCHIVE
    if archive_digest(archive) != digest:
        raise ValueError("runtime archive SHA-256 does not match the preparation job")
    with archive.open("rb") as stream:
        subprocess.run(["nix-store", "--import"], cwd=root, stdin=stream, check=True)
    # Evaluate the receiving checkout independently; a different closure must
    # fail here instead of silently starting another cold build during tests.
    runtime = runtime_path(root, build=False)
    paths = output(root, "nix-store", "--query", "--requisites", runtime).splitlines()
    if runtime not in paths:
        raise ValueError("the imported archive does not contain this runtime")
    subprocess.run(["nix-store", "--verify-path", *paths], cwd=root, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["export", "import"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("candidate")
    parser.add_argument("digest", nargs="?")
    args = parser.parse_args()
    if args.operation == "export":
        if args.digest is not None:
            parser.error("export does not accept a digest")
        digest = export_closure(ROOT, args.directory, args.candidate)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
            stream.write("sha256=" + digest + "\n")
        print("Exported runtime closure with SHA-256 " + digest)
    else:
        if args.digest is None:
            parser.error("import requires the preparation job's digest")
        import_closure(ROOT, args.directory, args.candidate, args.digest)
        print("Verified and imported the candidate runtime closure")


if __name__ == "__main__":
    main()
