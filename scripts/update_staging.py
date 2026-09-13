"""Fixed update phases, separated by host launcher calls and private snapshots.

Only preparation, inspection and finalization see the transaction directory.
Resolvers and verification receive the disposable checkout and an immutable
bootstrap. They cannot rewrite the snapshot authorizing changes to the original.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from collections.abc import Iterable, Iterator, Mapping
import json
import hashlib
import os
import stat
import tempfile
import sys
from pathlib import Path

import chainman
import chainman_updates as runtime_updates
import dependency_api
import toolchain as tc
import updates
import workflows
from transaction_state import Inspection, RuntimeMode, State
from adapter_data import strings, table


def directory(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError("Update staging requires a real absolute directory")
    return path


def export_bootstrap(runtime: Path, target: Path) -> None:
    target.mkdir(exist_ok=True)
    for source, name, mode in (
        ("chainman.sh", "chainman.sh", 0o700),
        ("fetch.nix", "chainman-fetch.nix", 0o600),
    ):
        tc.atomic_bytes(
            target / name, tc.regular_input(runtime, "bootstrap/" + source), mode
        )


def export_authority(
    root: Path,
    candidate: Path,
    target: Path,
    *,
    pin_root: Path | None = None,
    git_directories: Iterable[str] = (".git",),
) -> None:
    """Keep entry policy and runtime selection outside the writable candidate.

    The launcher mounts this directory read-only. It contains no transaction
    state or original Git metadata. Relative declarations still use candidate.
    """
    pin_root = pin_root or root
    git_directories = list(git_directories)
    if any(any(character in name for character in "\r\n,") for name in git_directories):
        raise ValueError(
            "Candidate Git directory paths must be single-line mount paths"
        )
    tc.atomic_bytes(target / "authority-root", (str(candidate) + "\n").encode())
    tc.atomic_bytes(target / "chainman.toml", tc.regular_input(root, "chainman.toml"))
    policy_file = tc.config(root).get("updates", {}).get("policy_file")
    if policy_file:
        tc.atomic_bytes(
            target / "dependency-policy.toml", tc.regular_input(root, policy_file)
        )
    tc.atomic_bytes(
        target / "git-directories", ("\n".join(git_directories) + "\n").encode()
    )
    pin = json.loads(tc.regular_input(pin_root, "chainman.lock"))
    if pin.get("bundled_archive"):
        tc.atomic_bytes(
            target / "runtime.tar.gz",
            tc.regular_input(pin_root, pin["bundled_archive"]),
        )
        pin["bundled_archive"] = "runtime.tar.gz"
    tc.atomic_json(target / "chainman.lock", pin)


def patterns(root: Path, policy: Mapping[str, object]) -> list[str]:
    result = [
        *strings(policy.get("outputs", []), "Update outputs"),
        *runtime_files(root),
    ]
    if not policy.get("resolver") and not policy.get("steps"):
        result += [
            p
            for name in tc.config(root)["modules"]
            for p in tc.module(name, root).get("update_outputs", [])
        ]
    updates.allowed([], result)
    return result


def runtime_files(root: Path) -> list[str]:
    import recipes

    if not (root / "chainman.toml").is_file():
        return []  # The source repository builds Chainman; it does not pin itself.
    return list(runtime_updates.managed_paths(root)) + [
        str((path / recipes.FILE).relative_to(root)) for path in recipes.roots(root)
    ]


def verification(root: Path, policy: Mapping[str, object]) -> list[str]:
    task = policy.get("verify_task")
    tasks = policy.get("verify_tasks")
    if (
        sum(
            policy.get(key) is not None
            for key in ("verify_task", "verify_tasks", "verify")
        )
        > 1
    ):
        raise ValueError(
            "Declare only one of updates.verify_task, verify_tasks or verify"
        )
    if task is not None or tasks is not None:
        selected_tasks = workflows.names([task] if task is not None else tasks)
        if not selected_tasks or len(selected_tasks) != len(set(selected_tasks)):
            raise ValueError("Update verification requires distinct finite tasks")
        cfg = workflows.configuration(root)
        tasks_by_name = workflows.declarations(cfg, "tasks")
        order = workflows.order(tasks_by_name, selected_tasks)
        if any(tasks_by_name[name].get("wait_for_services") for name in order):
            raise ValueError("Update verification must be a finite task")
        return [argument for task in selected_tasks for argument in ("run", task)]
    if tc.config(root)["schema"] in (2, 3) and not policy.get("verify"):
        raise ValueError("Schema 2 updates require updates.verify_task or verify_tasks")
    return ["_update-verify", "legacy"]


def index(root: Path) -> dict[str, tuple[str, str]]:
    return updates.staged_entries(root)


def unchanged(root: Path, state: State) -> None:
    if (
        updates.repository(root, clean=False) != state.identity
        or index(root) != state.index
        or updates.snapshot(root) != state.before
    ):
        raise ValueError(
            "Original checkout changed during the update; candidate and user changes are preserved"
        )


def prepare(
    root: Path, destination: Path, args: list[str], *, source: bool = False
) -> None:
    candidate = directory(destination / "candidate")
    control = directory(destination / "control")
    try:
        opts = runtime_updates.options(args)
        if source:
            if opts.only_chainman:
                raise ValueError(
                    "The Chainman source repository does not pin its own runtime"
                )
            opts = replace(opts, runtime=RuntimeMode.EXCLUDE)
    except SystemExit as result:
        if result.code == 0:
            tc.atomic_bytes(control / "help", b"")
        raise
    with tc.operation(root):
        identity = updates.repository(root, clean=not (opts.preview or opts.staged))
        before = updates.snapshot(root)
        policy = updates.settings(root) if source else dependency_api.policy(root)
        if opts.format and not source:
            import recipes

            cfg = tc.config(root)
            declared = recipes.bindings(cfg)
            if not declared.get("format-write"):
                raise ValueError("Declare recipes.format-write before running format")
            policy = dict(
                policy,
                outputs=["*"],
                verify_tasks=declared.get("format-check", [])
                + declared.get("format-hygiene", []),
            )
            policy.pop("verify_task", None)
            policy.pop("verify", None)
        if not policy:
            raise ValueError("Declare project updates and verification first")
        if not source and not opts.format and not opts.only_chainman:
            if policy.get("adapters") or policy.get("steps"):
                dependency_api.plan_steps(root, policy, opts.extra)
        if source and opts.format:
            policy = dict(policy, outputs=["*"])
        verify = verification(root, policy)
        original_index = index(root)
        output_patterns = (
            (runtime_files(root) + policy.get("reconcile_outputs", []))
            if opts.only_chainman
            else patterns(root, policy)
        )
        if opts.runtime is RuntimeMode.INCLUDE:
            output_patterns += policy.get("reconcile_outputs", [])
        selected = None
        if opts.staged:
            staged = set(
                updates.git(
                    root, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACM"
                ).split("\0")
            ) - {""}
            unstaged = set(updates.git(root, "diff", "--name-only", "-z").split("\0"))
            selected = sorted(staged - unstaged)
        with updates.preview_git_environment():
            if opts.preview or opts.staged:
                updates.prepare_preview(root, candidate, before)
            else:
                # Keep clean-source revision metadata meaningful to project
                # verifiers without copying history, remotes or executable hooks.
                updates.copy_submodule(root, candidate, identity[1])
                previous = updates.git(candidate, "symbolic-ref", "HEAD")
                updates.git(candidate, "update-ref", identity[0], identity[1])
                updates.git(candidate, "symbolic-ref", "HEAD", identity[0])
                if previous != identity[0]:
                    updates.git(candidate, "update-ref", "-d", previous)
            candidate_before = updates.snapshot(candidate)
            candidate_modes = {
                name: (candidate / name).stat().st_mode & 0o777
                for name in candidate_before
                if (candidate / name).is_file() and not (candidate / name).is_symlink()
            }
            candidate_git = {
                name: administration(candidate, name)
                for name in git_directories(candidate)
            }
            state = State(
                root=str(root),
                candidate=str(candidate),
                identity=identity,
                before=before,
                index=original_index,
                patterns=output_patterns,
                options=opts,
                runtime_files=runtime_files(root),
                verify=verify,
                at=datetime.now(timezone.utc),
                source=source,
                selected=selected,
                candidate_identity=updates.repository(candidate),
                candidate_before=candidate_before,
                candidate_index=index(candidate),
                candidate_modes=candidate_modes,
                candidate_git=candidate_git,
            )
        unchanged(root, state)
        if not source:
            export_bootstrap(chainman.RUNTIME, destination / "original-bootstrap")
            export_authority(
                root,
                candidate,
                destination / "original-bootstrap",
                git_directories=state.candidate_git,
            )
        tc.atomic_json(control / "state.json", state.encode())
        tc.atomic_bytes(control / "at", (state.at.isoformat() + "\n").encode())


def runtime_snapshot(candidate: Path, names: list[str]) -> dict[str, str]:
    result = {}
    for name in names:
        identity = updates.file_identity(tc.contained(candidate, name))
        if identity is not None:
            result[name] = identity
    return result


def prepare_runtime(root: Path, destination: Path) -> None:
    """Select the runtime before project resolution and retain its exact files."""
    state, candidate = read_state(root, destination)
    if state.source or state.options.skip_chainman:
        return
    with tc.operation(root):
        unchanged(root, state)
    with updates.preview_git_environment(), tc.operation(candidate):
        candidate_unchanged(candidate, state)
        if state.runtime_snapshot is not None:
            raise ValueError("Candidate runtime has already been prepared")
        if updates.snapshot(candidate) != state.candidate_before:
            raise ValueError("Candidate changed before runtime preparation")
        with tc.nix_temporary_directory("chainman-runtime-stage-") as directory:
            try:
                runtime = runtime_updates.runtime_candidate(
                    candidate,
                    dependency_api.policy(root),
                    state.at,
                    gc_root=Path(directory) / "runtime",
                )
            except (OSError, ValueError) as error:
                raise ValueError(
                    f"Chainman runtime preparation failed: {error}. "
                    "Use --skip-chainman for project-only dependency updates."
                ) from error
            launcher = destination / "resolution-bootstrap"
            export_bootstrap(runtime, launcher)
            export_authority(
                root,
                candidate,
                launcher,
                pin_root=candidate,
                git_directories=state.candidate_git,
            )
        state = replace(
            state, runtime_snapshot=runtime_snapshot(candidate, state.runtime_files)
        )
        tc.atomic_json(destination / "control/state.json", state.encode())


def resolve(root: Path, at: str, args: list[str]) -> None:
    opts = runtime_updates.options(args)
    with updates.preview_git_environment(), tc.operation(root):
        updates.repository(root, clean=opts.skip_chainman)
        if opts.format or opts.only_chainman:
            # Host orchestration runs the ordinary declared task lanes, including
            # service ownership. No project command executes in this phase.
            return
        runtime_updates.resolve_current(
            root, dependency_api.policy(root), datetime.fromisoformat(at), opts.extra
        )


def read_state(root: Path, destination: Path) -> tuple[State, Path]:
    state = State.decode(
        json.loads(tc.regular_input(directory(destination / "control"), "state.json"))
    )
    if state.root != str(root) or state.candidate != str(destination / "candidate"):
        raise ValueError("Update transaction identity changed")
    return state, directory(state.candidate)


def resume(root: Path, destination: Path) -> None:
    """Admit edited candidate sources without trusting previous verification."""
    state, candidate = read_state(root, destination)
    with tc.operation(root):
        unchanged(root, state)
    with updates.preview_git_environment(), tc.operation(candidate):
        candidate_unchanged(candidate, state)
        retry_runtime = (
            state.schema >= 2
            and not state.source
            and not state.options.skip_chainman
            and state.runtime_snapshot is None
        )
        if retry_runtime and updates.snapshot(candidate) != state.candidate_before:
            raise ValueError(
                "Runtime preparation did not complete; restore the retained candidate "
                "to its original contents before retrying runtime preparation"
            )
        if state.runtime_snapshot is not None:
            if (
                runtime_snapshot(candidate, state.runtime_files)
                != state.runtime_snapshot
            ):
                raise ValueError("Candidate runtime changed after preparation")
            with tc.nix_temporary_directory("chainman-runtime-resume-") as directory:
                runtime = verified_runtime(
                    candidate, gc_root=Path(directory) / "runtime"
                )
                launcher = destination / "resolution-bootstrap"
                export_bootstrap(runtime, launcher)
                export_authority(
                    root,
                    candidate,
                    launcher,
                    pin_root=candidate,
                    git_directories=state.candidate_git,
                )
    state = replace(state, at=datetime.now(timezone.utc), inspection=None)
    opts = state.options
    args = ["--message", opts.message]
    for enabled, flag in (
        (opts.format, "--format"),
        (opts.staged, "--staged"),
        (opts.only_chainman, "--only-chainman"),
        (opts.skip_chainman, "--skip-chainman"),
        (opts.runtime is RuntimeMode.INCLUDE, "--include-chainman"),
        (opts.preview, "--preview"),
        (opts.no_commit, "--no-commit"),
    ):
        if enabled:
            args.append(flag)
    if opts.extra:
        args += ["--", *opts.extra]
    if any("\n" in value or "\r" in value for value in args):
        raise ValueError("Resumed transaction arguments must be single-line values")
    control = destination / "control"
    tc.atomic_bytes(control / "resume-arguments", ("\n".join(args) + "\n").encode())
    tc.atomic_bytes(control / "retry-runtime", b"yes\n" if retry_runtime else b"no\n")
    tc.atomic_json(control / "state.json", state.encode())
    tc.atomic_bytes(control / "at", (state.at.isoformat() + "\n").encode())


def reaudit(root: Path, at: str, args: list[str]) -> None:
    """Reconstruct original dependency identities from the unchanged Git commit."""
    opts = runtime_updates.options(args)
    if opts.format:
        return
    now = datetime.fromisoformat(at)
    with (
        updates.preview_git_environment(),
        tempfile.TemporaryDirectory(prefix="chainman-reaudit-") as temporary,
    ):
        baseline = Path(temporary) / "original"
        updates.copy_submodule(
            root, baseline, updates.git(root, "rev-parse", "HEAD"), preserve_modes=False
        )
        declared = dependency_api.policy(baseline)
        legacy_modules = not declared.get("steps") and not declared.get("resolver")
        settings = dependency_api.inspection_policy(baseline)
        if not opts.skip_chainman and tc.regular_input(
            root, "chainman.lock"
        ) != tc.regular_input(baseline, "chainman.lock"):
            import registry

            pin = json.loads(tc.regular_input(root, "chainman.lock"))
            selected = registry.select(
                "github",
                [
                    release
                    for release in registry.github_releases("chainmandev/chainman")
                    if release.version.lstrip("v") == pin["version"].lstrip("v")
                ],
                settings,
                "chainmandev/chainman",
                now,
            )
            metadata, body, revision = runtime_updates.release_assets(
                selected, settings, now
            )
            metadata = table(metadata, "Runtime release metadata")
            if (
                any(
                    pin[key] != metadata[key]
                    for key in ("version", "revision", "url", "narHash")
                )
                or pin["revision"] != revision
            ):
                raise ValueError(
                    "Resumed runtime no longer matches its release evidence"
                )
            if (
                pin.get("bundled_archive")
                and tc.regular_input(root, pin["bundled_archive"]) != body
            ):
                raise ValueError(
                    "Resumed runtime archive differs from release evidence"
                )
        if opts.only_chainman:
            return
        if settings.get("resolver"):
            raise ValueError(
                "Re-audit requires declared adapters; an opaque resolver cannot certify an edited candidate"
            )
        _, _, adapters = dependency_api.plan_steps(baseline, settings, opts.extra)
        with dependency_api.transaction_environment(root, now):
            for spec, policy in adapters.values():
                adapter = dependency_api.implementation(spec)
                before = adapter.snapshot(baseline, spec)
                adapter.audit(root, spec, before, policy, now)
            if legacy_modules:
                import module_updates
                import source_updates

                image_before = module_updates.image_snapshot(baseline, settings)
                if image_before is not None:
                    expected = source_updates.select_oci(
                        image_before, {}, settings, now
                    )
                    actual = module_updates.image_snapshot(root, settings)
                    if actual is None or any(
                        actual[key] != expected[key] for key in actual
                    ):
                        raise ValueError(
                            "Resumed runtime image differs from fresh eligibility evidence"
                        )


def git_directories(root: Path, prefix: str = "") -> Iterator[str]:
    """Discover administration only while the newly copied input is trusted."""
    yield prefix + ".git"
    for name in updates.gitlinks(root):
        nested = tc.contained(root, name)
        if (nested / ".git").exists():
            yield from git_directories(nested, prefix + name + "/")


def administration(root: Path, name: str) -> str:
    """Read frozen candidate metadata as bytes, before invoking Git against it."""
    relative = Path(name)
    if relative.name != ".git":
        raise ValueError("Expected a frozen Git administrative directory")
    base = tc.contained(root, str(relative.parent)) / relative.name
    if not base.is_dir() or base.is_symlink():
        raise ValueError("Candidate Git administration must remain a real directory")
    digest = hashlib.sha256()
    for path in [base, *sorted(base.rglob("*"))]:
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError(
                "Candidate Git administration contains a non-regular entry"
            )
        digest.update(os.fsencode(str(path.relative_to(base))) + b"\0")
        digest.update(str(info.st_mode & 0o177777).encode() + b"\0")
        if stat.S_ISREG(info.st_mode):
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def candidate_unchanged(candidate: Path, state: State) -> None:
    if any(
        administration(candidate, name) != expected
        for name, expected in state.candidate_git.items()
    ):
        raise ValueError("Updater or verifier changed candidate Git administration")
    if (
        updates.repository(candidate, clean=False) != state.candidate_identity
        or index(candidate) != state.candidate_index
    ):
        raise ValueError("Updater or verifier changed candidate Git HEAD or index")


def verified_runtime(candidate: Path, *, gc_root: Path) -> Path:
    # Evaluate the old trusted fetch helper; never import candidate source merely
    # because the resolver left it in the checkout.
    runtime = runtime_updates.fetch_source(candidate, gc_root=gc_root)
    lock = json.loads(tc.regular_input(candidate, "chainman.lock"))
    actual = tc.managed_run(
        [
            tc.nix_command(),
            "--extra-experimental-features",
            "nix-command",
            "hash",
            "path",
            str(runtime),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if actual != lock["narHash"]:
        raise ValueError("Candidate runtime store source failed NAR verification")
    runtime_updates.validate_runtime(runtime, lock["version"])
    for source, target in (
        ("chainman.sh", "chainman.sh"),
        ("fetch.nix", "chainman-fetch.nix"),
    ):
        if tc.regular_input(candidate, "scripts/" + target) != tc.regular_input(
            runtime, "bootstrap/" + source
        ):
            raise ValueError("Candidate bootstrap differs from the verified runtime")
    for destination, source in runtime_updates.managed_paths(candidate).items():
        if runtime_updates.managed_state(
            candidate, destination
        ) != runtime_updates.managed_state(candidate, source):
            raise ValueError(f"Candidate runtime copy differs: {destination}")
    import recipes

    for consumer in recipes.roots(candidate):
        expected = tc.managed_run(
            [
                sys.executable,
                str(runtime / "scripts/recipes.py"),
                str(consumer),
                "--render",
            ],
            cwd=candidate,
            capture_output=True,
            check=True,
        ).stdout
        if tc.regular_input(consumer, recipes.FILE) != expected:
            raise ValueError(f"Candidate recipe facade differs: {consumer}")
    return runtime


def inspect(root: Path, destination: Path) -> None:
    state, candidate = read_state(root, destination)
    with tc.operation(root):
        unchanged(root, state)
    with updates.preview_git_environment(), tc.operation(candidate):
        candidate_unchanged(candidate, state)
        updated = updates.snapshot(candidate)
        paths = updates.changed(state.candidate_before, updated)
        if state.options.staged:
            if state.selected is None:
                raise ValueError("Staged update state lacks its selected paths")
            # Verification must see the exact effective working tree to be
            # applied, including the original bytes of every excluded path.
            updates.restore_paths(
                candidate,
                state.candidate_identity[1],
                [path for path in paths if path not in state.selected],
                state.candidate_modes,
            )
            updated = updates.snapshot(candidate)
            paths = updates.changed(state.candidate_before, updated)
        if set(paths) & (set(updates.gitlinks(candidate)) | {".gitmodules"}):
            raise ValueError(
                "Submodule inputs and metadata require a separate transaction"
            )
        updates.allowed(paths, state.patterns)
        if state.runtime_snapshot is not None:
            if (
                runtime_snapshot(candidate, state.runtime_files)
                != state.runtime_snapshot
            ):
                raise ValueError("Candidate runtime changed after preparation")
        elif not state.source and not state.options.skip_chainman and state.schema >= 2:
            raise ValueError("Candidate runtime has not been prepared")
        if state.options.skip_chainman and set(paths) & set(state.runtime_files):
            raise ValueError(
                "Dependency resolvers must not change the runtime; use chainman-update"
            )
        # Check all output kinds before starting potentially expensive verification.
        updates.expected_entries(candidate, state.candidate_identity[1], paths)
        if tc.config(candidate) != tc.config(root) or dependency_api.policy(
            candidate
        ) != dependency_api.policy(root):
            raise ValueError("Update must not change its workflow or dependency policy")
        if paths and not state.source:
            with tc.nix_temporary_directory("chainman-inspect-") as directory:
                runtime = verified_runtime(
                    candidate, gc_root=Path(directory) / "runtime"
                )
                export_bootstrap(runtime, destination / "candidate-bootstrap")
                export_authority(
                    root,
                    candidate,
                    destination / "candidate-bootstrap",
                    pin_root=candidate,
                    git_directories=state.candidate_git,
                )
        state = replace(state, inspection=Inspection(updated=updated, paths=paths))
        control = destination / "control"
        tc.atomic_json(control / "state.json", state.encode())
        tc.atomic_bytes(control / "changed", ("yes\n" if paths else "no\n").encode())
        tc.atomic_bytes(control / "verify", ("\n".join(state.verify) + "\n").encode())


def finalize(root: Path, destination: Path) -> None:
    state, candidate = read_state(root, destination)
    inspected = state.require_inspection()
    with updates.preview_git_environment(), tc.operation(candidate):
        candidate_unchanged(candidate, state)
        if updates.snapshot(candidate) != inspected.updated:
            raise ValueError(
                "Verification changed candidate sources; original checkout is untouched"
            )
        # Freeze bytes before writing anything in the original checkout.
        outputs = {}
        for name in inspected.paths:
            path = tc.contained(candidate, name)
            outputs[name] = (
                (tc.regular_input(candidate, name), path.stat().st_mode & 0o777)
                if path.exists()
                else None
            )
    with tc.operation(root):
        unchanged(root, state)
        opts = state.options
        commit = None
        if not opts.preview and inspected.paths:
            for name, output in outputs.items():
                target = tc.contained(root, name)
                # An editor can change a later destination after the initial
                # checkout check while earlier files are being applied.
                if updates.file_identity(target) != state.before.get(name):
                    raise ValueError(
                        f"Original output changed during application: {name}; "
                        "candidate and partial application are preserved"
                    )
                if output is None:
                    target.unlink(missing_ok=True)
                else:
                    tc.atomic_bytes(target, *output)
            expected = dict(state.before)
            for name in inspected.paths:
                if name in inspected.updated:
                    expected[name] = inspected.updated[name]
                else:
                    expected.pop(name, None)
            if updates.snapshot(root) != expected:
                raise ValueError(
                    "Original source changed while applying verified files; inspect preserved changes"
                )
            if not opts.no_commit:
                commit = updates.commit_verified(
                    root,
                    state.identity[0],
                    state.identity[1],
                    expected,
                    inspected.paths,
                    opts.message,
                )
            elif opts.staged:
                # Preserve partial/unrelated staging, but require selected entries
                # to contain exactly the verified raw bytes and executable modes.
                # Git clean filters and core.filemode can otherwise silently
                # turn a successful format check into an unverified staged tree.
                expected_index = {
                    name: value
                    for name, value in state.index.items()
                    if name not in inspected.paths
                }
                expected_index.update(
                    {
                        name: value
                        for name, value in updates.raw_entries(
                            root, inspected.paths
                        ).items()
                    }
                )
                updates.git(root, "add", "--", *inspected.paths)
                if index(root) != expected_index:
                    raise ValueError(
                        "The staged tree differs from verified bytes/modes or contains unrelated index changes; inspect Git filters and the preserved index"
                    )
                if (
                    updates.repository(root, clean=False) != state.identity
                    or updates.snapshot(root) != expected
                ):
                    raise ValueError(
                        "Original checkout changed during staged formatting; inspect the preserved changes"
                    )
        result = dict(
            schema=1,
            changed=inspected.paths,
            commit=commit,
            verification="passed" if inspected.paths else "no changes",
        )
        if opts.preview:
            result["preview"] = True
        print(json.dumps(result, indent=2))


def run(root: Path, action: str, args: list[str]) -> int:
    if action == "_update-reaudit" and args:
        reaudit(root, args[0], args[1:])
        return 0
    if action == "_update-tasks":
        opts = runtime_updates.options(args)
        cfg = tc.config(root)
        if opts.format:
            import recipes

            declared = recipes.bindings(cfg)
            tasks = (
                [] if opts.staged else declared.get("generate", [])
            ) + declared.get("format-write", [])
        else:
            tasks = dependency_api.policy(root).get("reconcile_tasks", [])
        workflows.names(tasks)
        for task in tasks:
            workflows.order(cfg.get("tasks", {}), [task])
            print(task)
        return 0
    if action == "_update-verify" and args == ["legacy"]:
        with updates.preview_git_environment():
            runtime_updates.verify_current(root)
        return 0
    if action == "_update-resolve" and args:
        resolve(root, args[0], args[1:])
        return 0
    if not args:
        raise ValueError("Missing update transaction directory")
    destination = directory(args[0])
    if action == "_update-prepare":
        prepare(root, destination, args[1:])
    elif action == "_update-runtime" and len(args) == 1:
        prepare_runtime(root, destination)
    elif action == "_update-resume" and len(args) == 1:
        resume(root, destination)
    elif action == "_update-inspect" and len(args) == 1:
        inspect(root, destination)
    elif action == "_update-finalize" and len(args) == 1:
        finalize(root, destination)
    else:
        raise ValueError("Invalid update phase")
    return 0
