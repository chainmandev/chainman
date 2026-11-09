"""Frozen pnpm installs with the project profile's exact package-manager binary."""

from collections.abc import Mapping, Sequence
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


def required_version(
    manifest: Mapping[str, object], *, required: bool = True
) -> str | None:
    if not required and "packageManager" not in manifest:
        return None
    selected = manifest.get("packageManager")
    match = (
        re.fullmatch(
            r"pnpm@(\d+\.\d+\.\d+)(?:\+sha(?:224|256|384|512)\.[a-zA-Z0-9+/=]+)?",
            selected,
        )
        if isinstance(selected, str)
        else None
    )
    if match is None:
        raise ValueError("Declare an exact pnpm version in package.json packageManager")
    return match[1]


def options(actual: str, expected: str | None) -> list[str]:
    if re.fullmatch(r"\d+\.\d+\.\d+", actual) is None:
        raise ValueError("The selected profile lacks one exact pnpm version")
    if expected is not None and actual != expected:
        raise ValueError(
            f"pnpm mismatch: packageManager requires {expected}, selected profile provides {actual}. Reconcile the project flake and packageManager; chainman will not download a replacement."
        )
    # Identity was checked above. pnpm 12's error mode also locks and resolves
    # packageManagerDependencies, including for a matching binary.
    return (
        ["--pm-on-fail=ignore"]
        if int(actual.split(".")[0]) >= 11
        else [
            "--config.manage-package-manager-versions=false",
            "--config.package-manager-strict-version=true",
        ]
    )


def execution_command(roots: Sequence[Path], argv: list[str]) -> list[str]:
    """Probe inside the task profile, then replace this wrapper with its command."""
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "exec",
        json.dumps([str(root) for root in roots]),
        *argv,
    ]


def execution_environment(roots: Sequence[Path]) -> dict[str, str]:
    env = dict(os.environ)
    expected = [
        required_version(json.loads((root / "package.json").read_bytes()))
        for root in roots
    ]
    if not expected:
        raise ValueError("Checked pnpm execution requires a managed setup manifest")
    actual = subprocess.run(
        ["pnpm", "--version"],
        cwd="/",
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=15,
    ).stdout.strip()
    for version in expected:
        options(actual, version)
    # pmOnFail=error itself makes pnpm 12 resolve a separate manager lock, even
    # for `run` with the right binary. Identity was checked in this exact profile;
    # disable only that duplicate manager handling for this command and children.
    for name in (
        "PNPM_CONFIG_PM_ON_FAIL",
        "pnpm_config_pm_on_fail",
        "npm_config_pm_on_fail",
        "NPM_CONFIG_PM_ON_FAIL",
    ):
        env[name] = "ignore"
    for prefix in ("npm_config_", "pnpm_config_", "NPM_CONFIG_", "PNPM_CONFIG_"):
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
        env[
            prefix
            + (
                "package_manager_strict_version"
                if prefix.islower()
                else "PACKAGE_MANAGER_STRICT_VERSION"
            )
        ] = "true"
    return env


def run(action: str, root: Path) -> int:
    if action not in {"install", "check"}:
        raise ValueError("pnpm setup accepts install or check")
    manifest = json.loads((root / "package.json").read_bytes())
    selected = required_version(manifest)
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
    command = ["pnpm", *options(actual, selected)]
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
        if len(sys.argv) >= 4 and sys.argv[1] == "exec":
            declared = json.loads(sys.argv[2])
            if (
                not isinstance(declared, list)
                or not declared
                or any(
                    not isinstance(path, str) or not Path(path).is_absolute()
                    for path in declared
                )
            ):
                raise ValueError("Checked pnpm execution requires absolute setup roots")
            env = execution_environment([Path(path) for path in declared])
            os.execvpe(sys.argv[3], sys.argv[3:], env)
        if len(sys.argv) != 2:
            raise ValueError("pnpm setup requires one action")
        raise SystemExit(run(sys.argv[1], Path.cwd()))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"chainman pnpm: {error}", file=sys.stderr)
        raise SystemExit(1) from error
