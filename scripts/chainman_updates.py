"""Consumer hooks and runtime updates in one verified Git transaction."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import chainman
import registry
import source_updates
import toolchain as tc
import updates


def managed_state(root: Path, name: str) -> tuple[bytes, int] | None:
    path = tc.contained(root, name)
    if not path.exists():
        return None
    return tc.regular_input(root, name), stat.S_IMODE(path.stat().st_mode)


class ManagedFiles:
    """Restore only our still-identical managed outputs after a failed candidate."""

    def __init__(self, root: Path):
        self.root = root
        self.written = {}

    def publish(self, name: str, before, after):
        if managed_state(self.root, name) != before:
            raise ValueError(
                f"Managed runtime input changed during preparation: {name}"
            )
        # Record before attempting the replacement: a failure can occur after
        # the atomic replacement, and recovery must still recognize its bytes.
        self.written[name] = (before, after)
        tc.atomic_bytes(tc.contained(self.root, name), after[0], after[1])

    def restore(self):
        preserved = []
        for name, (before, candidate) in reversed(list(self.written.items())):
            try:
                current = managed_state(self.root, name)
                if current != candidate:
                    if current != before:
                        preserved.append(name)
                    continue
                path = tc.contained(self.root, name)
                if before is None:
                    path.unlink()
                else:
                    tc.atomic_bytes(path, before[0], before[1])
            except (OSError, ValueError):
                preserved.append(name)
        self.written.clear()
        if preserved:
            print(
                "Chainman: concurrent or inaccessible managed changes were preserved: "
                + ", ".join(preserved),
                file=sys.stderr,
            )


def fetch_runtime(candidate: dict, body: bytes) -> Path:
    # Fetch exactly the downloaded bytes, before changing the live consumer pin.
    # The trusted old helper validates their unpacked NAR hash; no candidate
    # source is imported or executed while this temporary lock is evaluated.
    with tempfile.TemporaryDirectory(prefix="chainman-candidate-") as directory:
        staged = Path(directory)
        (staged / "archive.tar.gz").write_bytes(body)
        (staged / "chainman.lock").write_text(
            json.dumps({**candidate, "bundled_archive": "archive.tar.gz"})
        )
        fetch_env = dict(
            os.environ,
            CHAINMAN_PROJECT_ROOT=str(staged),
            CHAINMAN_BOOTSTRAP_HELPER=str(chainman.RUNTIME / "bootstrap/fetch.nix"),
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
                text=True,
                cwd=staged,
                env=fetch_env,
                capture_output=True,
                check=True,
            ).stdout.strip()
        )
    if (
        runtime.parent != Path("/nix/store")
        or runtime.is_symlink()
        or not runtime.is_dir()
    ):
        raise ValueError("Runtime fetch did not return a real Nix store tree")
    return runtime


def validate_runtime(runtime: Path, version: str):
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("Candidate runtime must be a real directory")

    def unreadable(error):
        raise ValueError("Candidate runtime tree could not be inspected") from error

    for directory, folders, files in os.walk(
        runtime, followlinks=False, onerror=unreadable
    ):
        for name in folders + files:
            kind = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISREG(kind) or stat.S_ISDIR(kind)):
                raise ValueError(
                    "Candidate runtime must contain only regular files and directories"
                )
    if not (runtime / "tests").is_dir():
        raise ValueError("Candidate runtime is missing its test directory")
    for name in (
        "VERSION",
        "bootstrap/chainman.sh",
        "bootstrap/fetch.nix",
        "nix/flake.nix",
        "nix/flake.lock",
        "scripts/chainman.py",
        "scripts/chainman_updates.py",
    ):
        path = runtime / name
        if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(
                f"Candidate runtime is missing a required regular file: {name}"
            )
    actual = (runtime / "VERSION").read_text().strip()
    if registry.version("github", actual) is None or registry.version(
        "github", actual
    ) != registry.version("github", version):
        raise ValueError("Candidate VERSION does not match release metadata")


def release_assets(selected: registry.Release, policy: dict, now: datetime):
    """Bind release maturity to the exact server-dated assets before execution."""
    repository = "chainmandev/chainman"
    api = f"https://api.github.com/repos/{repository}"
    release = registry.data(f"{api}/releases/tags/{quote(selected.version, safe='')}")
    if (
        not isinstance(release, dict)
        or release.get("tag_name") != selected.version
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or registry.timestamp(release.get("published_at")) != selected.published
        or not isinstance(release.get("assets"), list)
    ):
        raise ValueError("Runtime release changed or lacks publication evidence")
    revision = registry.github_commit(repository, selected.version)
    published = max(
        selected.published, source_updates.commit_time(repository, revision)
    )
    names = ("chainman-release.json", f"chainman-{selected.version.lstrip('v')}.tar.gz")
    assets = {}
    for name in names:
        matches = [
            item
            for item in release["assets"]
            if isinstance(item, dict) and item.get("name") == name
        ]
        if len(matches) != 1:
            raise ValueError("Runtime release lacks one exact required asset")
        item = matches[0]
        if (
            type(item.get("id")) is not int
            or item["id"] <= 0
            or item.get("state") != "uploaded"
            or type(item.get("size")) is not int
            or item["size"] <= 0
            or not isinstance(item.get("digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"])
        ):
            raise ValueError("Runtime asset lacks immutable checksum evidence")
        published = max(
            published,
            registry.timestamp(item.get("created_at")),
            registry.timestamp(item.get("updated_at")),
        )
        assets[name] = item
    registry.eligible(
        "github",
        [registry.Release(selected.version, published)],
        policy,
        repository,
        now,
    )
    bodies = []
    for name in names:
        item = assets[name]
        body = registry.fetch(
            f"{api}/releases/assets/{item['id']}", accept="application/octet-stream"
        )[0]
        if (
            len(body) != item["size"]
            or "sha256:" + hashlib.sha256(body).hexdigest() != item["digest"]
        ):
            raise ValueError("Runtime asset bytes differ from dated release identity")
        bodies.append(body)
    if registry.github_commit(repository, selected.version) != revision:
        raise ValueError("Runtime release tag changed during download")
    return json.loads(bodies[0]), bodies[1], revision


def runtime_candidate(
    root: Path, policy: dict, now: datetime, managed: ManagedFiles | None = None
) -> Path:
    lockpath = tc.contained(root, "chainman.lock")
    if not lockpath.exists():
        return chainman.RUNTIME
    lock_before = managed_state(root, "chainman.lock")
    old = json.loads(lock_before[0])
    inputs = {
        name: managed_state(root, name)
        for name in ("scripts/chainman.sh", "scripts/chainman-fetch.nix")
    }
    if old.get("bundled_archive"):
        name = old["bundled_archive"]
        if name in inputs or name == "chainman.lock":
            raise ValueError("Bundled archive overlaps a managed bootstrap input")
        inputs[name] = managed_state(root, name)
    selected = registry.select(
        "github",
        registry.github_releases("chainmandev/chainman"),
        policy,
        "chainmandev/chainman",
        now,
    )
    if registry.version("github", selected.version) <= registry.version(
        "github", old["version"]
    ):
        return chainman.RUNTIME
    metadata, body, expected = release_assets(selected, policy, now)
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema") != 1
        or not isinstance(metadata.get("version"), str)
        or metadata["version"].lstrip("v") != selected.version.lstrip("v")
    ):
        raise ValueError("Runtime release metadata does not match selected version")
    keys = ("version", "revision", "url", "narHash")
    if any(not isinstance(metadata.get(key), str) or not metadata[key] for key in keys):
        raise ValueError("Runtime release metadata lacks required identity fields")
    required = {key: metadata[key] for key in keys}
    registry.artifact_url(required["url"])
    if required["revision"] != expected:
        raise ValueError("Runtime archive provenance differs from release tag")
    candidate = {"schema": 1, **required}
    if (
        old.get("bundled_archive") or metadata.get("archive_sha256") is not None
    ) and hashlib.sha256(body).hexdigest() != metadata.get("archive_sha256"):
        raise ValueError("Runtime archive checksum does not match release")
    runtime = fetch_runtime(candidate, body)
    validate_runtime(runtime, required["version"])
    prepared = {}
    for source, target in (
        ("chainman.sh", "chainman.sh"),
        ("fetch.nix", "chainman-fetch.nix"),
    ):
        name = "scripts/" + target
        before = inputs[name]
        if before is not None:
            prior = chainman.RUNTIME / "bootstrap" / source
            if before[0] != prior.read_bytes():
                raise ValueError(
                    "Managed bootstrap was locally modified; reconcile it explicitly"
                )
            prepared[name] = (
                before,
                (
                    (runtime / "bootstrap" / source).read_bytes(),
                    0o755 if target.endswith(".sh") else 0o644,
                ),
            )
    if old.get("bundled_archive"):
        # A self-contained consumer remains self-contained after its runtime upgrade.
        target = old["bundled_archive"]
        prepared[target] = (inputs[target], (body, 0o644))
        candidate["bundled_archive"] = old["bundled_archive"]
    prepared["chainman.lock"] = (
        lock_before,
        ((json.dumps(candidate, indent=2) + "\n").encode(), 0o644),
    )
    publication = managed if managed is not None else ManagedFiles(root)
    try:
        for name, (before, after) in prepared.items():
            publication.publish(name, before, after)
    except BaseException:
        publication.restore()
        raise
    return runtime


def perform(
    root: Path,
    policy: dict,
    now: datetime,
    extra: list[str],
    *,
    only_runtime=False,
    skip_runtime=False,
    managed: ManagedFiles | None = None,
) -> Path:
    runtime = (
        chainman.RUNTIME
        if skip_runtime
        else runtime_candidate(root, policy, now, managed)
    )
    if only_runtime:
        return runtime
    if runtime != chainman.RUNTIME:
        tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "develop",
                f"path:{quote(str(runtime / 'nix'), safe='/')}#core",
                "--no-write-lock-file",
                "--command",
                "python3",
                str(runtime / "scripts/chainman_updates.py"),
                "--resolve-root",
                str(root),
                now.isoformat(),
                json.dumps(extra),
            ],
            cwd=root,
            env=dict(
                os.environ,
                CHAINMAN_ROOT=str(root),
                CHAINMAN_PROJECT_ROOT=str(root),
                TOOLCHAIN_FRESH="1",
                CHAINMAN_UPDATE_ACTIVE="1",
            ),
            check=True,
        )
    else:
        resolve_current(root, policy, now, extra)
    return runtime


def resolve_current(root: Path, policy: dict, now: datetime, extra: list[str]):
    import dependency_api

    policy = dependency_api.policy(root)
    env = tc.environment(root)
    env.update(
        TOOLCHAIN_FRESH="1",
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(chainman.RUNTIME),
        CHAINMAN_UPDATE_ACTIVE="1",
        CHAINMAN_UPDATE_AT=now.isoformat(),
    )
    if policy.get("steps"):
        if policy.get("resolver"):
            raise ValueError("Use ordered steps or a legacy resolver, not both")
        dependency_api.run_steps(root, policy, now, extra)
    elif policy.get("resolver"):
        if policy.get("eligibility") != "resolver":
            raise ValueError(
                "Custom resolvers must explicitly own eligibility with updates.eligibility='resolver'"
            )
        env["CHAINMAN_MINIMUM_RELEASE_AGE_DAYS"] = str(
            policy.get("minimum_age_days", 30)
        )
        # Hooks perform selection/generation only. The surrounding transaction owns Git.
        chainman.run_hook(
            root,
            policy["resolver"],
            name=policy.get(
                "profile",
                tc.config(root).get("project", {}).get("default_profile", "default"),
            ),
            extra=extra,
            env=env,
        )
    else:
        if extra:
            raise ValueError("Built-in module updates do not accept resolver arguments")
        selected = tc.config(root)["modules"]
        updates.perform(root, now, selected)


def verify(root: Path, policy: dict, runtime: Path):
    # Re-enter the candidate runtime even when only the runtime pin changed.
    env = dict(
        os.environ,
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(runtime),
        TOOLCHAIN_FRESH="1",
        CHAINMAN_UPDATE_ACTIVE="1",
    )
    if runtime != chainman.RUNTIME:
        tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "develop",
                f"path:{quote(str(runtime / 'nix'), safe='/')}#core",
                "--no-write-lock-file",
                "--command",
                "python3",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                str(runtime / "tests"),
            ],
            cwd=runtime,
            env=env,
            check=True,
        )
    tc.managed_run(
        [
            tc.nix_command(),
            "--extra-experimental-features",
            "nix-command flakes",
            "develop",
            f"path:{quote(str(runtime / 'nix'), safe='/')}#core",
            "--no-write-lock-file",
            "--command",
            "python3",
            str(runtime / "scripts/chainman_updates.py"),
            "--verify-root",
            str(root),
        ],
        cwd=root,
        env=env,
        check=True,
    )


def verify_current(root: Path):
    import dependency_api

    policy = dependency_api.policy(root)
    env = tc.environment(root)
    env.update(TOOLCHAIN_FRESH="1", CHAINMAN_UPDATE_ACTIVE="1")
    if policy.get("verify"):
        chainman.run_hook(
            root,
            policy["verify"],
            name=policy.get(
                "profile",
                tc.config(root).get("project", {}).get("default_profile", "default"),
            ),
            env=env,
        )
    elif tc.config(root).get("commands", {}).get("verify"):
        chainman.run_project(root, "verify", [])
    else:
        updates.verify(root, tc.config(root)["modules"])


def apply(root: Path, opts, now: datetime):
    import dependency_api

    policy = dependency_api.policy(root)
    if not policy:
        raise ValueError("Declare project updates and verification first")
    patterns = [
        *policy.get("outputs", []),
        "chainman.lock",
        "scripts/chainman.sh",
        "scripts/chainman-fetch.nix",
    ]
    if (root / "chainman.lock").exists():
        lock = json.loads(tc.regular_input(root, "chainman.lock"))
        if lock.get("bundled_archive"):
            patterns.append(lock["bundled_archive"])
    if not policy.get("resolver") and not policy.get("steps"):
        patterns += [
            p
            for n in tc.config(root)["modules"]
            for p in tc.module(n, root).get("update_outputs", [])
        ]
    runtime = [chainman.RUNTIME]
    managed = ManagedFiles(root)

    def change():
        runtime[0] = perform(
            root,
            policy,
            now,
            opts.extra,
            only_runtime=opts.only_chainman,
            skip_runtime=opts.skip_chainman,
            managed=managed,
        )

    try:
        return updates.transaction(
            root,
            patterns,
            change,
            lambda: verify(root, policy, runtime[0]),
            not opts.no_commit,
            message=getattr(opts, "message", "chore: update dependencies"),
        )
    except BaseException:
        managed.restore()
        raise


def preview(root: Path, opts, now: datetime):
    updates.repository(root, clean=False)
    before = updates.snapshot(root)
    with tempfile.TemporaryDirectory(prefix="chainman-preview-") as directory:
        copy = Path(directory)
        with updates.preview_git_environment():
            updates.prepare_preview(root, copy, before)
            # Ignored installed runtimes are not copied. Nested launchers verify
            # the copied bundle or fetch the declared pin, just like a new checkout.
            saved = {
                name: os.environ.get(name)
                for name in (
                    "TOOLCHAIN_LOCK_FD",
                    "CHAINMAN_ROOT",
                    "CHAINMAN_PROJECT_ROOT",
                    "CHAINMAN_COMPILER_OWNER",
                    "CHAINMAN_CONTAINER_OPTIONS_FILE",
                )
            }
            try:
                for name in saved:
                    os.environ.pop(name, None)
                os.environ["CHAINMAN_ROOT"] = str(copy)
                os.environ["CHAINMAN_PROJECT_ROOT"] = str(copy)
                opts.no_commit = True
                with tc.operation(copy):
                    result = apply(copy, opts, now)
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        if updates.snapshot(root) != before:
            raise ValueError("Original project changed during preview")
        return {"preview": True, **result}


@contextmanager
def machine_output(enabled: bool):
    """Keep inherited child-process output out of the JSON result stream."""
    if not enabled:
        yield
        return
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def run(root: Path, args: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write one schema-1 JSON result; send command output to stderr",
    )
    parser.add_argument("--message", default="chore: update dependencies")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--only-chainman", action="store_true")
    group.add_argument("--skip-chainman", action="store_true")
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    opts = parser.parse_args(args)
    if opts.extra[:1] == ["--"]:
        opts.extra = opts.extra[1:]
    if os.environ.get("CHAINMAN_UPDATE_ACTIVE"):
        raise ValueError("An update hook must not recursively start another update")
    now = datetime.now(timezone.utc)
    with machine_output(opts.json), tc.operation(root):
        result = preview(root, opts, now) if opts.preview else apply(root, opts, now)
    print(json.dumps({"schema": 1, **result}, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-root":
        verify_current(Path(sys.argv[2]).resolve())
    elif len(sys.argv) == 5 and sys.argv[1] == "--resolve-root":
        root = Path(sys.argv[2]).resolve()
        resolve_current(
            root,
            tc.config(root)["updates"],
            datetime.fromisoformat(sys.argv[3]),
            json.loads(sys.argv[4]),
        )
    else:
        raise SystemExit("Use the Chainman launcher")
