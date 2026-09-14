"""Check explicit consumer roots against a released runtime without running them."""

import argparse
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping
from typing import TypedDict

import chainman
import chainman_updates
import config_inspection
import configuration
import toolchain as tc
from adapter_data import table, text


class ConsumerStatus(TypedDict):
    root: str
    valid: bool
    runtime_files: int
    declarations: dict[str, int]
    baseline_equal: bool


def check(
    root: Path,
    release: Mapping[str, object],
    baseline: Mapping[str, object] | None = None,
) -> ConsumerStatus:
    cfg = config_inspection.validated(root)
    if "recipes" in cfg:
        import recipes

        for consumer in recipes.roots(root):
            recipes.sync(consumer, check=True)
    pin = table(json.loads(tc.regular_input(root, "chainman.lock")), "Runtime pin")
    for field in ("version", "revision", "url", "narHash"):
        if pin.get(field) != release[field]:
            raise ValueError(f"Consumer runtime {field} differs from the candidate")
    bundle = pin.get("bundled_archive")
    if bundle and (
        hashlib.sha256(
            tc.regular_input(root, text(bundle, "Runtime bundle"))
        ).hexdigest()
        != release["archive_sha256"]
    ):
        raise ValueError("Consumer bundled archive differs from the candidate")
    for source, destination in (
        ("chainman.sh", "chainman.sh"),
        ("fetch.nix", "chainman-fetch.nix"),
    ):
        if not chainman_updates.managed_matches(
            chainman_updates.managed_state(root, f"scripts/{destination}"),
            chainman_updates.managed_state(chainman.RUNTIME, f"bootstrap/{source}"),
        ):
            raise ValueError(
                f"Consumer bootstrap differs from the candidate: {destination}"
            )
    copies = chainman_updates.managed_paths(root)
    for destination, source in copies.items():
        if not chainman_updates.managed_matches(
            chainman_updates.managed_state(root, destination),
            chainman_updates.managed_state(root, source),
        ):
            raise ValueError(f"Consumer runtime copy differs: {destination}")
    if baseline is not None:
        before, _ = configuration.compile(baseline)
        before.setdefault("modules", ["project"])
        after = dict(cfg)
        after["schema"] = before["schema"]
        if before != after:
            raise ValueError(
                "Expanded consumer configuration differs from the baseline"
            )
    return {
        "root": str(root),
        "valid": True,
        "runtime_files": len(copies),
        "declarations": {
            kind: len(table(cfg.get(kind, {}), kind)) for kind in configuration.FIELDS
        },
        "baseline_equal": baseline is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--baselines", type=Path)
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    release = table(json.loads(args.release.read_text()), "Candidate release")
    baselines = (
        table(json.loads(args.baselines.read_text()), "Consumer baselines")
        if args.baselines
        else {}
    )
    rows: list[ConsumerStatus] = []
    for root in args.roots:
        root = root.absolute()
        baseline = baselines.get(str(root / "chainman.toml"))
        if args.baselines and baseline is None:
            raise ValueError(f"Missing explicit baseline for {root}")
        rows.append(
            check(
                root, release, None if baseline is None else table(baseline, "Baseline")
            )
        )
    print(json.dumps({"schema": 1, "consumers": rows}, indent=2))


if __name__ == "__main__":
    main()
