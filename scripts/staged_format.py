"""Format the active Git index and merge formatting into unstaged work.

No user stashes, mutable justfiles, application gates or Git clean filters are
involved. Git owns the active index's name; during commit -a it can be index.lock.
We lock that index with its own .lock suffix and never remove Git's lock.
"""

from collections.abc import Mapping
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

import configuration_files
import formatters
import toolchain as tc


def git(
    root: Path,
    *args: str,
    index: Path | None = None,
    input: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    # Preserve repository discovery for linked worktrees, but never inherited
    # alternate object stores, replacement refs, command-config or work trees.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
    )
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    return subprocess.run(
        [
            "git",
            "--literal-pathspecs",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "gc.auto=0",
            *args,
        ],
        cwd=root,
        env=env,
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def active_index(root: Path) -> Path:
    actual = os.fsdecode(git(root, "rev-parse", "--show-toplevel").stdout.rstrip(b"\n"))
    if Path(actual).resolve() != root:
        raise ValueError("Formatting requires the project's own Git repository")
    explicit = os.environ.get("GIT_INDEX_FILE")
    path = (
        Path(explicit)
        if explicit
        else Path(
            os.fsdecode(
                git(
                    root, "rev-parse", "--path-format=absolute", "--git-path", "index"
                ).stdout.rstrip(b"\n")
            )
        )
    )
    if not path.is_absolute():
        path = root / path
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Active Git index must not contain symlinks")
    if path.exists() and not path.is_file():
        raise ValueError("Active Git index must be a regular file")
    return path


def entries(root: Path, index: Path) -> dict[str, tuple[str, str]]:
    result = {}
    for item in git(root, "ls-files", "--stage", "-z", index=index).stdout.split(b"\0"):
        if not item:
            continue
        header, raw = item.split(b"\t", 1)
        mode, oid, stage = header.decode("ascii").split()
        if stage != "0":
            raise ValueError("Resolve unmerged index entries before formatting")
        path = os.fsdecode(raw)
        if Path(path).is_absolute() or any(
            part in {"..", ".git"} for part in Path(path).parts
        ):
            raise ValueError("Unsafe Git index path")
        result[path] = (mode, oid)
    return result


def identity(path: Path) -> tuple[bytes, int] | None:
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    return path.read_bytes(), stat.S_IMODE(info.st_mode)


def inventory(root: Path) -> dict[str, tuple[str, int]]:
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        # Only Chainman's own candidate cache is excluded. New files elsewhere,
        # including formatter caches, are unexpected output and fail closed.
        if base == root:
            dirs[:] = [d for d in dirs if d != ".git"]
        if base == root / ".cache":
            dirs[:] = [d for d in dirs if d != "toolchain"]
        for name in [*dirs, *files]:
            path = base / name
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                result[relative] = (
                    hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest(),
                    info.st_mode,
                )
            elif stat.S_ISREG(info.st_mode):
                result[relative] = (
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                    stat.S_IMODE(info.st_mode),
                )
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"Unexpected formatter output: {relative!r}")
    return result


def materialize(
    root: Path, candidate: Path, index: Path, items: Mapping[str, tuple[str, str]]
) -> None:
    """Export raw index blobs, never checkout filters or the current working tree."""
    # Batch avoids one process per file in large application repositories.
    regular = [
        (path, mode, oid)
        for path, (mode, oid) in items.items()
        if mode in {"100644", "100755"}
    ]
    git(candidate, "init", "--quiet", "--template=")
    for start in range(0, len(regular), 32):
        batch = regular[start : start + 32]
        objects = git(
            root,
            "cat-file",
            "--batch",
            input="".join(oid + "\n" for _, _, oid in batch).encode(),
        ).stdout
        offset = 0
        records = bytearray()
        for path, mode, oid in batch:
            end = objects.index(b"\n", offset)
            header = objects[offset:end].decode().split()
            if header[:2] != [oid, "blob"]:
                raise ValueError("Missing or non-blob staged object")
            size = int(header[2])
            body = objects[end + 1 : end + 1 + size]
            offset = end + size + 2
            target = tc.contained(candidate, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            target.chmod(0o755 if mode == "100755" else 0o644)
            records.extend(
                mode.encode() + b" " + oid.encode() + b"\t" + os.fsencode(path) + b"\0"
            )
        # No checkout/add filters: retain raw staged bytes even with attributes.
        git(
            candidate,
            "hash-object",
            "-w",
            "--no-filters",
            "--",
            *("./" + path for path, _, _ in batch),
        )
        git(candidate, "update-index", "-z", "--index-info", input=bytes(records))
    tree = git(candidate, "write-tree").stdout.strip().decode()
    commit = (
        git(
            candidate,
            "-c",
            "user.name=chainman snapshot",
            "-c",
            "user.email=snapshot@example.invalid",
            "commit-tree",
            tree,
            input=b"Staged formatting snapshot\n",
        )
        .stdout.strip()
        .decode()
    )
    git(candidate, "update-ref", "HEAD", commit)


def merge(
    root: Path,
    directory: Path,
    base: bytes,
    formatted: bytes,
    working: bytes,
    label: str,
) -> bytes:
    if working == base:
        return formatted
    for name, body in (("base", base), ("formatted", formatted), ("working", working)):
        (directory / name).write_bytes(body)
    result = git(
        root,
        "merge-file",
        "-p",
        "--diff3",
        str(directory / "working"),
        str(directory / "base"),
        str(directory / "formatted"),
        check=False,
    )
    if result.returncode:
        raise ValueError(
            f"Formatting conflicts with unstaged edits in {label!r}; index and working files are unchanged. Stage a coherent version or format the file manually."
        )
    return result.stdout


def run(root: Path, *, check: bool = False) -> int:
    cfg = tc.config(root)
    declared = formatters.declarations(cfg)
    if not declared:
        raise ValueError(
            "Staged formatting needs [formatters.NAME] declarations; repository-wide tasks are never a fallback"
        )
    index = active_index(root)
    before_index = identity(index)
    before_head = (
        git(root, "rev-parse", "--verify", "HEAD", check=False).stdout,
        git(root, "symbolic-ref", "-q", "HEAD", check=False).stdout,
    )
    items = entries(root, index)
    # --no-renames selects destinations directly, including staged rename edits.
    changed = [
        os.fsdecode(p)
        for p in git(
            root,
            "diff",
            "--cached",
            "--no-renames",
            "--name-only",
            "--diff-filter=ACM",
            "-z",
            index=index,
        ).stdout.split(b"\0")
        if p
    ]
    paths = sorted(
        {
            p
            for spec in declared.values()
            for p in formatters.selected(spec, changed)
            if items.get(p, ("", ""))[0] in {"100644", "100755"}
        }
    )
    if identity(index) != before_index:
        raise ValueError(
            "Git index changed while selecting staged files; retry formatting"
        )
    if not paths:
        return 0
    # Don't chase a tracked symlink/submodule, or flatten sparse checkout flags.
    flags = git(root, "ls-files", "-v", "-z", index=index).stdout.split(b"\0")
    if any(
        item
        and os.fsdecode(item[2:]) in paths
        and (chr(item[0]).islower() or item[:1] == b"S")
        for item in flags
    ):
        raise ValueError(
            "Clear assume-unchanged/skip-worktree on selected files before formatting"
        )
    before_files = {path: identity(tc.contained(root, path)) for path in paths}
    if any(value is None for value in before_files.values()):
        raise ValueError(
            "A staged formatting input is missing from the working tree; restore it before formatting"
        )
    config_name = (
        "chainman.toml" if (root / "chainman.toml").is_file() else "toolchain.toml"
    )
    authority = configuration_files.read(root, config_name).documents
    pool = tc.contained(root, ".chainman/staged-format")
    pool.mkdir(parents=True, exist_ok=True)
    for journal in pool.glob("*/apply.json"):
        raise ValueError(
            f"Interrupted formatting requires review: {journal.parent}. Original index/files and formatted results are preserved there; do not overwrite subsequent edits."
        )
    directory = Path(tempfile.mkdtemp(prefix="transaction-", dir=pool))
    candidate = directory / "snapshot"
    candidate.mkdir()
    keep = False
    try:
        saved_index = directory / "original-index"
        if before_index is None:
            git(root, "read-tree", "--empty", index=saved_index)
        else:
            saved_index.write_bytes(before_index[0])
        materialize(root, candidate, saved_index, items)
        before = inventory(candidate)
        # Freeze orchestration declarations from the original project separately
        # from staged formatter configs. No candidate justfile is executed.
        frozen = directory / "authority"
        for path, body in authority.items():
            tc.atomic_bytes(tc.contained(frozen, path), body)
        tc.atomic_bytes(frozen / "authority-root", (str(candidate) + "\n").encode())
        if (root / "chainman.lock").is_file():
            tc.atomic_bytes(
                frozen / "chainman.lock", tc.regular_input(root, "chainman.lock")
            )
        previous = os.environ.get("CHAINMAN_ENTRY_AUTHORITY")
        os.environ["CHAINMAN_ENTRY_AUTHORITY"] = str(frozen)
        try:

            def prepared() -> None:
                nonlocal before
                installed = inventory(candidate)
                if any(installed.get(path) != value for path, value in before.items()):
                    raise ValueError("Formatter setup changed staged source files")
                before = installed

            formatters.execute(candidate, cfg, paths, check=check, prepared=prepared)
        finally:
            if previous is None:
                os.environ.pop("CHAINMAN_ENTRY_AUTHORITY", None)
            else:
                os.environ["CHAINMAN_ENTRY_AUTHORITY"] = previous
        after = inventory(candidate)
        touched = sorted(
            p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
        )
        if any(
            p not in paths or p not in after or before[p][1] != after[p][1]
            for p in touched
        ):
            raise ValueError(
                f"Formatter changed files outside its selected content: {touched!r}"
            )
        if check or not touched:
            return 0
        replacement = directory / "formatted-index"
        shutil.copyfile(saved_index, replacement)
        merged: dict[str, bytes] = {}
        for number, path in enumerate(touched):
            mode, oid = items[path]
            base = git(root, "cat-file", "blob", oid).stdout
            formatted = tc.regular_input(candidate, path)
            working = before_files[path]
            assert working is not None
            merged[path] = merge(root, directory, base, formatted, working[0], path)
            blob = git(
                root, "hash-object", "-w", "--stdin", input=formatted
            ).stdout.strip()
            git(
                root,
                "update-index",
                "-z",
                "--index-info",
                index=replacement,
                input=mode.encode() + b" " + blob + b"\t" + os.fsencode(path) + b"\0",
            )
            (directory / f"original-{number}").write_bytes(working[0])
            (directory / f"merged-{number}").write_bytes(merged[path])
        # Lock only the active index. With commit -a this is index.lock.lock,
        # preserving the index.lock owned by Git's parent commit process.
        lock = index.with_name(index.name + ".lock")
        try:
            descriptor = os.open(
                lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError as error:
            raise ValueError(
                f"Git index is in use: {lock}; no formatting applied"
            ) from error
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(replacement.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            if (
                identity(index) != before_index
                or (
                    git(root, "rev-parse", "--verify", "HEAD", check=False).stdout,
                    git(root, "symbolic-ref", "-q", "HEAD", check=False).stdout,
                )
                != before_head
                or any(
                    identity(tc.contained(root, p)) != before_files[p] for p in paths
                )
                or configuration_files.read(root, config_name).documents != authority
            ):
                raise ValueError(
                    "HEAD, index, working files or configuration changed during formatting; nothing applied"
                )
            tc.atomic_json(
                directory / "apply.json",
                {
                    "index": str(index),
                    "paths": touched,
                    "modes": [
                        value[1]
                        for p in touched
                        if (value := before_files[p]) is not None
                    ],
                },
            )
            keep = True
            for path in touched:
                current = before_files[path]
                assert current is not None
                if identity(tc.contained(root, path)) != current:
                    raise ValueError(
                        f"Concurrent edit while applying {path!r}; inspect preserved recovery files"
                    )
                tc.atomic_bytes(root / path, merged[path], mode=current[1])
            if before_index is not None:
                lock.chmod(before_index[1])
            os.replace(lock, index)
            (directory / "apply.json").unlink()
            keep = False
        finally:
            lock.unlink(missing_ok=True)
        print(f"Formatted {len(touched)} staged file(s); unstaged edits preserved")
        return 0
    finally:
        if not keep:
            shutil.rmtree(directory)
        else:
            print(f"Formatting recovery material: {directory}")


if __name__ == "__main__":
    import sys

    try:
        with tc.operation(Path.cwd(), exclusive=True, new_execution=True):
            raise SystemExit(run(Path.cwd()))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"chainman formatting: {error}", file=sys.stderr)
        raise SystemExit(1) from error
