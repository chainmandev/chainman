"""Check explicit consumer roots against a released runtime without running them."""

import argparse
import hashlib
import json
from pathlib import Path
import stat

import chainman
import chainman_updates
import config_inspection
import configuration
import toolchain as tc


def check(root, release, baseline=None):
    cfg = config_inspection.validated(root)
    if "recipes" in cfg:
        import recipes

        for consumer in recipes.roots(root):
            recipes.sync(consumer, check=True)
    pin = json.loads(tc.regular_input(root, "chainman.lock"))
    for field in ("version", "revision", "url", "narHash"):
        if pin.get(field) != release[field]:
            raise ValueError(f"Consumer runtime {field} differs from the candidate")
    bundle = pin.get("bundled_archive")
    if (
        not bundle
        or hashlib.sha256(tc.regular_input(root, bundle)).hexdigest()
        != release["archive_sha256"]
    ):
        raise ValueError("Consumer bundled archive differs from the candidate")
    for source, destination in (
        ("chainman.sh", "chainman.sh"),
        ("fetch.nix", "chainman-fetch.nix"),
    ):
        path = chainman.RUNTIME / "bootstrap" / source
        target = tc.contained(root, f"scripts/{destination}")
        if tc.regular_input(
            root, f"scripts/{destination}"
        ) != path.read_bytes() or stat.S_IMODE(target.stat().st_mode) != stat.S_IMODE(
            path.stat().st_mode
        ):
            raise ValueError(
                f"Consumer bootstrap differs from the candidate: {destination}"
            )
    copies = chainman_updates.managed_paths(root)
    for destination, source in copies.items():
        if chainman_updates.managed_state(
            root, destination
        ) != chainman_updates.managed_state(root, source):
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
        "declarations": {kind: len(cfg.get(kind, {})) for kind in configuration.FIELDS},
        "baseline_equal": baseline is not None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--baselines", type=Path)
    parser.add_argument("roots", nargs="+", type=Path)
    args = parser.parse_args()
    release = json.loads(args.release.read_text())
    baselines = json.loads(args.baselines.read_text()) if args.baselines else {}
    rows = []
    for root in args.roots:
        root = root.absolute()
        baseline = baselines.get(str(root / "chainman.toml"))
        if args.baselines and baseline is None:
            raise ValueError(f"Missing explicit baseline for {root}")
        rows.append(check(root, release, baseline))
    print(json.dumps({"schema": 1, "consumers": rows}, indent=2))


if __name__ == "__main__":
    main()
