"""Declared Go workspaces, strict update bounds, and public checksum evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote
from semantic_version import Version

import chainman
import registry
import toolchain as tc
import adapter_data as ad


class Snapshot(TypedDict):
    adapter: str
    members: dict[str, ad.Table]
    workspaces: dict[str, ad.Table]
    identities: list[list[str]]
    files: dict[str, str]


def documents(value: object) -> dict[str, ad.Table]:
    return {
        name: ad.table(body, "Go document")
        for name, body in ad.table(value, "Go documents").items()
    }


def identities(value: object) -> set[tuple[str, str, str, str]]:
    result: set[tuple[str, str, str, str]] = set()
    for raw in ad.array(value, "Go identities"):
        item = ad.strings(raw, "Go identity")
        if len(item) != 4:
            raise ValueError("Go identity requires package, version, URL and digest")
        result.add((item[0], item[1], item[2], item[3]))
    return result


def execute(
    root: Path,
    spec: Mapping[str, object],
    argv: list[str],
    *,
    directory: Path | None = None,
    output: bool = False,
    workspace: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = tc.environment(root)
    env.update(
        GOENV="off",
        GOFLAGS="",
        GOWORK="off",
        GO111MODULE="on",
        GOPROXY="https://proxy.golang.org",
        GOSUMDB="sum.golang.org",
        GOPRIVATE="",
        GONOPROXY="",
        GONOSUMDB="",
        GOVCS="*:off",
        GOTOOLCHAIN="local",
    )
    if workspace is not None:
        env["GOWORK"] = str(tc.contained(root, workspace))
    return chainman.execute(
        root,
        ad.text(spec.get("profile", "go"), "Go profile"),
        argv,
        env=env,
        cwd=root if directory is None else directory,
        text=True,
        stdout=subprocess.PIPE if output else None,
    )


def native_json(root: Path, spec: Mapping[str, object], argv: list[str]) -> ad.Table:
    with tempfile.TemporaryDirectory(prefix="chainman Go evidence ") as temporary:
        result = execute(root, spec, argv, directory=Path(temporary), output=True)
    return ad.table(json.loads(result.stdout), "Go command result")


def query(
    root: Path, spec: Mapping[str, object], package: str, version: str
) -> ad.Table:
    registry.go_path(package)
    if version != "latest" and not registry.go_version(version):
        raise ValueError("Invalid Go query version")
    flags = ["-versions"] if version == "latest" else ["-retracted"]
    result = native_json(
        root, spec, ["go", "list", "-m", "-json", *flags, package + "@" + version]
    )
    if (
        result.get("Path") != package
        or result.get("Error")
        or (version != "latest" and result.get("Version") != version)
    ):
        raise ValueError("Go returned an unexpected module identity")
    return result


def go_candidates(
    root: Path,
    spec: Mapping[str, object],
    package: str,
    *,
    policy: Mapping[str, object] | None = None,
    now: datetime | None = None,
    bounds: tuple[str, ...] = (),
    exact: str | None = None,
) -> list[registry.Release]:
    available = query(root, spec, package, "latest").get("Versions")
    if not isinstance(available, list):
        raise ValueError("Go did not return its unretracted release inventory")  # noqa: TRY004 - decoded external data
    return registry.go_releases(
        package,
        ad.strings(available, "Go versions"),
        policy=policy,
        now=now,
        bounds=bounds,
        exact=exact,
    )


def local_directory(root: Path, base: Path, value: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("Go local source needs an explicit directory")
    path = Path(value)
    current = Path(path.anchor) if path.is_absolute() else base
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        if current.is_symlink():
            raise ValueError("Go source traverses a symlink")
        current /= part
        if current.is_symlink():
            raise ValueError("Go source traverses a symlink")
    location = current.resolve()
    if not location.is_relative_to(root.resolve()):
        raise ValueError("Go local source escapes the adopted project")
    location = tc.contained(root, str(location.relative_to(root.resolve())))
    if not location.is_dir():
        raise ValueError("Go local source is not a directory")
    return location


def local_module_path(module: str) -> None:
    # Go checks main/local module names as import paths, not public fetch paths.
    # Match x/mod/module.CheckImportPath while keeping registry.go_path strict.
    if (
        not isinstance(module, str)
        or not re.fullmatch(r"[A-Za-z0-9._~+/-]+", module)
        or module.startswith("-")
    ):
        raise ValueError("Invalid local Go module identity")
    for part in module.split("/"):
        short = part.split(".", 1)[0]
        if (
            not part
            or part.endswith(".")
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", short)
            or re.search(r"~[0-9]+$", short)
        ):
            raise ValueError("Invalid local Go module identity")


def metadata(
    root: Path, spec: Mapping[str, object]
) -> tuple[dict[str, ad.Table], dict[str, ad.Table]]:
    directories = spec.get("directories")
    if not isinstance(directories, list) or not directories:
        raise ValueError("Go requires explicit module or workspace directories")
    members: dict[str, ad.Table] = {}
    workspaces: dict[str, ad.Table] = {}
    pending: set[Path] = set()
    for relative in ad.strings(directories, "Go directories"):
        directory = tc.contained(root, relative)
        if (directory / "go.work").exists():
            name = str((directory / "go.work").relative_to(root))
            tc.regular_input(root, name)
            work = native_json(
                root, spec, ["go", "work", "edit", "-json", str(directory / "go.work")]
            )
            workspaces[name] = work
            for path in ad.GoFile.decode(work, workspace=True).uses:
                pending.add(local_directory(root, directory, path))
        elif (directory / "go.mod").exists():
            pending.add(directory)
        else:
            raise ValueError("Declared Go directory lacks go.mod or go.work")
    if not pending:
        raise ValueError("Declared Go workspace has no modules")
    for directory in sorted(pending):
        name = str((directory / "go.mod").relative_to(root))
        tc.regular_input(root, name)
        body = native_json(
            root, spec, ["go", "mod", "edit", "-json", str(directory / "go.mod")]
        )
        module = ad.GoFile.decode(body).module
        assert module is not None
        local_module_path(module)
        if any(ad.GoFile.decode(m).module == module for m in members.values()):
            raise ValueError("Go workspace has duplicate module identities")
        members[str(directory.relative_to(root))] = body
    # Local replacements are deliberate source bindings, never public releases.
    # Remote replacements need a separate exact source contract rather than bypassing audit.
    for name, body in [*members.items(), *workspaces.items()]:
        base = tc.contained(
            root, str(Path(name).parent) if name.endswith("go.work") else name
        )
        for entry in ad.GoFile.decode(
            body, workspace=name.endswith("go.work")
        ).replacements:
            target = entry.new
            if target.version:
                raise ValueError(
                    "Remote Go replacements require an explicit source contract"
                )
            location = local_directory(root, base, target.path)
            if str(location.relative_to(root)) not in members:
                raise ValueError(
                    "Go local replacement is not a declared workspace module"
                )
    return members, workspaces


def checksum_identities(
    root: Path, members: Mapping[str, object], workspaces: Mapping[str, object]
) -> list[list[str]]:
    names = {str(Path(name) / "go.sum") for name in members}
    names.update(name + ".sum" for name in workspaces)
    identities = set()
    for name in sorted(names):
        if not tc.contained(root, name).exists():
            continue
        for line in tc.regular_input(root, name).decode().splitlines():
            parts = line.split()
            if len(parts) != 3:
                raise ValueError("Malformed Go checksum entry")
            package, raw, digest = parts
            value = raw.removesuffix("/go.mod")
            if not registry.go_version(value):
                raise ValueError("Noncanonical Go checksum version")
            suffix = ".mod" if raw.endswith("/go.mod") else ".zip"
            url = f"https://proxy.golang.org/{registry.go_path(package)}/@v/{quote(value, safe='')}{suffix}"
            identities.add((package, value, url, registry.go_digest(digest)))
    return [list(item) for item in sorted(identities)]


def snapshot(root: Path, spec: Mapping[str, object]) -> Snapshot:
    members, workspaces = metadata(root, spec)
    paths = {
        str(Path(name) / filename)
        for name in members
        for filename in ("go.mod", "go.sum")
    }
    paths.update(workspaces)
    paths.update(name + ".sum" for name in workspaces)
    files = {
        name: hashlib.sha256(tc.regular_input(root, name)).hexdigest()
        for name in sorted(paths)
        if tc.contained(root, name).exists()
    }
    return {
        "adapter": "go",
        "members": members,
        "workspaces": workspaces,
        "identities": checksum_identities(root, members, workspaces),
        "files": files,
    }


def workspace_for(
    root: Path, directory: Path, state: Mapping[str, object]
) -> str | None:
    selected = [
        name
        for name, body in documents(state["workspaces"]).items()
        if any(
            local_directory(root, tc.contained(root, str(Path(name).parent)), path)
            == directory
            for path in ad.GoFile.decode(body, workspace=True).uses
        )
    ]
    if len(selected) > 1:
        raise ValueError("One Go module is selected through multiple workspaces")
    return selected[0] if selected else None


def compatible_bound(current: str) -> str:
    rank = registry.version("go", current)
    if rank is None:
        raise ValueError("Compatible Go updates require a stable current version")
    return f">={rank.major}.{rank.minor}.0 <{rank.major}.{rank.minor + 1}.0"


def select(
    root: Path,
    spec: Mapping[str, object],
    package: str,
    current: str,
    policy: Mapping[str, object],
    now: datetime,
) -> registry.Release:
    mode = spec.get("mode", "aggressive")
    if mode not in ("aggressive", "compatible"):
        raise ValueError("Go mode must be aggressive or compatible")
    bounds = (compatible_bound(current),) if mode == "compatible" else ()
    candidates = go_candidates(
        root, spec, package, policy=policy, now=now, bounds=bounds
    )
    if mode == "compatible":
        bound = compatible_bound(current)
        candidates = [
            release
            for release in candidates
            if registry.compatible("go", release.version, bound)
        ]
    return registry.select("go", candidates, policy, package, now)


def resolve(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object], now: datetime
) -> ad.Table:
    before = snapshot(root, spec)
    local = {ad.GoFile.decode(body).module for body in before["members"].values()}
    selections: list[dict[str, str]] = []
    for name, body in before["members"].items():
        arguments = []
        for item in ad.GoFile.decode(body).requires:
            package, current = item.path, item.version
            if package in local:
                continue
            registry.go_path(package)
            if registry.version("go", current) is None:
                # Existing exact pseudoversions remain explicit pins. A changed
                # pseudoversion introduced by a hook still undergoes the final audit.
                if not registry.go_version(current):
                    raise ValueError("Unsupported Go requirement version")
                selections.append(
                    {
                        "package": package,
                        "version": current,
                        "reason": "explicit non-release pin",
                    }
                )
                continue
            chosen = select(root, spec, package, current, policy, now)
            if registry.stable_version("go", chosen.version) > registry.stable_version(
                "go", current
            ):
                arguments.append(package + "@" + chosen.version)
            selections.append(
                {
                    "package": package,
                    "version": chosen.version,
                    "published": chosen.published.isoformat(),
                }
            )
        directory = tc.contained(root, name)
        workspace = workspace_for(root, directory, before)
        if arguments:
            execute(
                root,
                spec,
                ["go", "get", *arguments],
                directory=directory,
                workspace=workspace,
            )
        execute(
            root, spec, ["go", "mod", "tidy"], directory=directory, workspace=workspace
        )
    audit(root, spec, before, policy, now)
    after = snapshot(root, spec)
    changed = [
        name
        for name in sorted(before["files"].keys() | after["files"].keys())
        if before["files"].get(name) != after["files"].get(name)
    ]
    return {"changed": changed, "selected": selections}


def audit(
    root: Path,
    spec: Mapping[str, object],
    before: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> None:
    if spec.get("mode", "aggressive") not in ("aggressive", "compatible"):
        raise ValueError("Go mode must be aggressive or compatible")
    after = snapshot(root, spec)
    before_members = documents(before["members"])
    if (
        before_members.keys() != after["members"].keys()
        or before["workspaces"] != after["workspaces"]
    ):
        raise ValueError(
            "Go workspace membership or source bindings changed during resolution"
        )
    old_versions: dict[str, set[str]] = {}
    for name, body in before_members.items():
        current = after["members"][name]
        if body.get("Module") != current.get("Module") or body.get(
            "Replace"
        ) != current.get("Replace"):
            raise ValueError(
                "Go module or replacement identity changed during resolution"
            )
        for item in ad.GoFile.decode(body).requires:
            old_versions.setdefault(item.path, set()).add(item.version)
    observed = identities(after["identities"])
    added = observed - identities(before["identities"])
    local = {ad.GoFile.decode(body).module for body in after["members"].values()}

    def check_policy(package: str, value: str) -> None:
        floor = registry.minimum_safe("go", policy, package)
        if floor is not None and Version(value.removeprefix("v")) < floor:
            raise ValueError("Go identity is below its declared security safe floor")
        if not registry.compatible(
            "go", value, registry.constraint("go", policy, package)
        ):
            raise ValueError("Go identity violates a declared constraint")

    for package, value, _, _ in observed:
        check_policy(package, value)
    public_requirements = [
        item
        for body in after["members"].values()
        for item in ad.GoFile.decode(body).requires
        if item.path not in local
    ]
    for item in public_requirements:
        registry.go_path(item.path)
        check_policy(item.path, item.version)
    public_packages = {identity[0] for identity in observed} | {
        item.path for item in public_requirements
    }

    def selection_bounds(package: str) -> tuple[str, ...]:
        return (
            tuple(
                compatible_bound(previous)
                for previous in old_versions.get(package, ())
                if registry.version("go", previous) is not None
            )
            if spec.get("mode", "aggressive") == "compatible"
            else ()
        )

    for package in sorted(public_packages):
        registry.minimum_safe("go", policy, package)
        if not any(
            ad.table(item, "Go exception").get("package") == "go:" + package
            for item in ad.array(policy.get("exceptions", []), "Go exceptions")
        ):
            continue
        candidates = go_candidates(
            root,
            spec,
            package,
            policy=policy,
            now=now,
            bounds=selection_bounds(package),
        )
        if spec.get("mode", "aggressive") == "compatible":
            for previous in old_versions.get(package, ()):
                if registry.version("go", previous) is not None:
                    candidates = [
                        item
                        for item in candidates
                        if registry.compatible(
                            "go", item.version, compatible_bound(previous)
                        )
                    ]
        registry.active_exceptions("go", candidates, policy, package, now)
    for name, body in after["members"].items():
        previous_requirements = {
            item.path: item.version
            for item in ad.GoFile.decode(before_members[name]).requires
        }
        for item in ad.GoFile.decode(body).requires:
            package, value = item.path, item.version
            if package in local:
                continue
            check_policy(package, value)
            if previous_requirements.get(package) == value:
                continue
            # Existing indirect lock entries cannot bypass eligibility when promoted
            # to a newly selected requirement by resolution or reconciliation.
            evidence = {i for i in observed if i[:2] == (package, value)}
            if not evidence:
                raise ValueError("Changed Go requirement lacks checksum evidence")
            added.update(evidence)
            old, new = (
                registry.version("go", previous_requirements.get(package, "")),
                registry.version("go", value),
            )
            if old is not None and new is not None and new < old:
                raise ValueError("Go resolution downgraded a declared requirement")
    required_versions = {(item.path, item.version) for item in public_requirements}
    for package, value in sorted(required_versions):
        if query(root, spec, package, value).get("Retracted"):
            raise ValueError("Required Go module version is retracted")
    for package in sorted({item[0] for item in added}):
        items = [item for item in added if item[0] == package]
        releases = []
        for value in sorted({item[1] for item in items}):
            if (package, value) not in required_versions and query(
                root, spec, package, value
            ).get("Retracted"):
                raise ValueError(
                    "New Go checksum identity refers to a retracted release"
                )
            artifacts = registry.go_artifacts(package, value)
            releases.append(
                registry.Release(
                    value, max(a.published for a in artifacts), artifacts=artifacts
                )
            )
        for _, value, url, digest in items:
            release = next(item for item in releases if item.version == value)
            if spec.get("mode", "aggressive") == "compatible":
                for previous in old_versions.get(package, ()):
                    if registry.version(
                        "go", previous
                    ) is not None and not registry.compatible(
                        "go", value, compatible_bound(previous)
                    ):
                        raise ValueError(
                            "Go resolution escaped the strict compatible bound"
                        )
            artifact = next(
                (a for a in release.artifacts if a.url == url and a.digest == digest),
                None,
            )
            if artifact is None:
                raise ValueError(
                    "Go checksum differs from its public immutable evidence"
                )
            # Pseudoversions may be resolver-selected transitives, but still need
            # dated checksum evidence. They are never stable direct-update candidates.
            limit = now - timedelta(days=registry.minimum_age(policy))
            bound = registry.constraint("go", policy, package)
            if not registry.compatible("go", value, bound):
                raise ValueError("Go checksum identity violates a declared constraint")
            if artifact.published > now:
                raise ValueError("Go artifact has future age evidence")
            if artifact.published > limit:
                allowed = registry.active_exceptions(
                    "go",
                    go_candidates(
                        root,
                        spec,
                        package,
                        policy=policy,
                        now=now,
                        bounds=selection_bounds(package),
                    ),
                    policy,
                    package,
                    now,
                )
                if value not in {r.version for r in allowed}:
                    raise ValueError("New Go checksum identity lacks mature evidence")
    # Native Go validates the checksum database and downloaded module bytes;
    # parsing its public lookup response alone is not a transparency proof.
    for name in after["members"]:
        directory = tc.contained(root, name)
        workspace = workspace_for(root, directory, after)
        execute(
            root,
            spec,
            ["go", "mod", "download"],
            directory=directory,
            workspace=workspace,
        )
        execute(
            root,
            spec,
            ["go", "mod", "verify"],
            directory=directory,
            workspace=workspace,
        )
    if snapshot(root, spec) != after:
        raise ValueError("Go evidence verification changed dependency inputs")
