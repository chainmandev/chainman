"""Frozen pnpm installs with the project profile's exact package-manager binary."""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from adapter_data import Table


def expand(spec: Mapping[str, object]) -> Table:
    if "commands" in spec or "readiness" in spec:
        raise ValueError(
            "pnpm=true supplies commands and readiness; use explicit declarations for a custom installer"
        )
    command = ["python3", str(Path(__file__).resolve())]
    return {
        "commands": [[*command, "install"]],
        "readiness": {"command": [*command, "check"], "timeout_seconds": 30},
    }


def run(action: str, root: Path) -> int:
    if action not in {"install", "check"}:
        raise ValueError("pnpm setup accepts install or check")
    manifest = json.loads((root / "package.json").read_bytes())
    selected = manifest.get("packageManager", "")
    match = re.fullmatch(
        r"pnpm@(\d+\.\d+\.\d+)(?:\+sha(?:224|256|384|512)\.[a-zA-Z0-9+/=]+)?", selected
    )
    if match is None:
        raise ValueError("Declare an exact pnpm version in package.json packageManager")
    env = dict(os.environ)
    # pnpm 10 and 11 recognize these spellings differently. Explicit command
    # options also prevent workspace settings from requesting a silent download.
    for prefix in ("npm_config_", "NPM_CONFIG_", "PNPM_CONFIG_"):
        env[
            prefix
            + (
                "manage_package_manager_versions"
                if prefix.islower()
                else "MANAGE_PACKAGE_MANAGER_VERSIONS"
            )
        ] = "false"
        env[
            prefix
            + (
                "verify_deps_before_run"
                if prefix.islower()
                else "VERIFY_DEPS_BEFORE_RUN"
            )
        ] = "error"
    # Version/help parsing differs across pnpm generations and can select the
    # project's manager before processing config overrides. Inspect the binary
    # outside every project instead of relying on those short-circuit flags.
    actual = subprocess.run(
        ["pnpm", "--version"],
        cwd="/",
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=15,
    ).stdout.strip()
    if actual != match[1]:
        raise ValueError(
            f"pnpm mismatch: packageManager requires {match[1]}, selected profile provides {actual}. Reconcile the project flake and packageManager; chainman will not download a replacement."
        )
    command = ["pnpm"] + (
        ["--pm-on-fail=error"]
        if int(actual.split(".")[0]) >= 11
        else [
            "--config.manage-package-manager-versions=false",
            "--config.package-manager-strict-version=true",
        ]
    )
    arguments = (
        ["install", "--frozen-lockfile", "--config.confirmModulesPurge=false"]
        if action == "install"
        else ["--config.verify-deps-before-run=error", "exec", "node", "-e", ""]
    )
    return subprocess.run(
        [*command, *arguments], cwd=root, env=env, check=False
    ).returncode


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("pnpm setup requires one action")
        raise SystemExit(run(sys.argv[1], Path.cwd()))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"chainman pnpm: {error}", file=sys.stderr)
        raise SystemExit(1) from error
