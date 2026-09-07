"""Declared Go workspaces, strict update bounds, and public checksum evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import chainman
import registry
import toolchain as tc


def execute(
    root: Path,
    spec: dict,
    argv: list[str],
    *,
    directory=None,
    output=False,
    workspace=None,
):
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
        spec.get("profile", "go"),
        argv,
        env=env,
        cwd=root if directory is None else directory,
        text=True,
        **({"stdout": subprocess.PIPE} if output else {}),
    )


def native_json(root: Path, spec: dict, argv: list[str]) -> dict:
    with tempfile.TemporaryDirectory(prefix="chainman Go evidence ") as temporary:
        result = execute(root, spec, argv, directory=Path(temporary), output=True)
    return json.loads(result.stdout)


def query(root: Path, spec: dict, package: str, version: str) -> dict:
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


def go_candidates(root: Path, spec: dict, package: str) -> list[registry.Release]:
    releases = registry.releases("go", package)
    available = query(root, spec, package, "latest").get("Versions")
    if not isinstance(available, list):
        raise ValueError("Go did not return its unretracted release inventory")  # noqa: TRY004 - decoded external data
    return [release for release in releases if release.version in available]


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


def metadata(root: Path, spec: dict) -> tuple[dict, dict]:
    directories = spec.get("directories")
    if not isinstance(directories, list) or not directories:
        raise ValueError("Go requires explicit module or workspace directories")
    members, workspaces = {}, {}
    pending = set()
    for relative in directories:
        directory = tc.contained(root, relative)
        if (directory / "go.work").exists():
            name = str((directory / "go.work").relative_to(root))
            tc.regular_input(root, name)
            work = native_json(
                root, spec, ["go", "work", "edit", "-json", str(directory / "go.work")]
            )
            workspaces[name] = work
            for item in work.get("Use") or []:
                pending.add(local_directory(root, directory, item["DiskPath"]))
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
        module = body.get("Module", {}).get("Path")
        registry.go_path(module)
        if any(m.get("Module", {}).get("Path") == module for m in members.values()):
            raise ValueError("Go workspace has duplicate module identities")
        members[str(directory.relative_to(root))] = body
    # Local replacements are deliberate source bindings, never public releases.
    # Remote replacements need a separate exact source contract rather than bypassing audit.
    for name, body in [*members.items(), *workspaces.items()]:
        base = tc.contained(
            root, str(Path(name).parent) if name.endswith("go.work") else name
        )
        for entry in body.get("Replace") or []:
            target = entry["New"]
            if target.get("Version"):
                raise ValueError(
                    "Remote Go replacements require an explicit source contract"
                )
            location = local_directory(root, base, target["Path"])
            if str(location.relative_to(root)) not in members:
                raise ValueError(
                    "Go local replacement is not a declared workspace module"
                )
    return members, workspaces


def checksum_identities(root: Path, members: dict, workspaces: dict) -> list[list[str]]:
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


def snapshot(root: Path, spec: dict) -> dict:
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


def workspace_for(root: Path, directory: Path, state: dict):
    selected = [
        name
        for name, body in state["workspaces"].items()
        if any(
            local_directory(
                root, tc.contained(root, str(Path(name).parent)), entry["DiskPath"]
            )
            == directory
            for entry in body.get("Use") or []
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
    root: Path, spec: dict, package: str, current: str, policy: dict, now: datetime
) -> registry.Release:
    mode = spec.get("mode", "aggressive")
    if mode not in ("aggressive", "compatible"):
        raise ValueError("Go mode must be aggressive or compatible")
    candidates = go_candidates(root, spec, package)
    if mode == "compatible":
        bound = compatible_bound(current)
        candidates = [
            release
            for release in candidates
            if registry.compatible("go", release.version, bound)
        ]
    return registry.select("go", candidates, policy, package, now)


def resolve(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    before = snapshot(root, spec)
    local = {body["Module"]["Path"] for body in before["members"].values()}
    selections = []
    for name, body in before["members"].items():
        arguments = []
        for item in body.get("Require") or []:
            package, current = item["Path"], item["Version"]
            if package in local:
                continue
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
            if registry.version("go", chosen.version) > registry.version("go", current):
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


def audit(root: Path, spec: dict, before: dict, policy: dict, now: datetime) -> None:
    if spec.get("mode", "aggressive") not in ("aggressive", "compatible"):
        raise ValueError("Go mode must be aggressive or compatible")
    after = snapshot(root, spec)
    if (
        before["members"].keys() != after["members"].keys()
        or before["workspaces"] != after["workspaces"]
    ):
        raise ValueError(
            "Go workspace membership or source bindings changed during resolution"
        )
    old_versions = {}
    for name, body in before["members"].items():
        current = after["members"][name]
        if body.get("Module") != current.get("Module") or body.get(
            "Replace"
        ) != current.get("Replace"):
            raise ValueError(
                "Go module or replacement identity changed during resolution"
            )
        for item in body.get("Require") or []:
            old_versions.setdefault(item["Path"], set()).add(item["Version"])
    identities = set(map(tuple, after["identities"]))
    added = identities - set(map(tuple, before["identities"]))
    local = {body["Module"]["Path"] for body in after["members"].values()}
    for name, body in after["members"].items():
        previous = {
            item["Path"]: item["Version"]
            for item in before["members"][name].get("Require") or []
        }
        for item in body.get("Require") or []:
            package, value = item["Path"], item["Version"]
            if package in local or previous.get(package) == value:
                continue
            # Existing indirect lock entries cannot bypass eligibility when promoted
            # to a newly selected requirement by resolution or reconciliation.
            evidence = {i for i in identities if i[:2] == (package, value)}
            if not evidence:
                raise ValueError("Changed Go requirement lacks checksum evidence")
            added.update(evidence)
            old, new = (
                registry.version("go", previous.get(package, "")),
                registry.version("go", value),
            )
            if old is not None and new is not None and new < old:
                raise ValueError("Go resolution downgraded a declared requirement")
    for package in sorted({item[0] for item in added}):
        items = [item for item in added if item[0] == package]
        releases = []
        for value in sorted({item[1] for item in items}):
            if query(root, spec, package, value).get("Retracted"):
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
                    "go", go_candidates(root, spec, package), policy, package, now
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
