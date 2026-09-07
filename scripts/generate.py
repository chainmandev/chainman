"""Generate a neutral asset, or verify the committed bytes without rewriting."""

import argparse
import json
from pathlib import Path
from toolchain import atomic_bytes, contained, regular_input


def generate(root: Path, check: bool = False) -> None:
    source = json.loads(regular_input(root, "examples/core/labels.json"))
    content = (
        "\n".join(f"{key}={value}" for key, value in sorted(source.items())) + "\n"
    ).encode()
    target = contained(root, "examples/core/labels.txt")
    if check:
        if regular_input(root, "examples/core/labels.txt") != content:
            raise ValueError("Generated asset is stale; run just format.")
    else:
        atomic_bytes(target, content, 0o644)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generate(Path(__file__).resolve().parents[1], args.check)
