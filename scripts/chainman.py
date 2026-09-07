"""Run an explicitly selected consumer with an immutable tooling runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, unquote

import toolchain as tc

RUNTIME = Path(__file__).resolve().parents[1]


def configuration(root: Path) -> dict:
    return tc.config(root)


def profile(root: Path, name: str) -> tuple[str | None, dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid profile name")
    cfg = configuration(root)
    spec = cfg.get("profiles", {}).get(name)
    if name == "host":
        return None, {}
    if spec is None:
        # Built-in modules remain available without loading them by default.
        if name not in {
            "default",
            "core",
            "javascript",
            "rust",
            "python",
            "go",
            "flutter",
            "swift",
            "compose",
            "browser",
        }:
            raise ValueError(f"Profile {name!r} is not declared")
        spec = {"runtime_profile": "core" if name == "default" else name}
    if "flake" in spec:
        path, sep, attribute = spec["flake"].partition("#")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]+", attribute):
            raise ValueError("A project profile must select a flake path#shell")
        if path.endswith("/flake.nix") or path == "flake.nix":
            path = str(Path(path).parent)
        location = tc.contained(root, path)
        tc.regular_input(location, "flake.nix")
    else:
        location = RUNTIME / "nix"
        attribute = spec.get("runtime_profile", "core")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", attribute):
            raise ValueError("Invalid runtime shell")
    return f"path:{quote(str(location), safe='/')}#{attribute}", spec


def profile_fingerprint(root: Path, name: str, ref: str | None) -> str:
    digest = hashlib.sha256(str(RUNTIME).encode())
    digest.update((ref or "host").encode())
    for path in (root / "chainman.toml", root / "chainman.lock"):
        if path.exists():
            digest.update(tc.regular_input(root, path.name))
    if ref:
        directory = Path(unquote(ref[5:].partition("#")[0]))
        for name in ("flake.nix", "flake.lock"):
            if (directory / name).exists():
                digest.update(tc.regular_input(directory, name))
    return digest.hexdigest()


def execute(
    root: Path,
    name: str,
    argv: list[str],
    *,
    env=None,
    check=True,
    cwd: Path | None = None,
    **kwargs,
):
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("Commands must be nonempty argument arrays")
    ref, spec = profile(root, name)
    selected = dict(os.environ if env is None else env)
    selected.update(
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(RUNTIME),
        TOOLCHAIN_MODE=selected.get("CHAINMAN_MODE", "host-nix"),
    )
    for values in (
        configuration(root).get("environment", {}).get("values", {}),
        spec.get("environment", {}),
    ):
        for key, value in values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(
                value, str
            ):
                raise ValueError(
                    "Environment entries require variable names and strings"
                )
            selected[key] = value.replace("{root}", str(root))
    for key in configuration(root).get("environment", {}).get("unset", []):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Environment unset entries must be variable names")
        if key.startswith(("CHAINMAN_", "TOOLCHAIN_")) or key in {
            "SCCACHE_SERVER_UDS",
            "RUSTC_WRAPPER",
        }:
            raise ValueError(
                "Cannot unset managed runtime and cache lifecycle variables"
            )
        selected.pop(key, None)
    token = profile_fingerprint(root, name, ref)
    active = (
        selected.get("CHAINMAN_ACTIVE_PROFILE") == name
        and selected.get("CHAINMAN_ACTIVE_FINGERPRINT") == token
    )
    target = root if cwd is None else cwd
    command = argv
    if ref and (not active or selected.get("TOOLCHAIN_FRESH") == "1"):
        command = [
            selected.get("CHAINMAN_NIX_BIN", "nix"),
            "--extra-experimental-features",
            "nix-command flakes",
            "develop",
            ref,
            "--no-write-lock-file",
            "--command",
            "sh",
            "-eu",
            "-c",
            'cd "$1"; shift; exec "$@"',
            "sh",
            str(target),
            *argv,
        ]
    selected.update(CHAINMAN_ACTIVE_PROFILE=name, CHAINMAN_ACTIVE_FINGERPRINT=token)
    selected.pop("TOOLCHAIN_FRESH", None)
    return tc.managed_run(command, cwd=target, env=selected, check=check, **kwargs)


def run_hook(root: Path, commands, *, name="default", extra=(), env=None):
    if not isinstance(commands, list) or not commands:
        raise ValueError("A hook must declare at least one argument-array command")
    for index, command in enumerate(commands):
        if not isinstance(command, list) or not command:
            raise ValueError("Hook commands must be nonempty argument arrays")
        argv = [*command, *(extra if index == len(commands) - 1 else [])]
        execute(root, name, argv, env=env)


def run_project(root: Path, action: str, extra: list[str]):
    cfg = configuration(root)
    with tc.operation(root) as outer_operation:
        if outer_operation and cfg.get("cache", {}).get("automatic_prune", True):
            tc.prune(root)
        env = tc.environment(root)
        if action in cfg.get("commands", {}) and action != "setup":
            name = cfg.get("command_profiles", {}).get(
                action, cfg.get("project", {}).get("default_profile", "default")
            )
            with tc.compiler_cache(name, env, root) as owned:
                run_hook(
                    root, cfg["commands"][action], name=name, extra=extra, env=owned
                )
            return
        if extra:
            raise ValueError("Module actions do not accept extra arguments")
        for name in cfg["modules"]:
            spec = tc.module(name, root)
            if action != "format":
                tc.setup(spec, env, root)
            if action != "setup":
                tc.run_commands(spec, action, env, root)


def dependencies(root: Path, args: list[str]):
    import chainman_updates

    return chainman_updates.run(root, args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(os.environ.get("CHAINMAN_ROOT", os.getcwd()))
    )
    parser.add_argument("action", nargs="?", default="doctor")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    root = args.root.absolute()
    try:
        # Resolve after rejecting indirection in existing project components.
        for part in [root, *root.parents]:
            if part.is_symlink():
                raise ValueError("Project root must not contain symlink components")
        root = root.resolve(strict=True)
        cfg = configuration(root)
        os.environ.update(CHAINMAN_ROOT=str(root), CHAINMAN_RUNTIME=str(RUNTIME))
        rest = args.arguments
        if args.action == "version":
            print((RUNTIME / "VERSION").read_text().strip())
        elif args.action in {"exec", "shell"}:
            name = cfg.get("project", {}).get("default_profile", "default")
            if rest[:1] == ["--profile"]:
                if len(rest) < 2:
                    raise ValueError("--profile requires a name")
                name, rest = rest[1], rest[2:]
            if rest[:1] == ["--"]:
                rest = rest[1:]
            if not rest:
                if args.action != "shell":
                    raise ValueError("exec requires a command")
                rest = ["bash"]
            with tc.operation(root) as outer_operation:
                if outer_operation and cfg.get("cache", {}).get(
                    "automatic_prune", True
                ):
                    tc.prune(root)
                env = tc.environment(root)
                if os.environ.get("CHAINMAN_COMPILER_OWNER") == str(root):
                    env["RUSTC_WRAPPER"] = os.environ.get("RUSTC_WRAPPER", "")
                with tc.compiler_cache(name, env, root) as owned:
                    return execute(root, name, rest, env=owned, check=False).returncode
        elif args.action == "deps-update":
            return dependencies(root, rest)
        elif args.action == "nix-update":
            if rest or os.environ.get("CHAINMAN_UPDATE_ACTIVE") != "1":
                raise ValueError("nix-update is a resolver hook inside deps-update")
            from datetime import datetime, timezone
            import updates

            updates.update_nix(
                root, cfg["updates"], datetime.now(timezone.utc), tc.environment(root)
            )
        elif args.action == "module":
            if len(rest) not in (1, 2):
                raise ValueError("module requires a name and optional action")
            spec = tc.module(rest[0], root)
            action = rest[1] if len(rest) == 2 else "verify"
            with tc.operation(root) as outer_operation:
                if outer_operation and cfg.get("cache", {}).get(
                    "automatic_prune", True
                ):
                    tc.prune(root)
                env = tc.environment(root)
                if action != "format":
                    tc.setup(spec, env, root)
                if action != "setup":
                    tc.run_commands(spec, action, env, root)
        elif args.action in {"clean", "cache-prune"}:
            if any(a != "--all" for a in rest):
                raise ValueError("cleanup accepts only --all")
            with tc.operation(root) as outer_operation:
                if not outer_operation:
                    raise ValueError(
                        "Cleanup cannot run inside an active managed operation"
                    )
                removed = tc.prune(
                    root, all_outputs=args.action == "clean" or "--all" in rest
                )
                print(json.dumps({"removed": removed}))
        elif args.action in {"ci-prune", "sdk-doctor"}:
            if args.action == "sdk-doctor" and rest not in (["apple"], ["android"]):
                raise ValueError("sdk-doctor requires apple or android")
            name = (
                ("swift" if rest == ["apple"] else "flutter")
                if args.action == "sdk-doctor"
                else "core"
            )
            script = (
                "native_sdks.py" if args.action == "sdk-doctor" else "ci_cleanup.py"
            )
            with tc.operation(root):
                return execute(
                    root,
                    name,
                    ["python3", str(RUNTIME / "scripts" / script), *rest],
                    env=tc.environment(root),
                    check=False,
                ).returncode
        elif args.action == "cache-status":
            work = tc.contained(root, ".cache/toolchain/work")
            downloads = Path(
                os.environ.get(
                    "TOOLCHAIN_DOWNLOAD_CACHE",
                    str(
                        Path(
                            os.environ.get(
                                "XDG_CACHE_HOME", str(Path.home() / ".cache")
                            )
                        )
                        / "nix-just-downloads"
                    ),
                )
            )
            print(
                json.dumps(
                    {
                        "build_bytes": tc.size(work, reporting=True),
                        "free_bytes": shutil.disk_usage(root).free,
                        "download_cache": str(downloads),
                        "download_bytes": tc.size(downloads, reporting=True),
                        "project": str(root),
                        "cache": cfg.get("cache", {}),
                    }
                )
            )
        elif args.action == "doctor":
            print(
                json.dumps(
                    {
                        "version": (RUNTIME / "VERSION").read_text().strip(),
                        "project": str(root),
                        "runtime": str(RUNTIME),
                        "mode": os.environ.get("CHAINMAN_MODE", "host-nix"),
                        "profiles": list(cfg.get("profiles", {})),
                        "modules": cfg["modules"],
                    },
                    indent=2,
                )
            )
        else:
            if args.action == "run":
                if not rest:
                    raise ValueError("run requires a project command name")
                action, rest = rest[0], rest[1:]
            else:
                action = args.action
            run_project(root, action, rest[1:] if rest[:1] == ["--"] else rest)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Chainman: {exc}", file=sys.stderr)
        return exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
