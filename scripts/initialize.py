"""Initialize a consumer from an explicitly selected immutable public release."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile

from adapter_data import table
import chainman_updates
import example
import registry


def initialize(destination: Path, version: str) -> dict[str, str | int]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(
            "Choose an explicit numeric release version, for example 0.1.0"
        )
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError("Initialization destination must not contain symlinks")
    if destination.exists() and (
        not destination.is_dir() or any(destination.iterdir())
    ):
        raise ValueError("Choose a new or empty project directory")
    tag = f"v{version}"
    base = "https://github.com/chainmandev/chainman/releases/download/" + tag
    release = table(
        registry.data(
            f"https://api.github.com/repos/chainmandev/chainman/releases/tags/{tag}"
        ),
        "Public release",
    )
    selected = registry.Release(tag, registry.timestamp(release.get("published_at")))
    runtime_name = f"chainman-{version}.tar.gz"
    source_name = f"chainman-source-{version}.tar.gz"
    # Explicit initial adoption is deliberate trust in this release. Automatic
    # updates subsequently apply the generated project's normal maturity policy.
    bodies, revision = chainman_updates.published_assets(
        selected,
        {"minimum_age_days": 0},
        datetime.now(timezone.utc),
        ("chainman-release.json", runtime_name, source_name),
    )
    metadata = table(json.loads(bodies["chainman-release.json"]), "Release metadata")
    source = table(metadata.get("source"), "Source archive metadata")
    if (
        metadata.get("schema") != 1
        or metadata.get("version") != version
        or metadata.get("revision") != revision
        or metadata.get("url") != f"{base}/{runtime_name}"
        or source.get("filename") != source_name
        or source.get("url") != f"{base}/{source_name}"
    ):
        raise ValueError("Release metadata does not match its public tag and assets")
    with tempfile.TemporaryDirectory(prefix="chainman-initialize-") as temporary:
        staging = Path(temporary)
        for name, body in bodies.items():
            (staging / name).write_bytes(body)
        # The existing generator validates flat hashes, NAR hashes, source/runtime
        # agreement, archive paths and file types before writing consumer files.
        return example.create(destination, staging / "chainman-release.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("version")
    args = parser.parse_args()
    try:
        result = initialize(args.destination.absolute(), args.version)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Chainman initialization: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
