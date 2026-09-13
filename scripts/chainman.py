"""Run an explicitly selected consumer with an immutable tooling runtime."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, quote, unquote, urlsplit

import toolchain as tc
from adapter_data import table, strings

RUNTIME = Path(__file__).resolve().parents[1]


def configuration(root: Path) -> dict:
    return tc.config(root)


def flake_reference(root: Path, location: Path, attribute: str) -> str:
    """Use an adopted Git source so package caches never enter the Nix store."""
    if location.is_relative_to(root):
        owner = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        if (
            owner.returncode == 0
            and Path(owner.stdout.strip()).resolve() == root.resolve()
        ):
            relative = location.relative_to(root)
            tracked = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "--literal-pathspecs",
                    "-C",
                    str(root),
                    "ls-files",
                    "--error-unmatch",
                    "--",
                    str(relative / "flake.nix"),
                ],
                capture_output=True,
            )
            if tracked.returncode == 0:
                directory = (
                    ""
                    if str(relative) == "."
                    else "?dir=" + quote(str(relative), safe="")
                )
                return f"git+file://{quote(str(root), safe='/')}{directory}#{attribute}"
    return f"path:{quote(str(location), safe='/')}#{attribute}"


def profile(root: Path, name: str, *, cfg=None) -> tuple[str | None, dict]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid profile name")
    cfg = configuration(root) if cfg is None else cfg
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
        if not path or not sep or not re.fullmatch(r"[A-Za-z0-9_.-]+", attribute):
            raise ValueError("A project profile must select a flake path#shell")
        if path.endswith("/flake.nix") or path == "flake.nix":
            path = str(Path(path).parent)
        location = tc.contained(root, str(Path(path)))
        tc.regular_input(location, "flake.nix")
    else:
        location = RUNTIME / "nix"
        attribute = spec.get("runtime_profile", "core")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", attribute):
            raise ValueError("Invalid runtime shell")
    return flake_reference(root, location, attribute), spec


def profile_fingerprint(root: Path, name: str, ref: str | None) -> str:
    digest = hashlib.sha256(str(RUNTIME).encode())
    digest.update((ref or "host").encode())
    for path in (root / "chainman.toml", root / "chainman.lock"):
        if path.exists():
            digest.update(tc.regular_input(root, path.name))
    if ref:
        if ref.startswith("git+file:"):
            parsed = urlsplit(ref)
            directory = (
                Path(unquote(parsed.path)) / parse_qs(parsed.query).get("dir", [""])[0]
            )
        else:
            directory = Path(unquote(ref[5:].partition("#")[0]))
        for name in ("flake.nix", "flake.lock"):
            if (directory / name).exists():
                digest.update(tc.regular_input(directory, name))
    return digest.hexdigest()


def profile_environment(
    root: Path,
    spec: Mapping[str, object],
    inherited: Mapping[str, str],
    overrides: object = None,
    *,
    cfg: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Resolve declared environment identically for execution and input hashing."""
    import project_environment

    selected = dict(inherited)
    cfg = configuration(root) if cfg is None else cfg
    environment = table(cfg.get("environment", {}), "Project environment")
    selected = project_environment.apply(root, environment, selected)
    for values in (spec.get("environment", {}), overrides or {}):
        expanded = project_environment.expand(values, root, selected)
        selected.update(expanded)
        tc.pnpm_environment(selected, expanded)
    for key in strings(environment.get("unset", []), "Environment unset entries"):
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
    return selected


def execute(
    root: Path,
    name: str,
    argv: list[str],
    *,
    env=None,
    overrides=None,
    check=True,
    cwd: Path | None = None,
    gc_root: Path | None = None,
    **kwargs,
):
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("Commands must be nonempty argument arrays")
    cfg = configuration(root)
    ref, spec = profile(root, name, cfg=cfg)
    selected = dict(os.environ if env is None else env)
    # Library callers can enter without the shell launcher. Capture their selected
    # Nix before a project flake refreshes PATH, just as bootstrap does.
    if not selected.get("CHAINMAN_RUNTIME_NIX_BIN"):
        executable = shutil.which(tc.nix_command(selected), path=selected.get("PATH"))
        if executable:
            selected["CHAINMAN_RUNTIME_NIX_BIN"] = str(
                Path(executable).resolve().parent
            )
    selected.update(
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(RUNTIME),
        TOOLCHAIN_MODE=selected.get("CHAINMAN_MODE", "host-nix"),
    )
    selected = profile_environment(root, spec, selected, overrides, cfg=cfg)
    tc.runtime_nix_environment(selected)
    resource_policy = {}
    for settings in (
        cfg.get("resources", {}),
        spec.get("resources", {}),
    ):
        if not isinstance(settings, dict):
            raise ValueError("Resource policies must be tables")
        resource_policy.update(settings)
    if resource_policy:
        import resources

        resources.apply(resource_policy, selected)
    token = profile_fingerprint(root, name, ref)
    active = (
        selected.get("CHAINMAN_ACTIVE_PROFILE") == name
        and selected.get("CHAINMAN_ACTIVE_FINGERPRINT") == token
    )
    target = root if cwd is None else cwd
    # Explicit project/profile TMPDIR settings supersede the inherited base.
    # Restore it after external flakes as well as the bundled shell hook.
    if selected.get("TMPDIR"):
        selected["CHAINMAN_TEMP_BASE"] = selected["TMPDIR"]
    else:
        selected.pop("CHAINMAN_TEMP_BASE", None)
    import timing

    timed = timing.enabled(selected)
    timing_operation = None
    if timed:
        import uuid

        timing_operation = uuid.uuid4().hex
        timing.emit("profile_entry", "start", timing_operation)
        argv = [
            sys.executable,
            str(RUNTIME / "scripts/timing.py"),
            timing_operation,
            *argv,
        ]
    command = argv
    if ref and (
        gc_root is not None or not active or selected.get("TOOLCHAIN_FRESH") == "1"
    ):
        if selected.get("TOOLCHAIN_CONTAINER") == "1":
            # Nix's own temporary profile must be visible to its daemon. The
            # command below restores the application's selected temporary base.
            selected["TMPDIR"] = "/nix/tmp"
        command = [
            tc.nix_command(selected),
            "--extra-experimental-features",
            "nix-command flakes",
            "develop",
            ref,
            "--no-write-lock-file",
            *(["--profile", str(gc_root)] if gc_root is not None else []),
            "--command",
            "sh",
            "-eu",
            "-c",
            'if [ -n "${CHAINMAN_TEMP_BASE:-}" ]; then export TMPDIR="$CHAINMAN_TEMP_BASE"; '
            'elif [ -n "${TMPDIR:-}" ]; then export CHAINMAN_TEMP_BASE="$TMPDIR"; fi; '
            'if [ "${TOOLCHAIN_CONTAINER:-}" = 1 ]; then '
            "unset NIX_STATE_DIR NIX_STORE_DIR NIX_DAEMON_SOCKET_PATH; export NIX_REMOTE=daemon; "
            "export NIX_CONFIG='build-users-group =\nstore = daemon'; fi; "
            "runtime_nix=$1; shift; "
            'if [ -n "$runtime_nix" ]; then export CHAINMAN_RUNTIME_NIX_BIN="$runtime_nix" PATH="$runtime_nix:$PATH"; fi; '
            'cd "$1"; shift; exec "$@"',
            "sh",
            selected.get("CHAINMAN_RUNTIME_NIX_BIN", ""),
            str(target),
            *argv,
        ]
    selected.update(CHAINMAN_ACTIVE_PROFILE=name, CHAINMAN_ACTIVE_FINGERPRINT=token)
    selected.pop("TOOLCHAIN_FRESH", None)
    try:
        return tc.managed_run(command, cwd=target, env=selected, check=check, **kwargs)
    finally:
        if timing_operation is not None:
            timing.emit("command", "end", timing_operation)


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
    if cfg["schema"] in (2, 3):
        import workflows

        return workflows.run(root, action, extra)
    with tc.operation(
        root,
        exclusive=action == "setup" or action not in cfg.get("commands", {}),
        new_execution=True,
        automatic_prune=cfg.get("cache", {}).get("automatic_prune", True),
    ):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(os.environ.get("CHAINMAN_ROOT", os.getcwd()))
    )
    parser.add_argument("action", nargs="?", default="doctor")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    import timing

    timing.bootstrap()
    root = args.root.absolute()
    try:
        if args.action in {
            "deps-update",
            "chainman-update",
            "deps-query",
            "deps-resolve",
            "deps-check",
            "deps-coverage",
            "deps-policy-report",
            "deps-audit",
            "nix-update",
            "_update-prepare",
            "_update-runtime",
            "_update-resolve",
            "_update-inspect",
            "_update-verify",
            "_update-finalize",
            "_update-tasks",
            "_update-resume",
            "_update-reaudit",
        }:
            if any(
                importlib.util.find_spec(name) is None
                for name in ("tomlkit", "packaging", "yaml", "semantic_version")
            ):
                # Ordinary execution needs only the standard library. Resolve
                # updater libraries lazily in this same immutable runtime.
                return tc.managed_run(
                    [
                        tc.nix_command(),
                        "--extra-experimental-features",
                        "nix-command flakes",
                        "develop",
                        f"path:{quote(str(RUNTIME / 'nix'), safe='/')}#updates",
                        "--no-write-lock-file",
                        "--command",
                        "python3",
                        str(RUNTIME / "scripts/chainman.py"),
                        "--root",
                        str(root),
                        args.action,
                        *args.arguments,
                    ],
                    check=False,
                ).returncode
        # Resolve after rejecting indirection in existing project components.
        for part in [root, *root.parents]:
            if part.is_symlink():
                raise ValueError("Project root must not contain symlink components")
        root = root.resolve(strict=True)
        if args.action.startswith("_update-"):
            import update_staging

            return update_staging.run(root, args.action, args.arguments)
        if args.action == "_control-export":
            import services

            return services.export(root, args.arguments)
        if args.action == "_bootstrap-options":
            import bootstrap_plan

            if len(args.arguments) != 2:
                raise ValueError(
                    "Bootstrap options require the original action and task"
                )
            _, options = bootstrap_plan.plan(root, args.arguments[0], args.arguments[1])
            if options:
                print("\n".join(map(bootstrap_plan.line, options)))
            return 0
        cfg = configuration(root)
        os.environ.update(CHAINMAN_ROOT=str(root), CHAINMAN_RUNTIME=str(RUNTIME))
        rest = args.arguments
        if args.action == "_service-prepare":
            import services

            return services.prepare_requested(root, rest)
        elif args.action == "_format-plan":
            import recipes

            declared = recipes.bindings(cfg)
            if rest or not declared.get("format-write"):
                raise ValueError("Declare recipes.format-write")
            for task in (
                declared.get("generate", [])
                + declared["format-write"]
                + declared.get("format-check", [])
                + declared.get("format-hygiene", [])
            ):
                print(task)
        elif args.action == "_recipe-required":
            raise ValueError(f"Configure the required recipe: {' '.join(rest)}")
        elif args.action in {
            "_workflow-task",
            "_workflow-service",
            "_workflow-probe",
            "_workflow-prepare",
        }:
            import services

            return services.execute_internal(root, args.action, rest)
        elif args.action == "version":
            print((RUNTIME / "VERSION").read_text().strip())
        elif args.action in {"config", "explain"}:
            import config_inspection

            config_inspection.run(root, args.action, rest)
        elif args.action == "setup-status":
            import workflows

            return workflows.setup_status(root, rest)
        elif args.action in {"exec", "shell"}:
            reuse = rest[:1] == ["--reuse-operation"]
            if reuse:
                rest = rest[1:]
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
            with tc.operation(
                root,
                exclusive=False,
                new_execution=not reuse,
                automatic_prune=cfg.get("cache", {}).get("automatic_prune", True),
            ):
                env = tc.environment(root)
                if env.get("CHAINMAN_COMPILER_OWNER") == str(root):
                    env["RUSTC_WRAPPER"] = os.environ.get("RUSTC_WRAPPER", "")
                with tc.compiler_cache(name, env, root) as owned:
                    return execute(root, name, rest, env=owned, check=False).returncode
        elif args.action in {"deps-update", "chainman-update"}:
            raise ValueError(
                "Start updates through scripts/chainman.sh on the host; direct Python entry cannot orchestrate candidate services"
            )
        elif args.action == "deps-check":
            import dependency_api
            import recipes

            settings = dependency_api.inspection_policy(root)
            rest = recipes.selection_options(rest)
            names, _, adapters = dependency_api.plan_steps(root, settings, rest)
            print(
                json.dumps(
                    {"schema": 1, "targets": sorted(names), "adapters": list(adapters)}
                )
            )
        elif args.action in {"deps-query", "deps-resolve"}:
            import dependency_api

            result = (
                dependency_api.query_command(root, rest)
                if args.action == "deps-query"
                else dependency_api.resolve_command(root, rest)
            )
            print(json.dumps(result, sort_keys=True))
        elif args.action in {"deps-coverage", "deps-policy-report"}:
            import dependency_reports

            return dependency_reports.run(root, args.action, rest)
        elif args.action == "deps-audit":
            import dependency_audit

            return dependency_audit.run(root, rest)
        elif args.action == "nix-update":
            if rest or os.environ.get("CHAINMAN_UPDATE_ACTIVE") != "1":
                raise ValueError("nix-update is a resolver hook inside deps-update")
            import dependency_api
            import module_updates
            import source_updates

            policy, now = dependency_api.policy(root), dependency_api.instant()
            spec = module_updates.nix_spec(policy)
            if spec is not None:
                before = source_updates.snapshot(root, spec)
                source_updates.resolve(root, spec, policy, now)
                source_updates.audit(root, spec, before, policy, now)
        elif args.action in {"module", "modules"}:
            if args.action == "modules":
                if len(rest) != 1 or rest[0] not in {
                    "setup",
                    "build",
                    "test",
                    "verify",
                    "format",
                }:
                    raise ValueError(
                        "modules requires setup, build, test, verify or format"
                    )
                selected, action = cfg["modules"], rest[0]
            elif len(rest) not in (1, 2):
                raise ValueError("module requires a name and optional action")
            else:
                selected, action = [rest[0]], rest[1] if len(rest) == 2 else "verify"
            with tc.operation(
                root,
                exclusive=True,
                new_execution=True,
                automatic_prune=cfg.get("cache", {}).get("automatic_prune", True),
            ):
                env = tc.environment(root)
                for module_name in selected:
                    spec = tc.module(module_name, root)
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
                        "configuration_schema": cfg["schema"],
                        "nix_policy": "shared-container-daemon"
                        if os.environ.get("CHAINMAN_MODE") == "container-nix"
                        else "host-configuration",
                        "inspect": {
                            "configuration": ["config", "show", "--json"],
                            "setup": ["setup-status"],
                            "task": ["explain", "TASK", "--json"],
                        },
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
