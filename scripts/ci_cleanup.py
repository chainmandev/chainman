"""Explicitly reclaim unused SDKs only on disposable hosted Linux runners."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

from toolchain import ROOT, config, contained, module, operation, size
from adapter_data import strings, table

SDK_PATHS = {
    "android": "usr/local/lib/android",
    "dotnet": "usr/share/dotnet",
    "haskell": "opt/ghc",
}


def plan(
    root: Path, filesystem: Path, env: dict[str, str], selected: list[str]
) -> list[Path]:
    expected = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
    }
    if (
        any(env.get(key) != value for key, value in expected.items())
        or env.get("TOOLCHAIN_CONTAINER") == "1"
        or env.get("CHAINMAN_MODE") == "container-nix"
    ):
        raise ValueError(
            "SDK cleanup requires an explicitly disposable GitHub-hosted Linux host"
        )
    cfg = config(root)
    disposable = table(cfg.get("ci", {}), "CI settings").get("disposable_sdks", [])
    if not isinstance(disposable, list) or any(
        sdk not in SDK_PATHS for sdk in disposable
    ):
        raise ValueError(
            "Unknown disposable SDK; configure only the declared SDK names"
        )
    required: set[str] = set()
    for name in set(strings(cfg["modules"], "Modules") + selected):
        spec = module(name, root)
        required.update(strings(spec.get("native_sdks", []), "Module SDKs"))
        if spec["profile"] == "flutter":
            required.add("android")
    paths = []
    for sdk in sorted(set(disposable) - required):
        path = contained(filesystem, SDK_PATHS[sdk])
        if path.exists():
            if not path.is_dir():
                raise ValueError("Declared disposable SDK must be a directory")
            size(
                path
            )  # Validate every candidate, including nested escapes, before removal.
            paths.append(path)
    return paths


def remove(paths: list[Path], privileged: bool = False) -> list[str]:
    removed = []
    for path in paths:
        if privileged:
            subprocess.run(
                [
                    "/usr/bin/sudo",
                    "--non-interactive",
                    "--",
                    "rm",
                    "-rf",
                    "--",
                    str(path),
                ],
                check=True,
            )
        else:
            shutil.rmtree(path)
        if path.exists() or path.is_symlink():
            raise OSError("SDK deletion did not remove its declared directory")
        removed.append(str(path))
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--sudo",
        action="store_true",
        help="Explicit noninteractive privilege for the listed SDK paths",
    )
    parser.add_argument("--module", action="append", default=[])
    args = parser.parse_args()
    if platform.system() != "Linux" or (args.sudo and not args.apply):
        raise ValueError("SDK cleanup requires Linux; --sudo also requires --apply")
    with operation():
        paths = plan(ROOT, Path("/"), dict(os.environ), args.module)
        report = {
            "planned": [str(p) for p in paths],
            "apparent_bytes": sum(size(p) for p in paths),
            "applied": args.apply,
        }
        print(json.dumps(report), flush=True)
        if args.apply:
            print(json.dumps({"removed": remove(paths, args.sudo)}))


if __name__ == "__main__":
    main()
