"""Build a reproducible, explicit-inventory demonstration archive."""

import io
from pathlib import Path
import tarfile
from toolchain import atomic_bytes, contained, regular_input


def build(root: Path) -> None:
    output = contained(root, "dist/demo.tar")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in ("greeting.py", "labels.txt"):
            data = regular_input(root, "examples/core/" + name)
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
    atomic_bytes(output, buffer.getvalue(), 0o644)


if __name__ == "__main__":
    build(Path(__file__).resolve().parents[1])
