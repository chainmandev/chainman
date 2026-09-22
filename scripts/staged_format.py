"""Inventory isolated formatter output. Native hooks own Git and application."""

import hashlib
import os
from pathlib import Path
import stat


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
