"""Fixed update phases, separated by host launcher calls and private snapshots.

Only preparation, inspection and finalization see the transaction directory.
Resolvers and verification receive the disposable checkout and an immutable
bootstrap. They cannot rewrite the snapshot authorizing changes to the original.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

import chainman
import chainman_updates as runtime_updates
import dependency_api
import toolchain as tc
import updates
import workflows


def directory(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError("Update staging requires a real absolute directory")
    return path


def export_bootstrap(runtime, target):
    target.mkdir(exist_ok=True)
    for source, name, mode in (
        ("chainman.sh", "chainman.sh", 0o700),
        ("fetch.nix", "chainman-fetch.nix", 0o600),
    ):
        tc.atomic_bytes(
            target / name, tc.regular_input(runtime, "bootstrap/" + source), mode
        )


def patterns(root, policy):
    result = [
        *policy.get("outputs", []),
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


def runtime_files(root):
    return list(runtime_updates.managed_paths(root))


def verification(root, policy):
    task = policy.get("verify_task")
    if task is not None:
        if policy.get("verify"):
            raise ValueError("Declare updates.verify_task or updates.verify, not both")
        workflows.name(task)
        cfg = workflows.configuration(root)
        order = workflows.order(cfg.get("tasks", {}), [task])
        if any(cfg["tasks"][name].get("wait_for_services") for name in order):
            raise ValueError("Update verification must be a finite task")
        return ["run", task]
    if tc.config(root)["schema"] == 2 and not policy.get("verify"):
        raise ValueError("Schema 2 updates require updates.verify_task")
    return ["_update-verify", "legacy"]


def index(root):
    # JSON retains lists, so normalize Git's mode/OID tuples before comparison.
    return {name: list(value) for name, value in updates.staged_entries(root).items()}


def unchanged(root, state):
    if (
        list(updates.repository(root, clean=False)) != state["identity"]
        or index(root) != state["index"]
        or updates.snapshot(root) != state["before"]
    ):
        raise ValueError(
            "Original checkout changed during the update; candidate and user changes are preserved"
        )


def prepare(root, destination, args):
    candidate = directory(destination / "candidate")
    control = directory(destination / "control")
    try:
        opts = runtime_updates.options(args)
    except SystemExit as result:
        if result.code == 0:
            tc.atomic_bytes(control / "help", b"")
        raise
    with tc.operation(root):
        identity = updates.repository(root, clean=not opts.preview)
        before = updates.snapshot(root)
        policy = dependency_api.policy(root)
        if not policy:
            raise ValueError("Declare project updates and verification first")
        verify = verification(root, policy)
        state = dict(
            schema=1,
            root=str(root),
            candidate=str(candidate),
            identity=list(identity),
            before=before,
            index=index(root),
            patterns=patterns(root, policy),
            options=vars(opts),
            runtime_files=runtime_files(root),
            verify=verify,
            at=datetime.now(timezone.utc).isoformat(),
        )
        with updates.preview_git_environment():
            if opts.preview:
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
            state["candidate_identity"] = list(updates.repository(candidate))
            state["candidate_before"] = updates.snapshot(candidate)
            state["candidate_index"] = index(candidate)
        unchanged(root, state)
        export_bootstrap(chainman.RUNTIME, destination / "original-bootstrap")
        tc.atomic_json(control / "state.json", state)
        tc.atomic_bytes(control / "at", (state["at"] + "\n").encode())


def resolve(root, at, args):
    opts = runtime_updates.options(args)
    with updates.preview_git_environment(), tc.operation(root):
        updates.repository(root)
        runtime_updates.perform(
            root,
            dependency_api.policy(root),
            datetime.fromisoformat(at),
            opts.extra,
            only_runtime=opts.only_chainman,
            skip_runtime=opts.skip_chainman,
        )


def read_state(root, destination):
    state = json.loads(
        tc.regular_input(directory(destination / "control"), "state.json")
    )
    if (
        state.get("schema") != 1
        or state["root"] != str(root)
        or state["candidate"] != str(destination / "candidate")
    ):
        raise ValueError("Update transaction identity changed")
    return state, directory(state["candidate"])


def candidate_unchanged(candidate, state):
    if (
        list(updates.repository(candidate, clean=False)) != state["candidate_identity"]
        or index(candidate) != state["candidate_index"]
    ):
        raise ValueError("Updater or verifier changed candidate Git HEAD or index")


def verified_runtime(candidate):
    # Evaluate the old trusted fetch helper; never import candidate source merely
    # because the resolver left it in the checkout.
    env = dict(
        os.environ,
        CHAINMAN_BOOTSTRAP_HELPER=str(chainman.RUNTIME / "bootstrap/fetch.nix"),
        CHAINMAN_PROJECT_ROOT=str(candidate),
    )
    expression = 'import (builtins.toPath (builtins.getEnv "CHAINMAN_BOOTSTRAP_HELPER")) { root = builtins.getEnv "CHAINMAN_PROJECT_ROOT"; action = "fetch"; archive = ""; }'
    runtime = Path(
        tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "eval",
                "--impure",
                "--raw",
                "--expr",
                expression,
            ],
            cwd=candidate,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    if runtime.parent != Path("/nix/store"):
        raise ValueError("Candidate runtime must be a verified Nix store tree")
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
    return runtime


def inspect(root, destination):
    state, candidate = read_state(root, destination)
    with tc.operation(root):
        unchanged(root, state)
    with updates.preview_git_environment(), tc.operation(candidate):
        candidate_unchanged(candidate, state)
        updated = updates.snapshot(candidate)
        paths = updates.changed(state["candidate_before"], updated)
        if set(paths) & (set(updates.gitlinks(candidate)) | {".gitmodules"}):
            raise ValueError(
                "Submodule inputs and metadata require a separate transaction"
            )
        updates.allowed(paths, state["patterns"])
        if state["options"]["only_chainman"]:
            if set(paths) - set(state["runtime_files"]):
                raise ValueError(
                    "Runtime updates may change only managed runtime files"
                )
        elif set(paths) & set(state["runtime_files"]):
            raise ValueError(
                "Dependency resolvers must not change the runtime; use chainman-update"
            )
        # Check all output kinds before starting potentially expensive verification.
        updates.expected_entries(candidate, state["candidate_identity"][1], paths)
        if paths:
            runtime = verified_runtime(candidate)
            export_bootstrap(runtime, destination / "candidate-bootstrap")
        state.update(updated=updated, paths=paths)
        control = destination / "control"
        tc.atomic_json(control / "state.json", state)
        tc.atomic_bytes(control / "changed", ("yes\n" if paths else "no\n").encode())
        tc.atomic_bytes(
            control / "verify", ("\n".join(state["verify"]) + "\n").encode()
        )


def finalize(root, destination):
    state, candidate = read_state(root, destination)
    with updates.preview_git_environment(), tc.operation(candidate):
        candidate_unchanged(candidate, state)
        if updates.snapshot(candidate) != state["updated"]:
            raise ValueError(
                "Verification changed candidate sources; original checkout is untouched"
            )
        # Freeze bytes before writing anything in the original checkout.
        outputs = {}
        for name in state["paths"]:
            path = tc.contained(candidate, name)
            outputs[name] = (
                (tc.regular_input(candidate, name), path.stat().st_mode & 0o777)
                if path.exists()
                else None
            )
    with tc.operation(root):
        unchanged(root, state)
        opts = state["options"]
        commit = None
        if not opts["preview"] and state["paths"]:
            for name, output in outputs.items():
                target = tc.contained(root, name)
                if output is None:
                    target.unlink(missing_ok=True)
                else:
                    tc.atomic_bytes(target, *output)
            expected = dict(state["before"])
            for name in state["paths"]:
                if name in state["updated"]:
                    expected[name] = state["updated"][name]
                else:
                    expected.pop(name, None)
            if updates.snapshot(root) != expected:
                raise ValueError(
                    "Original source changed while applying verified files; inspect preserved changes"
                )
            if not opts["no_commit"]:
                commit = updates.commit_verified(
                    root, *state["identity"], expected, state["paths"], opts["message"]
                )
        result = dict(
            schema=1,
            changed=state["paths"],
            commit=commit,
            verification="passed" if state["paths"] else "no changes",
        )
        if opts["preview"]:
            result["preview"] = True
        print(json.dumps(result, indent=2))


def run(root, action, args):
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
    elif action == "_update-inspect" and len(args) == 1:
        inspect(root, destination)
    elif action == "_update-finalize" and len(args) == 1:
        finalize(root, destination)
    else:
        raise ValueError("Invalid update phase")
    return 0
