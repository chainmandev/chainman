"""Run an explicitly selected consumer with an immutable tooling runtime."""

from __future__ import annotations

import argparse
import fnmatch
from collections.abc import Mapping, Sequence
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Literal, Unpack, overload
from urllib.parse import parse_qs, quote, unquote, urlsplit

import toolchain as tc
from adapter_data import Table, table, strings, text as field_text

RUNTIME = Path(__file__).resolve().parents[1]


def configuration(root: Path) -> Table:
    return table(tc.config(root), "Project configuration")


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


def profile(
    root: Path, name: str, *, cfg: Mapping[str, object] | None = None
) -> tuple[str | None, Table]:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid profile name")
    cfg = configuration(root) if cfg is None else cfg
    raw = table(cfg.get("profiles", {}), "Profiles").get(name)
    if name == "host" and raw is None:
        raw = {}
    if raw is None:
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
        raw = {"runtime_profile": "core" if name == "default" else name}
    spec = table(raw, "Profile")
    import admission

    admission.declaration(spec)
    groups = strings(spec.get("entry_setup", []), "Profile entry_setup")
    if any(group not in table(cfg.get("setup", {}), "Setup") for group in groups):
        raise ValueError(f"Profile {name} entry_setup requires declared setup groups")
    for pattern in strings(spec.get("inputs", []), "Profile inputs"):
        tc.contained(root, pattern)
    if name == "host" or tc.host_mode():
        return None, spec
    if "flake" in spec:
        path, sep, attribute = field_text(spec["flake"], "Profile flake").partition("#")
        if not path or not sep or not re.fullmatch(r"[A-Za-z0-9_.-]+", attribute):
            raise ValueError("A project profile must select a flake path#shell")
        if path.endswith("/flake.nix") or path == "flake.nix":
            path = str(Path(path).parent)
        location = tc.contained(root, str(Path(path)))
        tc.regular_input(location, "flake.nix")
    else:
        location = RUNTIME / "nix"
        attribute = field_text(spec.get("runtime_profile", "core"), "Runtime profile")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", attribute):
            raise ValueError("Invalid runtime shell")
    return flake_reference(root, location, attribute), spec


def input_digests(
    root: Path, patterns: Sequence[str], exclusions: Sequence[str] = ()
) -> dict[str, str]:
    """Content inventory of declared regular files; additions/removals change it."""
    paths: set[Path] = set()
    for pattern in patterns:
        tc.contained(root, pattern)
        # Before Python 3.13, a terminal ** yields directories only. Inventory
        # files recursively on every supported interpreter, including 3.12.
        if pattern.endswith("**"):
            pattern += "/*"
        paths.update(root.glob(pattern))
    result = {}
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in exclusions):
            continue
        tc.contained(root, relative)
        if not path.is_dir():
            result[relative] = hashlib.sha256(
                tc.regular_input(root, relative)
            ).hexdigest()
    return result


def profile_inputs(root: Path, name: str, ref: str | None) -> dict[str, str]:
    import configuration_files

    spec = table(
        table(configuration(root).get("profiles", {}), "Profiles").get(name, {}),
        "Profile",
    )
    result = input_digests(root, ["chainman.lock"])
    result.update(
        {
            path: hashlib.sha256(body).hexdigest()
            for path, body in configuration_files.read(root).documents.items()
        }
    )
    # Bare host mode does not evaluate or depend on the declared Nix toolchain.
    if ref:
        result.update(
            input_digests(root, strings(spec.get("inputs", []), "Profile inputs"))
        )
        if ref.startswith("git+file:"):
            parsed = urlsplit(ref)
            directory = (
                Path(unquote(parsed.path)) / parse_qs(parsed.query).get("dir", [""])[0]
            )
        else:
            directory = Path(unquote(ref[5:].partition("#")[0]))
        for path, digest in input_digests(
            directory, ["flake.nix", "flake.lock"]
        ).items():
            label = (
                str((directory / path).relative_to(root))
                if directory.is_relative_to(root)
                else str(directory / path)
            )
            result[label] = digest
    return result


def profile_fingerprint(root: Path, name: str, ref: str | None) -> str:
    # Host exports have a fresh temporary path on every verified launch. Their
    # identity is the pin below, not that disposable materialization path.
    digest = hashlib.sha256(
        ("host:" + name if tc.host_mode() else str(RUNTIME)).encode()
    )
    digest.update((ref or "host").encode())
    digest.update(json.dumps(profile_inputs(root, name, ref), sort_keys=True).encode())
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


class ExecuteOptions(tc.ProcessIOOptions, total=False):
    capture_output: bool
    timeout: float | None


@overload
def execute(
    root: Path,
    name: str,
    argv: list[str],
    *,
    text: Literal[True],
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    resolved_defaults: Mapping[str, str] | None = None,
    overrides: object = None,
    check: bool = True,
    cwd: Path | None = None,
    gc_root: Path | None = None,
    **kwargs: Unpack[ExecuteOptions],
) -> subprocess.CompletedProcess[str]: ...


@overload
def execute(
    root: Path,
    name: str,
    argv: list[str],
    *,
    text: Literal[False] = False,
    input: bytes | None = None,
    env: Mapping[str, str] | None = None,
    resolved_defaults: Mapping[str, str] | None = None,
    overrides: object = None,
    check: bool = True,
    cwd: Path | None = None,
    gc_root: Path | None = None,
    **kwargs: Unpack[ExecuteOptions],
) -> subprocess.CompletedProcess[bytes]: ...


@overload
def execute(
    root: Path,
    name: str,
    argv: list[str],
    *,
    text: bool,
    input: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    resolved_defaults: Mapping[str, str] | None = None,
    overrides: object = None,
    check: bool = True,
    cwd: Path | None = None,
    gc_root: Path | None = None,
    **kwargs: Unpack[ExecuteOptions],
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]: ...


def execute(
    root: Path,
    name: str,
    argv: list[str],
    *,
    text: bool = False,
    input: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    resolved_defaults: Mapping[str, str] | None = None,
    overrides: object = None,
    check: bool = True,
    cwd: Path | None = None,
    gc_root: Path | None = None,
    **kwargs: Unpack[ExecuteOptions],
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("Commands must be nonempty argument arrays")
    cfg = configuration(root)
    import admission

    admission.profile(cfg, name, env=env)
    ref, spec = profile(root, name, cfg=cfg)
    selected = dict(os.environ if env is None else env)
    # Library callers can enter without the shell launcher. Capture their selected
    # Nix before a project flake refreshes PATH, just as bootstrap does.
    if not tc.host_mode() and not selected.get("CHAINMAN_RUNTIME_NIX_BIN"):
        executable = shutil.which(tc.nix_command(selected), path=selected.get("PATH"))
        if executable:
            selected["CHAINMAN_RUNTIME_NIX_BIN"] = str(
                Path(executable).resolve().parent
            )
    selected.update(
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(RUNTIME),
        CHAINMAN_RUNTIME_PYTHON=sys.executable,
        CHAINMAN_ACTIVE_PIN=(
            tc.regular_input(tc.configuration_root(root), "chainman.lock")
            .decode()
            .strip()
            if (tc.configuration_root(root) / "chainman.lock").exists()
            else ""
        ),
        TOOLCHAIN_MODE=selected.get("CHAINMAN_MODE", "host-nix"),
    )
    selected = profile_environment(root, spec, selected, overrides, cfg=cfg)
    # Internal literal defaults (e.g. a validated Python executable) apply after
    # project/profile expansion, without feeding effective values back into it.
    for key, value in (resolved_defaults or {}).items():
        selected.setdefault(key, value)
    if not tc.host_mode():
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
        return tc.managed_run(
            command,
            cwd=target,
            env=selected,
            check=check,
            text=text,
            input=input,
            **kwargs,
        )
    finally:
        if timing_operation is not None:
            timing.emit("command", "end", timing_operation)


def run_hook(
    root: Path,
    commands: object,
    *,
    name: str = "default",
    extra: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> None:
    if not isinstance(commands, list) or not commands:
        raise ValueError("A hook must declare at least one argument-array command")
    for index, command in enumerate(commands):
        if not isinstance(command, list) or not command:
            raise ValueError("Hook commands must be nonempty argument arrays")
        argv = [
            *strings(command, "Hook command"),
            *(extra if index == len(commands) - 1 else []),
        ]
        execute(root, name, argv, env=env)


def run_project(root: Path, action: str, extra: list[str]) -> int | None:
    cfg = configuration(root)
    if cfg["schema"] in (2, 3):
        import workflows

        return workflows.run(root, action, extra)
    with tc.operation(
        root,
        exclusive=action == "setup"
        or action not in table(cfg.get("commands", {}), "Project commands"),
        new_execution=True,
        automatic_prune=table(cfg.get("cache", {}), "Cache").get(
            "automatic_prune", True
        )
        is True,
    ):
        env = tc.environment(root)
        commands = table(cfg.get("commands", {}), "Project commands")
        if action in commands and action != "setup":
            name = field_text(
                table(cfg.get("command_profiles", {}), "Command profiles").get(
                    action,
                    table(cfg.get("project", {}), "Project").get(
                        "default_profile", "default"
                    ),
                ),
                "Command profile",
            )
            with tc.compiler_cache(name, env, root) as owned:
                run_hook(root, commands[action], name=name, extra=extra, env=owned)
            return None
        if extra:
            raise ValueError("Module actions do not accept extra arguments")
        specs = [tc.module(name, root) for name in strings(cfg["modules"], "Modules")]
        if action != "format":
            tc.setup_many(specs, env, root, explicit=action == "setup")
        if action != "setup":
            for spec in specs:
                tc.run_commands(spec, action, env, root)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Commands: exec [--profile NAME] -- COMMAND ..., shell [--profile NAME], "
            "run TASK ..., preflight TASK ..., setup [GROUP ...], setup-status, config validate, explain TASK or --profile NAME, "
            "config show --json, explain TASK, version, doctor. "
            "Use just chainman recipe NAME for project recipe bindings. "
            "Services and verified updates require a Nix execution mode."
        ),
    )
    parser.add_argument(
        "--root", type=Path, default=Path(os.environ.get("CHAINMAN_ROOT", os.getcwd()))
    )
    parser.add_argument("action", nargs="?", default="doctor")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    import timing

    timing.bootstrap()
    root = args.root.resolve()
    try:
        if tc.host_mode():
            import host_execution

            host_execution.validate_action(root, args.action)
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
        if args.action == "_display-prepare":
            import display_transport

            display_transport.prepare(
                Path("/chainman-x11-source"),
                Path("/chainman-x11-output/authority"),
                os.environ.get("DISPLAY", ""),
                os.environ.get("CHAINMAN_X11_HOSTNAME", ""),
            )
            return 0
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
        elif args.action == "_recipe-plan":
            import admission
            import recipes

            if len(rest) != 1:
                raise ValueError("recipe requires one standard recipe name")
            actions = recipes.actions(cfg).get(rest[0], [])
            admission.graph(
                root,
                cfg,
                [action[1] for action in actions if action[0] == "run"],
                groups=list(table(cfg.get("setup", {}), "Setup"))
                if any(action[0] == "setup" for action in actions)
                else [],
            )
            if tc.host_mode():
                host_execution.validate_recipe(root, rest[0])
            print(recipes.plan(cfg, rest[0]))
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
            import git_runtime

            revision = git_runtime.pin(tc.regular_input(root, "chainman.lock"))
            print(f"{(RUNTIME / 'VERSION').read_text().strip()} (Git {revision})")
        elif args.action in {"config", "explain"}:
            import config_inspection

            config_inspection.run(root, args.action, rest)
        elif args.action == "setup-status":
            import workflows

            return workflows.setup_status(root, rest)
        elif args.action == "_transport-prepare":
            import admission
            import workflows

            if len(rest) != 3:
                raise ValueError(
                    "Transport preparation requires action, task and profile"
                )
            action, task, selected_profile = rest
            groups = (
                admission.entry(root, cfg, selected_profile)
                if action in {"exec", "shell"}
                else admission.graph(root, cfg, [task if action == "run" else action])[
                    "setup"
                ]
            )
            with tc.operation(root, exclusive=False, new_execution=True):
                env = tc.environment(root)
                if action not in {"exec", "shell"}:
                    env = workflows.context_environment(
                        root, cfg, task if action == "run" else action, env
                    )
                with workflows.setup_use(root, cfg, groups, env):
                    return 0
        elif args.action == "preflight":
            import admission
            import workflows

            if not rest:
                raise ValueError("preflight requires at least one declared task")
            admission.graph(root, workflows.configuration(root), rest)
        elif args.action in {"exec", "shell"}:
            import admission
            import workflows

            reuse = rest[:1] == ["--reuse-operation"]
            if reuse:
                rest = rest[1:]
            name = field_text(
                table(cfg.get("project", {}), "Project").get(
                    "default_profile", "default"
                ),
                "Default profile",
            )
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
            if reuse:
                # Runtime-owned installers and compiler servers inherit the
                # parent's admission. They must not request public entry setup
                # while that parent holds a setup-use lease.
                descriptor, _, identity, compat, _ = tc.inherited_operation()
                lease = (
                    root / f".cache/toolchain/operations/{identity}"
                    if identity
                    else root / ".cache/toolchain/operation.lock"
                )
                if (
                    descriptor is None
                    or compat is None
                    or not tc.descriptor_matches(descriptor, lease)
                    or not tc.descriptor_matches(
                        compat, root / ".cache/toolchain/operation.lock"
                    )
                ):
                    raise ValueError(
                        "Internal entry requires this project's live operation"
                    )
                admission.profile(cfg, name)
                groups = []
            else:
                groups = admission.entry(root, cfg, name)
            with tc.operation(
                root,
                exclusive=False,
                new_execution=not reuse,
                automatic_prune=table(cfg.get("cache", {}), "Cache").get(
                    "automatic_prune", True
                )
                is True,
            ):
                env = tc.environment(root)
                if env.get("CHAINMAN_COMPILER_OWNER") == str(root):
                    env["RUSTC_WRAPPER"] = os.environ.get("RUSTC_WRAPPER", "")
                with workflows.setup_use(root, cfg, groups, env) as descriptors:
                    with tc.compiler_cache(name, env, root) as owned:
                        return execute(
                            root,
                            name,
                            rest,
                            env=owned,
                            pass_fds=descriptors,
                            check=False,
                        ).returncode
        elif args.action in {"deps-update", "chainman-update"}:
            raise ValueError(
                "Start updates through just chainman on the host; direct Python entry cannot orchestrate candidate services"
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
                selected, action = strings(cfg["modules"], "Modules"), rest[0]
            elif len(rest) not in (1, 2):
                raise ValueError("module requires a name and optional action")
            else:
                selected, action = [rest[0]], rest[1] if len(rest) == 2 else "verify"
            with tc.operation(
                root,
                exclusive=True,
                new_execution=True,
                automatic_prune=table(cfg.get("cache", {}), "Cache").get(
                    "automatic_prune", True
                )
                is True,
            ):
                env = tc.environment(root)
                specs = [tc.module(module_name, root) for module_name in selected]
                if action != "format":
                    tc.setup_many(specs, env, root, explicit=action == "setup")
                if action != "setup":
                    for spec in specs:
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
                        "build_bytes": tc.size(work, allow_external_links=True),
                        "free_bytes": shutil.disk_usage(root).free,
                        "download_cache": str(downloads),
                        "download_bytes": tc.size(downloads, allow_external_links=True),
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
                        "revision": tc.regular_input(root, "chainman.lock")
                        .decode()
                        .strip(),
                        "project": str(root),
                        "runtime": str(RUNTIME),
                        "mode": os.environ.get("CHAINMAN_MODE", "host-nix"),
                        "profiles": list(table(cfg.get("profiles", {}), "Profiles")),
                        "modules": cfg["modules"],
                        "configuration_schema": cfg["schema"],
                        "nix_policy": (
                            "caller-toolchain"
                            if tc.host_mode()
                            else "shared-container-daemon"
                            if os.environ.get("CHAINMAN_MODE") == "container-nix"
                            else "host-configuration"
                        ),
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
