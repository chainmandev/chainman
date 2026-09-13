"""Resolve, verify, and commit a precisely scoped local dependency update."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

import manifests
import adapter_data
import lock_adapters
import sdk_versions
import registry
from dependency_identity import Identity, inventory as identity_inventory
from toolchain import (
    atomic_bytes,
    ROOT,
    config,
    contained,
    local_source,
    environment,
    managed_run,
    module,
    operation as operation,
    run_commands,
    setup,
    entry_command,
    RUNTIME,
)


@contextmanager
def preview_git_environment():
    # Preview owns a disposable repository. Ambient Git routing, configuration,
    # identity, credentials and hooks must not target the original repository.
    saved = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
    try:
        for key in saved:
            del os.environ[key]
        os.environ.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="core.fsmonitor",
            GIT_CONFIG_VALUE_0="false",
            GIT_CONFIG_KEY_1="core.hooksPath",
            GIT_CONFIG_VALUE_1=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_OPTIONAL_LOCKS="0",
        )
        yield
    finally:
        for key in list(os.environ):
            if key.startswith("GIT_"):
                del os.environ[key]
        os.environ.update(saved)


def git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "--literal-pathspecs", *args],
        cwd=root,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    return result if "-z" in args else result.rstrip("\n")


def repository(root: Path, clean: bool = True) -> tuple[str, str]:
    actual = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    if actual != root.resolve():
        raise ValueError(
            "Copy the example into its own project root before updating; refusing the enclosing repository"
        )
    branch = git(root, "symbolic-ref", "HEAD")
    head = git(root, "rev-parse", "HEAD")
    for entry in git(root, "ls-files", "--cached", "-v", "-z").split("\0"):
        if entry and (entry[0].islower() or entry[0] == "S"):
            raise ValueError(
                "Dependency updates reject assume-unchanged or skip-worktree index flags; flags and source are preserved"
            )
    if clean:
        if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise ValueError("Dependency updates require a clean worktree and index")
        expected = tree_entries(root, head)
        if staged_entries(root) != expected or raw_entries(root, expected) != expected:
            raise ValueError(
                "Actual raw tracked bytes/Git modes differ from HEAD; inspect source filters, core.filemode and the preserved index"
            )
    return branch, head


def file_identity(path: Path) -> str | None:
    """Fingerprint one source entry using its raw bytes and full file mode."""
    if path.is_symlink():
        body = b"symlink\0" + os.readlink(path).encode()
    elif path.is_file():
        body = str(path.stat().st_mode & 0o777).encode() + b"\0" + path.read_bytes()
    elif not path.exists():
        return None
    else:
        raise ValueError("Unexpected directory in project source inventory")
    return hashlib.sha256(body).hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    paths = git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ).split("\0")
    result = {}
    links = gitlinks(root)
    for name in paths:
        if not name:
            continue
        path = root / name
        if name in links:
            body = json.dumps(
                submodule_state(root, name, links[name]), sort_keys=True
            ).encode()
            identity = hashlib.sha256(body).hexdigest()
        else:
            identity = file_identity(path)
        # Deleted tracked files have the same source identity before and after
        # staging removes them from the index.
        if identity is not None:
            result[name] = identity
    return result


def changed(before: dict, after: dict) -> list[str]:
    return sorted(
        p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
    )


def allowed(paths: list[str], patterns: list[str]) -> None:
    for pattern in patterns:
        if (
            Path(pattern).is_absolute()
            or ".." in Path(pattern).parts
            or ".git" in Path(pattern).parts
        ):
            raise ValueError("Unsafe declared dependency output")
    for path in paths:
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
            raise ValueError(
                f"Unexpected update/verification output: {path}; changes are preserved"
            )


def tree_entries(root: Path, revision: str) -> dict[str, tuple[str, str]]:
    entries = {}
    for record in git(root, "ls-tree", "-r", "-z", "--full-tree", revision).split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            mode, _, identity = metadata.split()
            entries[name] = (mode, identity)
    return entries


def gitlinks(root: Path) -> dict[str, str]:
    return {
        name: identity
        for name, (mode, identity) in tree_entries(root, "HEAD").items()
        if mode == "160000"
    }


def submodule_state(root: Path, name: str, identity: str) -> dict:
    """Submodules are frozen inputs, never targets or implicitly fetched sources."""
    path = contained(root, name)
    if not path.exists() or (path.is_dir() and not any(path.iterdir())):
        return {"commit": identity, "initialized": False}
    if not path.is_dir() or not (path / ".git").exists():
        raise ValueError("Submodule input is not an empty or initialized checkout")
    if Path(git(path, "rev-parse", "--show-toplevel")).resolve() != path.resolve():
        raise ValueError("Submodule input must own its checkout")
    if git(path, "rev-parse", "HEAD") != identity:
        raise ValueError(
            "Submodule commit changed; update it in a separate transaction"
        )
    for entry in git(path, "ls-files", "--cached", "-v", "-z").split("\0"):
        if entry and (entry[0].islower() or entry[0] == "S"):
            raise ValueError("Submodule input has hidden index flags")
    expected = tree_entries(path, identity)
    if (
        git(path, "ls-files", "--others", "--exclude-standard", "-z")
        or staged_entries(path) != expected
        or raw_entries(path, expected) != expected
    ):
        raise ValueError(
            "Submodule input changed; preserve it for separate verification"
        )
    return {"commit": identity, "initialized": True, "sources": snapshot(path)}


def raw_entries(root: Path, names) -> dict:
    """Read actual tracked contents without Git stat-cache or clean-filter decisions."""
    entries = {}
    links = gitlinks(root)
    for name in names:
        relative = Path(name)
        path = contained(root, str(relative.parent)) / relative.name
        if name in links:
            submodule_state(root, name, links[name])
            entries[name] = ("160000", links[name])
            continue
        if path.is_symlink():
            mode, body = "120000", os.fsencode(os.readlink(path))
        elif path.is_file():
            mode, body = (
                ("100755" if path.stat().st_mode & 0o100 else "100644"),
                path.read_bytes(),
            )
        elif not path.exists():
            continue
        else:
            raise ValueError(
                "Exact tracked-byte verification requires files, symlinks or unchanged submodules"
            )
        identity = (
            subprocess.run(
                ["git", "hash-object", "--no-filters", "--stdin"],
                cwd=root,
                input=body,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        entries[name] = (mode, identity)
    return entries


def preview_link(root: Path, path: Path) -> str:
    """Keep copied link text relative and contained before any workflow executes."""
    value = os.readlink(path)
    if Path(value).is_absolute():
        raise ValueError("Preview rejects absolute source symlinks")
    lexical = Path(os.path.abspath(path.parent / value))
    try:
        try:
            path.stat()
        except FileNotFoundError:
            pass  # A portable link may target a not-yet-generated file.
        actual = path.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError(
            "Preview rejects unresolved or cyclic source symlinks"
        ) from None
    if not lexical.is_relative_to(root) or not actual.is_relative_to(root):
        raise ValueError(
            "Preview rejects source symlinks escaping their copied project"
        )
    return value


def copy_submodule(
    source: Path, target: Path, identity: str, *, preserve_modes=True
) -> None:
    """Copy committed blobs, with no working edits, history, remotes or hooks."""
    target.mkdir(parents=True, exist_ok=True)
    git(
        target,
        "init",
        "-b",
        "input",
        "--object-format=" + git(source, "rev-parse", "--show-object-format"),
    )
    objects = {
        identity: "commit",
        git(source, "rev-parse", identity + "^{tree}"): "tree",
    }
    for record in git(source, "ls-tree", "-r", "-t", "-z", identity).split("\0"):
        if record:
            metadata, _ = record.split("\t", 1)
            _, kind, oid = metadata.split()
            if kind != "commit":
                objects[oid] = kind
    for oid, kind in objects.items():
        body = subprocess.run(
            ["git", "cat-file", kind, oid],
            cwd=source,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        actual = (
            subprocess.run(
                ["git", "hash-object", "-w", "-t", kind, "--stdin"],
                cwd=target,
                input=body,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        if actual != oid:
            raise ValueError("Submodule preview object identity changed")
    (target / ".git/shallow").write_text(identity + "\n")
    git(target, "update-ref", "HEAD", identity)
    git(target, "read-tree", identity)
    for name, (mode, oid) in tree_entries(source, identity).items():
        original = contained(source, str(Path(name).parent)) / Path(name).name
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if mode == "160000":
            state = submodule_state(source, name, oid)
            destination.mkdir()
            if state["initialized"]:
                copy_submodule(original, destination, oid)
        else:
            restore_blob(target, name, mode, oid)
            if (
                preserve_modes
                and mode in {"100644", "100755"}
                and original.is_file()
                and not original.is_symlink()
            ):
                destination.chmod(original.stat().st_mode & 0o777)
    if raw_entries(target, tree_entries(source, identity)) != tree_entries(
        source, identity
    ):
        raise ValueError("Submodule preview does not reproduce its current source tree")


def restore_blob(root: Path, name: str, mode: str, oid: str) -> None:
    """Restore exact Git bytes without filters, executable hooks or checkout config."""
    path = contained(root, str(Path(name).parent)) / Path(name).name
    body = subprocess.run(
        ["git", "cat-file", "blob", oid],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout
    if path.is_symlink():
        path.unlink()
    if mode == "120000":
        path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(os.fsdecode(body))
        preview_link(root, path)
    elif mode in {"100644", "100755"}:
        atomic_bytes(path, body, 0o755 if mode == "100755" else 0o644)
    else:
        raise ValueError("Cannot restore a non-file Git entry")


def restore_paths(root: Path, identity: str, names: list[str], modes=None) -> None:
    entries = tree_entries(root, identity)
    for name in names:
        if name in entries:
            restore_blob(root, name, *entries[name])
            if modes and name in modes:
                contained(root, name).chmod(modes[name])
        else:
            path = contained(root, str(Path(name).parent)) / Path(name).name
            path.unlink(missing_ok=True)


def prepare_preview(root: Path, copy: Path, before: dict) -> None:
    """Create a source-only baseline, preserving frozen submodule identities."""
    links = gitlinks(root)
    git(
        copy,
        "init",
        "-b",
        "preview",
        "--object-format=" + git(root, "rev-parse", "--show-object-format"),
    )
    files = []
    for name in before:
        source = contained(root, str(Path(name).parent)) / Path(name).name
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name in links:
            state = submodule_state(root, name, links[name])
            target.mkdir()
            if state["initialized"]:
                copy_submodule(source, target, links[name])
            git(
                copy,
                "update-index",
                "--add",
                "--cacheinfo",
                "160000," + links[name] + "," + name,
            )
        elif source.is_symlink():
            target.symlink_to(preview_link(root, source))
            files.append(name)
        elif source.is_file():
            shutil.copy2(source, target)
            files.append(name)
        elif source.exists():
            raise ValueError("Preview requires regular sources or unchanged submodules")
    if files:
        git(copy, "add", "--force", "--", *files)
    git(
        copy,
        "-c",
        "user.name=Preview",
        "-c",
        "user.email=preview@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--allow-empty",
        "-m",
        "Disposable preview baseline",
    )


def expected_entries(root: Path, head: str, paths: list[str]) -> dict:
    for name in paths:
        path = contained(root, name)
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("Dependency outputs must be regular files")
    # Re-read every tracked source, including paths that the updater did not change.
    return raw_entries(root, tree_entries(root, head).keys() | set(paths))


def staged_entries(root: Path) -> dict[str, tuple[str, str]]:
    entries = {}
    for record in git(root, "ls-files", "--stage", "-z").split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            mode, identity, stage = metadata.split()
            if stage != "0" or name in entries:
                raise ValueError("The index contains unresolved or duplicate entries")
            entries[name] = (mode, identity)
    return entries


def signing_required(root: Path) -> bool:
    if os.environ.get("TOOLCHAIN_GIT_POLICY_UNAVAILABLE") == "1":
        raise ValueError(
            "Host Git signing policy could not be read; use host mode or --no-commit"
        )
    result = subprocess.run(
        ["git", "config", "--bool", "--get", "commit.gpgsign"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode == 1:
        return False  # Git's documented missing-key result, distinct from malformed configuration.
    if result.returncode or result.stdout.strip() not in ("true", "false"):
        raise ValueError(
            "Invalid Git commit signing configuration; refusing an unsigned fallback"
        )
    return result.stdout.strip() == "true"


def commit_verified(
    root: Path,
    branch: str,
    head: str,
    candidate: dict,
    paths: list[str],
    message: str = "chore: update dependencies",
) -> str:
    if repository(root, clean=False) != (branch, head) or snapshot(root) != candidate:
        raise ValueError("The verified candidate changed before staging")
    if git(root, "diff", "--cached", "--name-only"):
        raise ValueError("Another process changed the index")
    expected = expected_entries(root, head, paths)
    signing = signing_required(root)
    git(root, "add", "--all", "--", *paths)
    if staged_entries(root) != expected:
        raise ValueError(
            "The staged tree differs from verified bytes/modes or contains unrelated index changes; inspect Git filters and the preserved index"
        )
    tree = git(root, "write-tree")
    if tree_entries(root, tree) != expected:
        raise ValueError(
            "The immutable staged tree differs from the verified candidate; no branch was advanced"
        )
    if snapshot(root) != candidate or repository(root, clean=False) != (branch, head):
        raise ValueError("The candidate changed during staging")
    args = ["commit-tree", tree, "-p", head, "-m", message]
    if signing:
        args.append("-S")
    # Commit the immutable verified tree, then compare-and-swap the named branch.
    # Git supplies identity and its configured signing backend/key. Arbitrary Git
    # commit hooks cannot rewrite this candidate; put required checks in verify.
    commit = git(root, *args)
    if (
        git(root, "write-tree") != tree
        or snapshot(root) != candidate
        or repository(root, clean=False) != (branch, head)
    ):
        raise ValueError("The candidate changed while preparing its signed commit")
    git(
        root,
        "update-ref",
        "-m",
        "dependency update: verified candidate",
        branch,
        commit,
        head,
    )
    if repository(root) != (branch, commit):
        raise ValueError(
            f"Concurrent change after creating commit {commit}; inspect the preserved repository"
        )
    return commit


def transaction(
    root: Path,
    patterns: list[str],
    update,
    verify,
    commit: bool = True,
    *,
    message: str = "chore: update dependencies",
) -> dict:
    if not isinstance(message, str) or not message.strip() or "\0" in message:
        raise ValueError("Commit message must be nonempty text without NUL")
    branch, head = repository(root)
    before = snapshot(root)
    update()
    updated = snapshot(root)
    paths = changed(before, updated)
    if set(paths) & (set(gitlinks(root)) | {".gitmodules"}):
        raise ValueError("Submodule inputs and metadata require a separate transaction")
    allowed(paths, patterns)
    if repository(root, clean=False) != (branch, head) or git(
        root, "diff", "--cached", "--name-only"
    ):
        raise ValueError("Updater changed Git HEAD or index")
    if not paths:
        return {"changed": [], "commit": None, "verification": "no changes"}
    verify()
    candidate = snapshot(root)
    if candidate != updated:
        raise ValueError(
            "Verification changed source or dependency outputs; inspect and rerun"
        )
    if repository(root, clean=False) != (branch, head) or git(
        root, "diff", "--cached", "--name-only"
    ):
        raise ValueError(
            "Verification changed Git HEAD or index; changes are preserved"
        )
    identifier = (
        commit_verified(root, branch, head, candidate, paths, message)
        if commit
        else None
    )
    return {"changed": paths, "commit": identifier, "verification": "passed"}


def settings(root: Path) -> dict:
    cfg = config(root)
    policy = dict(cfg["updates"])
    extra = tomllib.loads(contained(root, "dependencies.toml").read_text())
    policy.update(extra)
    return policy


def lock_identities(
    root: Path, selected: list[str], *, specs: dict | None = None
) -> set[Identity]:
    """One identity per actual distribution, with explicit URLs where the lock has them."""
    identities: set[Identity] = set()

    def add(provider, package, value, url, digest):
        if not package or registry.lock_version(provider, value) is None:
            raise ValueError("Unrecognized stable lock identity")
        identities.add(
            Identity(
                provider=provider,
                package=registry.package_name(provider, package),
                version=value,
                url=registry.artifact_url(url) if url else "",
                digest=digest,
            )
        )

    for name in selected:
        spec = module(name, root) if specs is None else specs[name]
        directory = contained(root, spec["directory"])
        kind = spec.get("ecosystem")
        if kind in ("go", "swift", "maven"):
            identities.update(lock_adapters.identities(root, spec))
            continue
        filename = {
            "npm": "pnpm-lock.yaml",
            "crates": "Cargo.lock",
            "pypi": "uv.lock",
            "pub": "pubspec.lock",
        }.get(kind)
        if not filename:
            continue
        path = contained(root, str((directory / filename).relative_to(root)))
        if not path.exists():
            continue
        if kind == "npm":
            lock = manifests.document(path)[0]
            projected = adapter_data.pnpm_lock(lock)
            entries = projected["packages"]
            if spec.get("retained_sources"):
                import javascript_sources

                entries = {
                    key: entries[key]
                    for key in javascript_sources.registry_entries(root, spec, lock)
                }
                identities.update(javascript_sources.lock_identities(root, spec, lock))
            import javascript_updates

            if javascript_updates.has_local_resolution(projected):
                entries = javascript_updates.local_registry_entries(
                    javascript_updates.Workspace(root, spec), projected, entries
                )
            for key, item in entries.items():
                package, _, version = key.partition("(")[0].rpartition("@")
                resolution = item.get("resolution", {})
                if set(resolution) - {"integrity", "tarball"}:
                    raise ValueError("Unrecognized npm lock resolution source")
                if "tarball" in resolution:
                    registry.artifact_url(resolution["tarball"])
                add(
                    kind,
                    package,
                    version,
                    resolution.get("tarball", ""),
                    registry.digest(resolution.get("integrity"), npm=True),
                )
        elif kind == "crates":
            lock = tomllib.loads(path.read_text())
            for item in lock.get("package", []):
                if not item.get("source"):
                    continue
                if (
                    item["source"]
                    != "registry+https://github.com/rust-lang/crates.io-index"
                ):
                    raise ValueError("Unrecognized Cargo lock registry")
                add(
                    kind,
                    item["name"],
                    item["version"],
                    "",
                    registry.digest("sha256:" + item.get("checksum", "")),
                )
        elif kind == "pypi":
            lock = tomllib.loads(path.read_text())
            for item in lock.get("package", []):
                source = item["source"]
                if set(source) in ({"virtual"}, {"editable"}):
                    local_source(root, directory, next(iter(source.values())))
                    continue
                if (
                    set(source) != {"registry"}
                    or source.get("registry") != "https://pypi.org/simple"
                ):
                    raise ValueError("Unrecognized Python lock registry")
                artifacts = list(item.get("wheels", [])) + (
                    [item["sdist"]] if "sdist" in item else []
                )
                if not artifacts:
                    raise ValueError("Python registry lock lacks artifact identities")
                for artifact in artifacts:
                    add(
                        kind,
                        item["name"],
                        item["version"],
                        registry.artifact_url(artifact.get("url")),
                        registry.digest(artifact.get("hash")),
                    )
        elif kind == "pub":
            lock = manifests.document(path)[0]
            for package, item in lock.get("packages", {}).items():
                if item["source"] == "path":
                    local_source(root, directory, item["description"]["path"])
                    continue
                if item["source"] == "sdk":
                    continue
                if (
                    item["source"] != "hosted"
                    or item["description"].get("url") != "https://pub.dev"
                ):
                    raise ValueError("Unrecognized Dart lock registry")
                if item["description"].get("name") != package:
                    raise ValueError("Dart lock package identity mismatch")
                add(
                    kind,
                    package,
                    item["version"],
                    "",
                    registry.digest("sha256:" + item["description"].get("sha256", "")),
                )
    return identities


def audit_locks(
    root: Path,
    selected: list[str],
    before: set,
    policy: dict,
    now: datetime,
    *,
    specs: dict | None = None,
) -> None:
    for name in selected:
        spec = module(name, root) if specs is None else specs[name]
        filename = {
            "crates": "Cargo.lock",
            "pypi": "uv.lock",
            "pub": "pubspec.lock",
        }.get(spec.get("ecosystem"))
        if filename:
            directory = contained(root, spec["directory"])
            path = contained(root, str((directory / filename).relative_to(root)))
            if not path.is_file():
                raise ValueError(f"Missing resolved dependency lock: {filename}")
    current = lock_identities(root, selected, specs=specs)
    for name in selected:
        spec = module(name, root) if specs is None else specs[name]
        if spec.get("ecosystem") == "go":
            lock_adapters.validate_go_sources(
                root, spec, lock_adapters.identities(root, spec)
            )
        elif spec.get("ecosystem") == "swift":
            lock_adapters.validate_swift_sources(
                root, spec, lock_adapters.identities(root, spec)
            )
    audit_identities(root, current, before, policy, now)


def audit_identities(
    root: Path, current: object, before: object, policy: dict, now: datetime
) -> None:
    """Check actual immutable artifacts; only the observed baseline is age-exempt."""
    current = identity_inventory(current)
    before = identity_inventory(before)
    cutoff = now - timedelta(days=registry.minimum_age(policy))
    evidence = {}
    for identity in sorted(current):
        provider, package, value, url, digest = identity
        if provider == "github-source":
            import javascript_sources

            javascript_sources.audit_identity(identity, before, policy, now)
            continue
        if (
            provider == "npm"
            and registry.version(provider, value) is None
            and identity not in before
        ):
            raise ValueError(
                "A new or changed npm prerelease identity requires explicit project migration"
            )
        safe = registry.minimum_safe(provider, policy, package)
        if safe is not None and registry.lock_version(provider, value) < safe:
            raise ValueError(
                "Locked artifact is below its declared security safe floor"
            )
        key = (provider, package)
        if key not in evidence:
            if provider in ("go", "swift", "maven"):
                items = {item for item in current if item[:2] == key}
                candidates = lock_adapters.evidence(root, provider, package, items)
                if any(
                    e.get("package") == provider + ":" + package
                    for e in policy.get("exceptions", [])
                ):
                    if provider == "go":
                        candidates += lock_adapters.go_candidates(
                            root, package, policy=policy, now=now
                        )
                    elif provider == "maven":
                        source = lock_adapters.maven_source(next(iter(items))[3])
                        candidates += registry.maven_releases(package, source)
                    else:
                        candidates += registry.releases(provider, package)
            else:
                candidates = (
                    registry.releases(
                        provider,
                        package,
                        include_prerelease=True,
                        include_deprecated=True,
                    )
                    if provider == "npm"
                    else registry.releases(provider, package)
                )
            exceptions = registry.active_exceptions(
                provider, candidates, policy, package, now
            )
            evidence[key] = (candidates, {r.version for r in exceptions})
        candidates, exceptions = evidence[key]
        if identity not in before and any(
            r.version == value and r.deprecated for r in candidates
        ):
            raise ValueError(
                f"A new or changed deprecated artifact requires explicit project migration: {provider}:{package}@{value}"
            )
        if not registry.compatible(
            provider, value, registry.constraint(provider, policy, package)
        ):
            raise ValueError(
                f"Resolved dependency violates compatibility constraint: {provider}:{package}@{value}"
            )
        artifacts = [
            a
            for r in candidates
            if r.version == value
            for a in r.artifacts
            if a.digest == digest and (not url or a.url == url)
        ]
        if not artifacts:
            raise ValueError(
                f"Locked artifact identity is absent from registry evidence: {provider}:{package}@{value}"
            )
        # Duplicate metadata must not let an old timestamp hide a newer upload.
        published = max(a.published for a in artifacts)
        if published > now:
            raise ValueError("Future registry artifact publication age")
        if identity not in before and published > cutoff and value not in exceptions:
            raise ValueError(
                f"Resolved artifact is not mature or eligible for an exact exception: {provider}:{package}@{value}"
            )


def uv_resolution_options(policy: dict, now: datetime) -> list[str]:
    """Retain the global cutoff and admit only exact, currently active Python fixes."""
    options = [
        "--exclude-newer",
        (now - timedelta(days=registry.minimum_age(policy))).isoformat(),
    ]
    packages = {
        registry.package_name("pypi", e["package"].partition(":")[2])
        for e in policy.get("exceptions", [])
        if e.get("package", "").startswith("pypi:")
    }
    for package in sorted(packages):
        candidates = registry.releases("pypi", package)
        exceptions = registry.active_exceptions(
            "pypi", candidates, policy, package, now
        )
        if exceptions:
            chosen = max(exceptions, key=lambda r: registry.version("pypi", r.version))
            options.extend(
                [
                    "--exclude-newer-package",
                    f"{package}={now.isoformat()}",
                    "--upgrade-package",
                    f"{package}=={chosen.version}",
                ]
            )
    return options


def configure_uv(root: Path, spec: dict, options: list[str]) -> str:
    # uv records these settings in its lock. Persist the same policy in the
    # declared manifest so ordinary `uv lock --check` remains reproducible.
    path = contained(
        root,
        str((contained(root, spec["directory"]) / "pyproject.toml").relative_to(root)),
    )
    document, render = manifests.document(path)
    table = document.setdefault("tool", {}).setdefault("uv", {})
    table["exclude-newer"] = options[1]
    overrides = {}
    for index, arg in enumerate(options):
        if arg == "--exclude-newer-package":
            package, _, cutoff = options[index + 1].partition("=")
            overrides[package] = cutoff
    if overrides:
        table["exclude-newer-package"] = overrides
    else:
        table.pop("exclude-newer-package", None)
    rendered = render()
    if rendered != path.read_text():
        path.write_text(rendered)
    return rendered


def retain_uv_noop(
    root: Path, spec: dict, old_manifest: str, old_lock: str | None, configured: str
) -> None:
    """Keep a previously reproducible stricter cutoff when only dates advanced."""
    if old_lock is None:
        return
    directory = contained(root, spec["directory"])
    manifest = contained(root, str((directory / "pyproject.toml").relative_to(root)))
    lock = contained(root, str((directory / "uv.lock").relative_to(root)))
    if manifest.read_text() != configured:
        raise ValueError("Python resolver changed its configured project manifest")
    old = tomllib.loads(old_manifest).get("tool", {}).get("uv", {})
    new = tomllib.loads(configured).get("tool", {}).get("uv", {})
    previous, current = tomllib.loads(old_lock), tomllib.loads(lock.read_text())

    def dates(table):
        return {
            "": registry.timestamp(table.get("exclude-newer")),
            **{
                package: registry.timestamp(at)
                for package, at in table.get("exclude-newer-package", {}).items()
            },
        }

    try:
        earlier, later = dates(old), dates(new)
        if earlier != dates(previous.get("options", {})) or later != dates(
            current.get("options", {})
        ):
            return  # An already inconsistent baseline must be repaired, not restored.
        if earlier.keys() != later.keys() or any(
            earlier[p] > later[p] for p in earlier
        ):
            return  # Tighter policy or retired exceptions are meaningful config updates.
    except ValueError:
        return
    for document in (previous, current):
        options = document.get("options", {})
        options.pop("exclude-newer", None)
        options.pop("exclude-newer-package", None)
        if not options:
            document.pop("options", None)
    if previous == current:
        manifest.write_text(old_manifest)
        lock.write_text(old_lock)


def resolve(root: Path, now: datetime, selected: list[str]) -> None:
    import module_updates

    module_updates.resolve(root, selected, settings(root), now)


def perform(root: Path, now: datetime, selected: list[str]) -> None:
    env = environment(root)
    import module_updates
    import source_updates

    policy = settings(root)
    spec = module_updates.nix_spec(policy)
    before = source_updates.snapshot(root, spec) if spec is not None else None
    if spec is not None:
        source_updates.resolve(root, spec, policy, now)
    env["TOOLCHAIN_FRESH"] = "1"
    # Resolve with the updated Python, package managers and SDKs, not the parent shell.
    managed_run(
        [
            *entry_command(root, "core"),
            "python3",
            str(RUNTIME / "scripts/updates.py"),
            "--resolve-at",
            now.isoformat(),
            "--modules",
            ",".join(selected),
        ],
        cwd=root,
        env=env,
        check=True,
    )

    if spec is not None:
        source_updates.audit(root, spec, before, policy, now)


def verify(root: Path, selected: list[str]) -> None:
    env = environment(root)
    env["TOOLCHAIN_FRESH"] = "1"
    env["CHAINMAN_UPDATE_ACTIVE"] = "1"
    sdk_versions.synchronize(root, selected, check=True)
    manifests.configure_build_dependencies(root, selected, check=True)
    for name in selected:
        spec = module(name, root)
        setup(spec, env, root)
        run_commands(spec, "verify", env, root)


def preview(root: Path, now: datetime, selected: list[str]) -> dict:
    repository(root, clean=False)
    before = snapshot(root)
    patterns = settings(root)["outputs"] + [
        p for n in selected for p in module(n, root).get("update_outputs", [])
    ]
    with (
        tempfile.TemporaryDirectory(prefix="toolchain-preview-") as tmp,
        preview_git_environment(),
    ):
        copy = Path(tmp)
        prepare_preview(root, copy, before)
        result = transaction(
            copy,
            patterns,
            lambda: perform(copy, now, selected),
            lambda: verify(copy, selected),
            False,
        )
        return {"preview": True, **result}


def main() -> int:
    if "--resolve-at" not in sys.argv[1:]:
        import source_workflow

        source_workflow.run(ROOT, "deps-update", sys.argv[1:])
        return 0
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--resolve-at", help=argparse.SUPPRESS)
    parser.add_argument("--modules", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        resolve(ROOT, registry.timestamp(args.resolve_at), args.modules.split(","))
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(
            f"Dependency update failed: {exc}. Existing changes are preserved; no reset, stash, or push was performed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
