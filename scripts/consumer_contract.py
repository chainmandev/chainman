"""Check explicit consumer roots against a released runtime without running them."""

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

import chainman_updates
import config_inspection
import configuration
import git_runtime
import pnpm_setup
import toolchain as tc
from adapter_data import table


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
    pin = git_runtime.pin(tc.regular_input(root, "chainman.lock"))
    if pin != release["revision"]:
        raise ValueError("Consumer runtime revision differs from the candidate")
    if "recipes" in cfg:
        import recipes

        recipes.verification(cfg)
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
        setups = table(before.get("setup", {}), "Baseline setup")
        for name, raw in setups.items():
            spec = table(raw, f"Baseline setup.{name}")
            if spec.get("pnpm"):
                setups[name] = {**spec, **pnpm_setup.expand(spec)}
        if "setup" in before:
            before["setup"] = setups
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
    parser.add_argument("--revision", required=True)
    parser.add_argument("--baselines", type=Path)
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    release = {"revision": git_runtime.pin((args.revision + "\n").encode())}
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
