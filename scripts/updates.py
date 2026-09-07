"""Resolve, verify, and commit a precisely scoped local dependency update."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlencode

import manifests
import lock_adapters
import sdk_versions
import registry
from toolchain import (
    ROOT,
    atomic_json,
    config,
    contained,
    local_source,
    environment,
    managed_run,
    module,
    operation,
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
            GIT_CONFIG_COUNT="0",
            GIT_TERMINAL_PROMPT="0",
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


def snapshot(root: Path) -> dict[str, str]:
    paths = git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    ).split("\0")
    result = {}
    for name in paths:
        if not name:
            continue
        path = root / name
        if path.is_symlink():
            body = b"symlink\0" + os.readlink(path).encode()
        elif path.is_file():
            body = str(path.stat().st_mode & 0o777).encode() + b"\0" + path.read_bytes()
        elif not path.exists():
            body = b"deleted"
        else:
            # Submodules are not inputs to this updater's file transaction.
            body = b"directory"
        result[name] = hashlib.sha256(body).hexdigest()
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


def tree_entries(root: Path, revision: str) -> dict:
    entries = {}
    for record in git(root, "ls-tree", "-r", "-z", "--full-tree", revision).split("\0"):
        if record:
            metadata, name = record.split("\t", 1)
            mode, _, identity = metadata.split()
            entries[name] = (mode, identity)
    return entries


def raw_entries(root: Path, names) -> dict:
    """Read actual tracked contents without Git stat-cache or clean-filter decisions."""
    entries = {}
    for name in names:
        relative = Path(name)
        path = contained(root, str(relative.parent)) / relative.name
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
                "Exact tracked-byte verification requires files or symlinks; submodule/directory sources are unsupported"
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


def expected_entries(root: Path, head: str, paths: list[str]) -> dict:
    for name in paths:
        path = contained(root, name)
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError("Dependency outputs must be regular files")
    # Re-read every tracked source, including paths that the updater did not change.
    return raw_entries(root, tree_entries(root, head).keys() | set(paths))


def staged_entries(root: Path) -> dict:
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


def update_nix(root: Path, policy: dict, now: datetime, env: dict) -> None:
    if not policy.get("nix", {}).get("enabled", True):
        return
    # Nix branch inputs are commits, not published releases. This is explicitly
    # revision age; no tag publication age is inferred from Git commit timestamps.
    spec = policy.get("nix", {})
    relative = spec.get("directory", "nix")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Nix input directory must be a project-relative path")
    directory = contained(root, relative)
    if not directory.is_dir():
        raise ValueError("Nix input directory must be an existing project directory")
    lockpath = contained(root, str(Path(relative) / "flake.lock"))
    contained(root, str(Path(relative) / "flake.nix"))
    repository_name = spec.get("repository", "NixOS/nixpkgs")
    branch = spec.get("branch", "nixos-unstable")
    cutoff = now - timedelta(days=policy.get("minimum_age_days", 30))
    query = urlencode({"sha": branch, "until": cutoff.isoformat(), "per_page": 1})
    candidates = registry.data(
        f"https://api.github.com/repos/{repository_name}/commits?{query}"
    )
    if not candidates:
        raise ValueError("No mature Nix revision is available")
    selected = candidates[0]
    at = registry.timestamp(selected["commit"]["committer"]["date"])
    if at > cutoff or not re.fullmatch(r"[a-f0-9]{40}", selected["sha"]):
        raise ValueError("Invalid Nix revision-age evidence")
    lock = json.loads(lockpath.read_text())
    name = spec.get("input", "nixpkgs")
    key = lock["nodes"][lock["root"]]["inputs"][name]
    current = lock["nodes"][key]["locked"]
    if current["lastModified"] >= at.timestamp():
        return
    managed_run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "flake",
            "lock",
            "path:.",
            "--override-input",
            name,
            f"github:{repository_name}/{selected['sha']}",
        ],
        cwd=directory,
        env=env,
        check=True,
    )


def current_version(pin: dict, root: Path):
    if pin.get("format") == "regex":
        matches = list(
            re.finditer(
                pin["pattern"], contained(root, pin["file"]).read_text(), re.MULTILINE
            )
        )
        if len(matches) != 1:
            raise ValueError("Explicit pin must match once")
        raw = matches[0].group("value")
        if pin.get("representation") == "action":
            raw = raw.split(" # ", 1)[1]
    else:
        content = manifests.document(contained(root, pin["file"]))[0]
        raw = manifests.lookup(content, pin["pointer"])
        if pin.get("representation") == "requirement":
            from packaging.requirements import Requirement

            bounds = [
                s.version
                for s in Requirement(raw).specifier
                if s.operator in (">=", "==", "~=")
            ]
            raw = bounds[0] if len(bounds) == 1 else ""
        else:
            raw = raw.removeprefix(pin.get("prefix", ""))
    return registry.version(pin["provider"], raw)


def lock_identities(
    root: Path, selected: list[str], *, specs: dict | None = None
) -> set[tuple[str, str, str, str, str]]:
    """One identity per actual distribution, with explicit URLs where the lock has them."""
    identities = set()

    def add(provider, package, value, url, digest):
        if not package or registry.version(provider, value) is None:
            raise ValueError("Unrecognized stable lock identity")
        identities.add(
            (
                provider,
                registry.package_name(provider, package),
                value,
                registry.artifact_url(url) if url else "",
                digest,
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
            entries = lock.get("packages", {})
            if spec.get("retained_sources"):
                import javascript_sources

                entries = javascript_sources.registry_entries(root, spec, lock)
                identities.update(javascript_sources.lock_identities(root, spec, lock))
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
    root: Path, current: set, before: set, policy: dict, now: datetime
) -> None:
    """Check actual immutable artifacts; only the observed baseline is age-exempt."""
    cutoff = now - timedelta(days=registry.minimum_age(policy))
    evidence = {}
    for identity in sorted(current):
        provider, package, value, url, digest = identity
        if provider == "github-source":
            import javascript_sources

            javascript_sources.audit_identity(identity, before, policy, now)
            continue
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
                        candidates += lock_adapters.go_candidates(root, package)
                    elif provider == "maven":
                        source = lock_adapters.maven_source(next(iter(items))[3])
                        candidates += registry.maven_releases(package, source)
                    else:
                        candidates += registry.releases(provider, package)
            else:
                candidates = registry.releases(provider, package)
            exceptions = registry.active_exceptions(
                provider, candidates, policy, package, now
            )
            evidence[key] = (candidates, {r.version for r in exceptions})
        candidates, exceptions = evidence[key]
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
    policy = settings(root)
    env = environment(root)
    env["TOOLCHAIN_FRESH"] = "1"
    manifests.configure_build_dependencies(root, selected, validate_only=True)
    sdk_versions.synchronize(root, selected)
    before = lock_identities(root, selected)
    pins = manifests.discover(root, selected)
    pins.extend(
        pin
        for pin in policy.get("pins", [])
        if not pin.get("module") or pin["module"] in selected
    )
    seen = set()
    for pin in pins:
        target = (pin["file"], str(pin.get("pointer", pin.get("pattern"))))
        if target in seen:
            raise ValueError(
                "Duplicate dependency target; use one authoritative declaration"
            )
        seen.add(target)
        provider, name = pin["provider"], pin["name"]
        if provider == "go":
            if pin.get("format") != "regex" or not pin["file"].endswith("go.mod"):
                raise ValueError(
                    "Go updates require an explicit exact go.mod version regex pin"
                )
            candidates = lock_adapters.go_candidates(root, name)
        elif provider == "maven" and pin.get("module"):
            candidates = registry.maven_releases(
                name, lock_adapters.maven_repository(module(pin["module"], root), name)
            )
        else:
            candidates = registry.releases(provider, name)
        chosen = registry.select(provider, candidates, policy, name, now)
        if provider == "swift" and (
            pin.get("format") != "regex"
            or not pin["file"].endswith("Package.swift")
            or pin.get("identity")
        ):
            raise ValueError(
                "SwiftPM updates require an explicit exact Package.swift version regex pin"
            )
        old = current_version(pin, root)
        if old is not None and registry.version(provider, chosen.version) <= old:
            continue
        if provider == "github" and (
            pin.get("identity") or pin.get("representation") == "action"
        ):
            chosen = registry.Release(
                chosen.version,
                chosen.published,
                registry.github_commit(name, chosen.version),
            )
        manifests.replace(pin, chosen, root)
        print(f"Selected {provider}:{name}@{chosen.version}")
    image_spec = policy.get("docker", {})
    if image_spec.get("enabled", True):
        package = image_spec.get("repository", "nixos/nix")
        chosen = registry.select(
            "docker", registry.releases("docker", package), policy, package, now
        )
        path = contained(root, "nix/container-image.txt")
        current = path.read_text().strip()
        old = registry.version("docker", current.split("@", 1)[0].rsplit(":", 1)[-1])
        if old is None:
            raise ValueError("Current container image lacks a stable version tag")
        if registry.version("docker", chosen.version) >= old:
            image = f"docker.io/{package}:{chosen.version}@{chosen.identity}"
            bootstrap = contained(root, "bootstrap/chainman.sh")
            if bootstrap.exists():
                content, count = re.subn(
                    r"(?m)^image=\S+$",
                    lambda _: "image=" + image,
                    bootstrap.read_text(),
                )
                if count != 1:
                    raise ValueError(
                        "Source bootstrap must declare exactly one managed image"
                    )
                bootstrap.write_text(content)
            path.write_text(image + "\n")
    manifests.configure_build_dependencies(root, selected)
    for name in selected:
        spec = module(name, root)
        if "resolve" in spec.get("commands", {}):
            if spec.get("ecosystem") == "pypi":
                options = uv_resolution_options(policy, now)
                commands = spec["commands"]["resolve"]
                if any(argv[:2] != ["uv", "lock"] for argv in commands):
                    raise ValueError(
                        "Python resolution requires explicit uv lock commands for scoped age policy"
                    )
                directory = contained(root, spec["directory"])
                manifest = contained(
                    root, str((directory / "pyproject.toml").relative_to(root))
                )
                lock = contained(root, str((directory / "uv.lock").relative_to(root)))
                old_manifest, old_lock = (
                    manifest.read_text(),
                    lock.read_text() if lock.exists() else None,
                )
                configured = configure_uv(root, spec, options)
                spec = {
                    **spec,
                    "commands": {
                        **spec["commands"],
                        "resolve": [argv + options for argv in commands],
                    },
                }
            run_commands(spec, "resolve", env, root)
            if spec.get("ecosystem") == "pypi":
                retain_uv_noop(root, spec, old_manifest, old_lock, configured)
    audit_locks(root, selected, before, policy, now)


def perform(root: Path, now: datetime, selected: list[str]) -> None:
    env = environment(root)
    update_nix(root, settings(root), now, env)
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


def verify(root: Path, selected: list[str]) -> None:
    env = environment(root)
    env["TOOLCHAIN_FRESH"] = "1"
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
        for name in before:
            source = contained(root, name)
            if source.is_file():
                target = copy / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            elif source.exists():
                raise ValueError(
                    "Preview requires regular project files; external submodules must have their own update transaction"
                )
        # This disposable baseline records the visible source inventory, including
        # tracked inputs under ignored build directory names. No parent history,
        # remotes, hooks, credentials, or repository identity is copied.
        git(copy, "init", "-b", "preview")
        files = [name for name in before if (copy / name).is_file()]
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
        result = transaction(
            copy,
            patterns,
            lambda: perform(copy, now, selected),
            lambda: verify(copy, selected),
            False,
        )
        return {"preview": True, **result}


def main() -> int:
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--no-commit", action="store_true")
    parser.add_argument("--resolve-at", help=argparse.SUPPRESS)
    parser.add_argument("--modules", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        selected = config(ROOT)["modules"]
        if args.resolve_at:
            resolve(ROOT, registry.timestamp(args.resolve_at), args.modules.split(","))
            return 0
        now = datetime.now(timezone.utc)
        with operation(ROOT):
            if args.preview:
                result = preview(ROOT, now, selected)
            else:
                policy = settings(ROOT)
                patterns = policy["outputs"] + [
                    p
                    for n in selected
                    for p in module(n, ROOT).get("update_outputs", [])
                ]
                result = transaction(
                    ROOT,
                    patterns,
                    lambda: perform(ROOT, now, selected),
                    lambda: verify(ROOT, selected),
                    not args.no_commit,
                )
            atomic_json(
                contained(ROOT, ".cache/toolchain/last-update.json"),
                {"at": now.isoformat(), **result},
            )
            print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(
            f"Dependency update failed: {exc}. Existing changes are preserved; no reset, stash, or push was performed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
