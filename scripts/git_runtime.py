"""Exact Git identities and verified source trees; no release asset protocol."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import toolchain as tc

REPOSITORY = "https://github.com/chainmandev/chainman.git"


def pin(body: bytes) -> str:
    if re.fullmatch(rb"[0-9a-f]{40}\n", body) is None:
        raise ValueError(
            "chainman.lock requires one full lowercase Git SHA and newline"
        )
    return body[:-1].decode("ascii")


def git(*args: str, input: bytes | None = None) -> bytes:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_COUNT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return tc.managed_run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        input=input,
        env=env,
        capture_output=True,
        check=True,
    ).stdout


def objects(revision: str) -> Path:
    pin((revision + "\n").encode())
    home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache = home / "chainman/git/github.com-chainmandev-chainman" / (revision + ".git")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        temporary = Path(tempfile.mkdtemp(prefix=cache.name + ".", dir=cache.parent))
        try:
            git("init", "--bare", "--quiet", "--template=", str(temporary))
            cache.symlink_to(temporary, target_is_directory=True)
        except FileExistsError:
            shutil.rmtree(temporary)
        except BaseException:
            shutil.rmtree(temporary)
            raise
    selected = "--git-dir=" + str(cache)
    try:
        git(selected, "cat-file", "-e", revision)
    except subprocess.CalledProcessError:
        git(
            selected,
            "-c",
            "gc.auto=0",
            "fetch",
            "--no-auto-maintenance",
            "--no-write-fetch-head",
            REPOSITORY,
            revision,
        )
    git(selected, "fsck", "--full", "--strict", "--no-reflogs", "--no-dangling")
    if git(selected, "cat-file", "-t", revision).strip() != b"commit":
        raise ValueError("Runtime pin must identify a commit object")
    return cache


def materialize(revision: str, destination: Path) -> None:
    """Export blobs directly: attributes, filters and cached checkouts have no role."""
    cache = objects(revision)
    selected = "--git-dir=" + str(cache)
    tree = git(selected, "ls-tree", "-rz", revision)
    entries = []
    for record in tree.split(b"\0"):
        if not record:
            continue
        identity, name = record.split(b"\t", 1)
        mode, kind, oid = identity.split(b" ")
        path = Path(os.fsdecode(name))
        if (
            mode not in (b"100644", b"100755")
            or kind != b"blob"
            or path.is_absolute()
            or any(part in (".", "..", ".git") for part in path.parts)
        ):
            raise ValueError("Runtime tree requires ordinary contained files")
        entries.append((path, mode, oid))
    blobs = git(
        selected,
        "cat-file",
        "--batch",
        input=b"".join(oid + b"\n" for _, _, oid in entries),
    )
    offset = 0
    destination.mkdir()
    for relative_path, mode, oid in entries:
        end = blobs.index(b"\n", offset)
        actual, kind, size = blobs[offset:end].split(b" ")
        if actual != oid or kind != b"blob":
            raise ValueError("Git returned inconsistent runtime objects")
        offset = end + 1
        body = blobs[offset : offset + int(size)]
        offset += int(size)
        if len(body) != int(size) or blobs[offset : offset + 1] != b"\n":
            raise ValueError("Git returned a truncated runtime blob")
        offset += 1
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        target.chmod(0o755 if mode == b"100755" else 0o644)
    if offset != len(blobs):
        raise ValueError("Git returned unexpected runtime data")


def store(revision: str, *, gc_root: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="chainman-git-source-") as temporary:
        source = Path(temporary) / "source"
        materialize(revision, source)
        expected = tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "path",
                str(source),
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        result = tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                "--impure",
                "--out-link",
                str(gc_root),
                "--print-out-paths",
                "--expr",
                'builtins.path { path = builtins.toPath (builtins.getEnv "CHAINMAN_GIT_SOURCE"); name = "chainman-source"; }',
            ],
            env=dict(os.environ, CHAINMAN_GIT_SOURCE=str(source)),
            text=True,
            capture_output=True,
            check=True,
        )
    runtime = Path(result.stdout.strip())
    if (
        runtime.parent != Path("/nix/store")
        or runtime.is_symlink()
        or not runtime.is_dir()
    ):
        raise ValueError("Git source import did not return a real Nix store tree")
    actual = tc.managed_run(
        [
            tc.nix_command(),
            "--extra-experimental-features",
            "nix-command",
            "hash",
            "path",
            str(runtime),
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    if actual != expected:
        raise ValueError("Runtime store differs from the verified Git source")
    return runtime
