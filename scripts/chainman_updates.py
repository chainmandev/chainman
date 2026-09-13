"""Consumer hooks and runtime updates in one verified Git transaction."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from typing import Literal, NotRequired, TypedDict

import adapter_data as ad
import chainman
import registry
import source_updates
import toolchain as tc
import updates
from transaction_state import Options

type FileState = tuple[bytes, int]


class RuntimePin(TypedDict):
    schema: Literal[1]
    version: str
    revision: str
    url: str
    narHash: str
    bundled_archive: NotRequired[str]


class ReleaseAsset(TypedDict):
    id: int
    size: int
    digest: str


def managed_state(root: Path, name: str) -> FileState | None:
    path = tc.contained(root, name)
    if not path.exists():
        return None
    return tc.regular_input(root, name), stat.S_IMODE(path.stat().st_mode)


def managed_paths(root: Path) -> dict[str, str]:
    """Map declared identical runtime copies to their root-owned source files."""
    names = ["chainman.lock", "scripts/chainman.sh", "scripts/chainman-fetch.nix"]
    if (root / "chainman.lock").exists():
        bundle = json.loads(tc.regular_input(root, "chainman.lock")).get(
            "bundled_archive"
        )
        if bundle:
            if not isinstance(bundle, str) or bundle in names:
                raise ValueError("Bundled archive overlaps a managed bootstrap input")
            tc.contained(root, bundle)
            names.append(bundle)
    cfg = (
        tc.config(root).get("runtime", {})
        if (root / "chainman.toml").exists() or (root / "toolchain.toml").exists()
        else {}
    )
    if not isinstance(cfg, dict) or set(cfg) - {"copies"}:
        raise ValueError("Runtime declarations support only copies")
    copies = cfg.get("copies", [])
    if not isinstance(copies, list) or any(
        not isinstance(item, str) for item in copies
    ):
        raise ValueError("Runtime copies must be relative project directories")
    result = {name: name for name in names}
    directories = set()
    for value in copies:
        path = tc.contained(root, value)
        if path == root or not path.is_dir() or ".git" in Path(value).parts:
            raise ValueError(
                "Runtime copies require ordinary contained project directories"
            )
        relative = path.relative_to(root).as_posix()
        if relative in directories:
            raise ValueError("Runtime copy directories must be distinct")
        directories.add(relative)
        for source in names:
            target = f"{relative}/{source}"
            tc.contained(root, target)
            if target in result or any(
                target.startswith(existing + "/") or existing.startswith(target + "/")
                for existing in result
            ):
                raise ValueError("Runtime copies overlap managed files")
            result[target] = source
    return result


class ManagedFiles:
    """Restore only our still-identical managed outputs after a failed candidate."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.written: dict[str, tuple[FileState | None, FileState]] = {}

    def publish(self, name: str, before: FileState | None, after: FileState) -> None:
        if managed_state(self.root, name) != before:
            raise ValueError(
                f"Managed runtime input changed during preparation: {name}"
            )
        # Record before attempting the replacement: a failure can occur after
        # the atomic replacement, and recovery must still recognize its bytes.
        self.written[name] = (before, after)
        tc.atomic_bytes(tc.contained(self.root, name), after[0], after[1])

    def restore(self) -> None:
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


def fetch_source(root: Path, *, gc_root: Path) -> Path:
    """Fetch with the trusted helper and register a root before Nix exits.

    The caller owns gc_root and must retain it through the last source read or
    execution. Container roots must be visible to the shared Nix daemon.
    """
    env = dict(
        os.environ,
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_BOOTSTRAP_HELPER=str(chainman.RUNTIME / "bootstrap/fetch.nix"),
    )
    expression = 'import (builtins.toPath (builtins.getEnv "CHAINMAN_BOOTSTRAP_HELPER")) { root = builtins.getEnv "CHAINMAN_PROJECT_ROOT"; action = "fetch"; archive = ""; }'
    runtime = Path(
        tc.managed_run(
            [
                tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                "--impure",
                "--out-link",
                str(gc_root),
                "--print-out-paths",
                "--expr",
                expression,
            ],
            text=True,
            cwd=root,
            env=env,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    if runtime.parent != Path("/nix/store"):
        raise ValueError("Runtime fetch did not return a Nix store tree")
    return runtime


def fetch_runtime(
    candidate: Mapping[str, object], body: bytes, *, gc_root: Path
) -> Path:
    # Fetch exactly the downloaded bytes, before changing the live consumer pin.
    # The trusted old helper validates their unpacked NAR hash; no candidate
    # source is imported or executed while this temporary lock is evaluated.
    with tempfile.TemporaryDirectory(prefix="chainman-candidate-") as directory:
        staged = Path(directory)
        (staged / "archive.tar.gz").write_bytes(body)
        (staged / "chainman.lock").write_text(
            json.dumps({**candidate, "bundled_archive": "archive.tar.gz"})
        )
        runtime = fetch_source(staged, gc_root=gc_root)
    if (
        runtime.parent != Path("/nix/store")
        or runtime.is_symlink()
        or not runtime.is_dir()
    ):
        raise ValueError("Runtime fetch did not return a real Nix store tree")
    return runtime


def validate_runtime(runtime: Path, version: str) -> None:
    if runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("Candidate runtime must be a real directory")

    def unreadable(error: OSError) -> None:
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


def release_assets(
    selected: registry.Release, policy: Mapping[str, object], now: datetime
) -> tuple[object, bytes, str]:
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
    assets: dict[str, ReleaseAsset] = {}
    for name in names:
        matches = [
            item
            for item in release["assets"]
            if isinstance(item, dict) and item.get("name") == name
        ]
        if len(matches) != 1:
            raise ValueError("Runtime release lacks one exact required asset")
        item = matches[0]
        asset_id, size, digest = item.get("id"), item.get("size"), item.get("digest")
        if (
            type(asset_id) is not int
            or asset_id <= 0
            or item.get("state") != "uploaded"
            or type(size) is not int
            or size <= 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        ):
            raise ValueError("Runtime asset lacks immutable checksum evidence")
        published = max(
            published,
            registry.timestamp(item.get("created_at")),
            registry.timestamp(item.get("updated_at")),
        )
        assets[name] = {"id": asset_id, "size": size, "digest": digest}
    registry.eligible(
        "github",
        [registry.Release(selected.version, published)],
        policy,
        repository,
        now,
    )
    bodies = []
    for name in names:
        asset = assets[name]
        body = registry.fetch(
            f"{api}/releases/assets/{asset['id']}", accept="application/octet-stream"
        )[0]
        if (
            len(body) != asset["size"]
            or "sha256:" + hashlib.sha256(body).hexdigest() != asset["digest"]
        ):
            raise ValueError("Runtime asset bytes differ from dated release identity")
        bodies.append(body)
    if registry.github_commit(repository, selected.version, fresh=True) != revision:
        raise ValueError("Runtime release tag changed during download")
    return json.loads(bodies[0]), bodies[1], revision


def runtime_candidate(
    root: Path,
    policy: Mapping[str, object],
    now: datetime,
    managed: ManagedFiles | None = None,
    *,
    gc_root: Path,
) -> Path:
    lockpath = tc.contained(root, "chainman.lock")
    if not lockpath.exists():
        return chainman.RUNTIME
    lock_before = managed_state(root, "chainman.lock")
    if lock_before is None:
        raise ValueError("Runtime pin disappeared during preparation")
    old = ad.table(json.loads(lock_before[0]), "Runtime pin")
    inputs = {
        name: managed_state(root, name)
        for name in ("scripts/chainman.sh", "scripts/chainman-fetch.nix")
    }
    if old.get("bundled_archive"):
        name = ad.text(old["bundled_archive"], "Bundled runtime archive")
        if name in inputs or name == "chainman.lock":
            raise ValueError("Bundled archive overlaps a managed bootstrap input")
        inputs[name] = managed_state(root, name)
    copies = {}
    source_states = {"chainman.lock": lock_before, **inputs}
    for target, source in managed_paths(root).items():
        if target == source:
            continue
        before = managed_state(root, target)
        if before is None or before != source_states[source]:
            raise ValueError(
                f"Managed runtime copy was locally modified; reconcile it explicitly: {target}"
            )
        copies[target] = (source, before)
    selected = registry.select(
        "github",
        registry.github_releases("chainmandev/chainman"),
        policy,
        "chainmandev/chainman",
        now,
    )
    if registry.version("github", selected.version) <= registry.version(
        "github", ad.text(old["version"], "Runtime version")
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
    required = {key: ad.text(metadata[key], f"Runtime {key}") for key in keys}
    registry.artifact_url(required["url"])
    if required["revision"] != expected:
        raise ValueError("Runtime archive provenance differs from release tag")
    candidate: RuntimePin = {
        "schema": 1,
        "version": required["version"],
        "revision": required["revision"],
        "url": required["url"],
        "narHash": required["narHash"],
    }
    if (
        old.get("bundled_archive") or metadata.get("archive_sha256") is not None
    ) and hashlib.sha256(body).hexdigest() != metadata.get("archive_sha256"):
        raise ValueError("Runtime archive checksum does not match release")
    runtime = fetch_runtime(candidate, body, gc_root=gc_root)
    validate_runtime(runtime, required["version"])
    prepared: dict[str, tuple[FileState | None, FileState]] = {}
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
        target = ad.text(old["bundled_archive"], "Bundled runtime archive")
        prepared[target] = (inputs[target], (body, 0o644))
        candidate["bundled_archive"] = target
    prepared["chainman.lock"] = (
        lock_before,
        ((json.dumps(candidate, indent=2) + "\n").encode(), 0o644),
    )
    for target, (source, before) in copies.items():
        prepared[target] = (before, prepared[source][1])
    import recipes

    for consumer in recipes.roots(root):
        path = str((consumer / recipes.FILE).relative_to(root))
        before = managed_state(root, path)
        if before is None or before[0] != recipes.render(recipes.config(consumer)):
            raise ValueError("Reconcile the declared recipe facade before updating")
        rendered = tc.managed_run(
            [
                sys.executable,
                str(runtime / "scripts/recipes.py"),
                str(consumer),
                "--render",
            ],
            capture_output=True,
            check=True,
        ).stdout
        prepared[path] = (before, (rendered, 0o644))
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
    policy: Mapping[str, object],
    now: datetime,
    extra: list[str],
    *,
    only_runtime: bool = False,
    skip_runtime: bool = True,
    managed: ManagedFiles | None = None,
) -> Path:
    with tc.nix_temporary_directory("chainman-runtime-update-") as directory:
        runtime = (
            chainman.RUNTIME
            if skip_runtime and not only_runtime
            else runtime_candidate(
                root, policy, now, managed, gc_root=Path(directory) / "runtime"
            )
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
                    f"path:{quote(str(runtime / 'nix'), safe='/')}#updates",
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


def resolve_current(
    root: Path, policy: Mapping[str, object], now: datetime, extra: list[str]
) -> None:
    import dependency_api

    policy = dependency_api.policy(root)
    env = tc.environment(root)
    if policy.get("adapters") or policy.get("steps"):
        dependency_api.plan_steps(root, policy, extra)
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
            name=ad.text(
                policy.get(
                    "profile",
                    ad.table(tc.config(root).get("project", {}), "Project").get(
                        "default_profile", "default"
                    ),
                ),
                "Resolver profile",
            ),
            extra=extra,
            env=env,
        )
    else:
        if extra:
            selection = dependency_api.selection_arguments(extra)
            if (
                selection.targets != "all"
                or selection.policy
                or selection.target_policy
            ):
                raise ValueError(
                    "Built-in module updates do not accept target selection or policy overrides"
                )
        selected = ad.strings(tc.config(root)["modules"], "Modules")
        updates.perform(root, now, selected)


def verify(root: Path, policy: Mapping[str, object], runtime: Path) -> None:
    # Re-enter the candidate runtime even when only the runtime pin changed.
    env = dict(
        os.environ,
        CHAINMAN_ROOT=str(root),
        CHAINMAN_PROJECT_ROOT=str(root),
        CHAINMAN_RUNTIME=str(runtime),
        TOOLCHAIN_FRESH="1",
        CHAINMAN_UPDATE_ACTIVE="1",
    )
    tc.managed_run(
        [
            tc.nix_command(),
            "--extra-experimental-features",
            "nix-command flakes",
            "develop",
            f"path:{quote(str(runtime / 'nix'), safe='/')}#updates",
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


def verify_current(root: Path) -> None:
    import dependency_api

    policy = dependency_api.policy(root)
    env = tc.environment(root)
    env.update(TOOLCHAIN_FRESH="1", CHAINMAN_UPDATE_ACTIVE="1")
    if policy.get("verify"):
        chainman.run_hook(
            root,
            policy["verify"],
            name=ad.text(
                policy.get(
                    "profile",
                    ad.table(tc.config(root).get("project", {}), "Project").get(
                        "default_profile", "default"
                    ),
                ),
                "Verification profile",
            ),
            env=env,
        )
    elif ad.table(tc.config(root).get("commands", {}), "Commands").get("verify"):
        chainman.run_project(root, "verify", [])
    else:
        updates.verify(root, ad.strings(tc.config(root)["modules"], "Modules"))


def options(args: list[str]) -> Options:
    import recipes

    args = recipes.options(args)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write one schema-1 JSON result; send command output to stderr",
    )
    parser.add_argument("--message", default="chore: update dependencies")
    runtime_flags = parser.add_mutually_exclusive_group()
    runtime_flags.add_argument(
        "--only-chainman", action="store_true", help=argparse.SUPPRESS
    )
    runtime_flags.add_argument(
        "--skip-chainman", action="store_true", help="Retain the Chainman runtime pin"
    )
    runtime_flags.add_argument(
        "--include-chainman", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    opts = parser.parse_args(args)
    if opts.staged:
        if not opts.format or opts.preview:
            raise ValueError("Staged formatting requires format without preview")
        opts.no_commit = True
    if opts.format:
        if opts.only_chainman or opts.include_chainman or opts.extra:
            raise ValueError("Format does not select dependency targets")
        if opts.message == "chore: update dependencies":
            opts.message = "chore: format"
    if opts.extra[:1] == ["--"]:
        opts.extra = opts.extra[1:]
    if not opts.message.strip() or "\0" in opts.message:
        raise ValueError("Commit message must be nonempty text without NUL")
    if opts.only_chainman and opts.extra:
        raise ValueError("Runtime updates do not accept dependency resolver arguments")
    import dependency_api

    if opts.only_chainman:
        opts.runtime = "only"
    elif opts.skip_chainman or opts.format:
        opts.runtime = "exclude"
    elif opts.include_chainman:
        opts.runtime = "include"
    else:
        selection = dependency_api.selection_arguments(opts.extra, allow_extra=True)
        opts.runtime = "include" if "all" in selection.targets.split(",") else "exclude"
    if os.environ.get("CHAINMAN_UPDATE_ACTIVE"):
        raise ValueError("An update hook must not recursively start another update")
    del opts.only_chainman
    del opts.skip_chainman
    del opts.include_chainman
    return Options.decode(vars(opts))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--verify-root":
        verify_current(Path(sys.argv[2]).resolve())
    elif len(sys.argv) == 5 and sys.argv[1] == "--resolve-root":
        root = Path(sys.argv[2]).resolve()
        resolve_current(
            root,
            ad.table(tc.config(root)["updates"], "Updates"),
            datetime.fromisoformat(sys.argv[3]),
            json.loads(sys.argv[4]),
        )
    else:
        raise SystemExit("Use the Chainman launcher")
