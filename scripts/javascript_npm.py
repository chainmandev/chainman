"""npm package-lock v2/v3 execution and immutable graph auditing."""

import json
import re
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import javascript_updates as js
import registry
import adapter_data as inputs
from dependency_identity import Identity
import toolchain as tc
from semantic_version import NpmSpec, Version


def read(workspace: js.Workspace) -> inputs.NpmLock:
    path = tc.contained(workspace.directory, workspace.lock)
    if not path.exists():
        return {"lockfileVersion": 3, "packages": {}}
    value = inputs.npm_lock(
        json.loads(tc.regular_input(workspace.directory, workspace.lock))
    )
    for location in value["packages"]:
        tc.contained(workspace.directory, location or ".")
    return value


def local_locations(workspace: js.Workspace) -> set[str]:
    return {
        "" if Path(name).parent == Path(".") else Path(name).parent.as_posix()
        for name in workspace.manifests
    }


def name_at(location: str, item: inputs.NpmPackage) -> str:
    suffix = location.rsplit("node_modules/", 1)[-1]
    return js.package_name(item.get("name", suffix))


def identities(workspace: js.Workspace) -> set[Identity]:
    result: set[Identity] = set()
    lock = read(workspace)
    locals = local_locations(workspace)
    for location, item in lock["packages"].items():
        if location in locals:
            if item.get("resolved") or item.get("integrity"):
                raise ValueError(
                    "A local npm workspace cannot carry a registry resolution"
                )
            manifest = workspace.documents[
                (location + "/" if location else "") + "package.json"
            ][0]
            if ("name" in item and item["name"] != manifest.get("name")) or (
                "version" in item and item["version"] != manifest.get("version")
            ):
                raise ValueError(
                    "npm local package identity disagrees with its declared manifest"
                )
            continue
        if item.get("link") is True:
            if item.get("resolved") not in locals or set(item) - {"resolved", "link"}:
                raise ValueError("npm link must name one declared local workspace")
            target = inputs.text(item.get("resolved"), "npm workspace link target")
            manifest = workspace.documents[
                (target + "/" if target else "") + "package.json"
            ][0]
            if not re.search(r"(?:^|/)node_modules/", location) or name_at(
                location, {}
            ) != manifest.get("name"):
                raise ValueError(
                    "npm local link alias disagrees with its declared workspace name"
                )
            continue
        if not re.search(r"(?:^|/)node_modules/", location):
            raise ValueError("npm lock contains an undeclared local package")
        name, version = (
            name_at(location, item),
            inputs.text(item.get("version"), "npm version"),
        )
        if registry.lock_version("npm", version) is None:
            raise ValueError("npm lock lacks a stable registry version")
        result.add(
            Identity(
                provider="npm",
                package=name,
                version=version,
                url=registry.artifact_url(
                    inputs.text(item.get("resolved"), "npm resolved")
                ),
                digest=registry.digest(
                    inputs.text(item.get("integrity"), "npm integrity"), npm=True
                ),
            )
        )
    return result


def locate(
    packages: dict[str, inputs.NpmPackage], source: str, name: str
) -> str | None:
    js.package_name(name)
    path = Path(source or ".")
    for parent in [path, *path.parents]:
        if parent.name == "node_modules":
            continue
        key = (parent / "node_modules" / name).as_posix().removeprefix("./")
        if key in packages:
            if packages[key].get("link") is True:
                key = inputs.text(
                    packages[key].get("resolved"), "npm workspace link target"
                )
                if key not in packages or packages[key].get("link"):
                    raise ValueError("Invalid or recursive npm workspace link")
            return key
    return None


def allowed(workspace: js.Workspace, pin: js.Pin) -> list[tuple[str, str]]:
    result = js.effective_requirements(workspace, pin)
    replacement = (
        workspace.documents["package.json"][0].get("overrides", {}).get(pin.alias)
    )
    if isinstance(replacement, dict):
        replacement = replacement.get(".")
    if isinstance(replacement, str) and not replacement.startswith("$"):
        parsed = js.parse_requirement(pin.alias, replacement)
        if parsed:
            result.append((parsed[0], parsed[2]))
    return result


def audit(
    workspace: js.Workspace, before: dict, policy: dict, now: datetime
) -> list[str]:
    lock, evidence = read(workspace), js.Evidence(policy, now)
    packages, options = lock["packages"], policy.get("javascript", {})
    scopes: dict[tuple[str, str], list[str]] = {}
    locals = local_locations(workspace)
    for manifest, refs in workspace.refs.items():
        importer = (
            ""
            if Path(manifest).parent == Path(".")
            else Path(manifest).parent.as_posix()
        )
        if importer not in packages:
            raise ValueError("npm lock is missing a declared workspace")
        queue = [importer]
        for alias, index in refs.items():
            pin = workspace.pins[index]
            location = locate(packages, importer, alias)
            if location is None:
                raise ValueError("npm lock is missing a declared dependency")
            item = packages[location]
            actual, version = (
                name_at(location, item),
                inputs.text(item.get("version"), "npm version"),
            )
            if not any(
                actual == name and Version(version) in NpmSpec(bound)
                for name, bound in allowed(workspace, pin)
            ):
                raise ValueError(
                    "npm locked dependency disagrees with its manifest or override"
                )
            if any(
                Version(version) not in NpmSpec(bound)
                for bound in js.compatibility(pin, options)
            ):
                raise ValueError(
                    "npm dependency violates scoped JavaScript compatibility"
                )
            scopes.setdefault((actual, version), []).extend(
                js.direct_scope(pin, workspace.spec, options, version, before)
            )
            queue.append(location)
        visited = set()
        while queue:
            location = queue.pop()
            if location in visited:
                continue
            visited.add(location)
            if len(visited) > 100000:
                raise ValueError("npm graph exceeds its audit bound")
            item = packages[location]
            if location in locals:
                info = workspace.documents[
                    (location + "/" if location else "") + "package.json"
                ][0]
                actual = info["name"]
                peers, metadata = (
                    info.get("peerDependencies", {}),
                    info.get("peerDependenciesMeta", {}),
                )
                for section in js.SECTIONS:
                    if item.get(section, {}) != info.get(section, {}):
                        raise ValueError(
                            "npm local dependency declarations disagree with their manifest"
                        )
                dependencies = info
            else:
                dependencies = item
                actual = name_at(location, item)
                peers, metadata = evidence.peers(
                    actual, item["version"], manifest=manifest
                )
                for rule in options.get("prefix_constraints", []):
                    if (
                        actual.startswith(rule["prefix"])
                        and actual not in rule.get("exclude", [])
                        and Version(item["version"]) not in NpmSpec(js.rule_range(rule))
                    ):
                        raise ValueError(
                            "npm transitive dependency violates prefix compatibility"
                        )
            for peer, bound in peers.items():
                if js.peer_ignored(options, manifest, actual, peer):
                    continue
                target = locate(packages, location, peer)
                if target is None and metadata.get(peer, {}).get("optional") is True:
                    continue
                if target is None or Version(
                    packages[target]["version"]
                ) not in NpmSpec(bound):
                    raise ValueError(
                        f"Missing or incompatible npm peer {actual}>{peer} in {manifest}"
                    )
                scopes.setdefault(
                    (name_at(target, packages[target]), packages[target]["version"]), []
                ).append(bound)
            for name, requirement in {
                **dependencies.get("dependencies", {}),
                **dependencies.get("optionalDependencies", {}),
                **(
                    dependencies.get("devDependencies", {})
                    if location in locals
                    else {}
                ),
            }.items():
                child = locate(packages, location, name)
                if child is None:
                    if name not in dependencies.get("optionalDependencies", {}):
                        raise ValueError(
                            "npm lock is missing a required transitive dependency"
                        )
                else:
                    if location in locals and requirement.startswith(
                        ("workspace:", "file:", "link:")
                    ):
                        expected_manifest = workspace.local_manifest(
                            name, requirement, location or "."
                        )
                        expected = str(Path(expected_manifest).parent)
                        expected = "" if expected == "." else expected
                        if child != expected:
                            raise ValueError(
                                "npm local dependency resolved to a different declared workspace"
                            )
                    queue.append(child)
    return js.audit_artifacts(workspace, before, policy, now, scopes)


def resolve(
    workspace: js.Workspace,
    before: dict,
    evidence: js.Evidence,
    selected: Sequence[str],
    policy: dict,
    now: datetime,
) -> dict:
    root, spec = workspace.root, workspace.spec
    options = policy.get("javascript", {})
    active = [
        r
        for e in policy.get("exceptions", [])
        if e.get("package", "").startswith("npm:")
        for r in registry.active_exceptions(
            "npm", evidence.get(e["package"][4:])[0], policy, e["package"][4:], now
        )
    ]
    # npm has one cutoff. With exact security exceptions, admit candidates at
    # the present cutoff and independently reject every other young artifact.
    # A failure stays visible; it never broadens the audited exception set.
    cutoff = (
        now
        if active
        or any(
            e.get("package", "").startswith("npm:")
            for e in policy.get("exceptions", [])
        )
        or js.baseline_maturity_exclusions(before, evidence)
        else now - timedelta(days=registry.minimum_age(policy))
    )
    peer_option = (
        "--legacy-peer-deps" if options.get("peer_exceptions") else "--strict-peer-deps"
    )
    parent = tc.contained(root, ".cache/toolchain/work")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="npm-update-", dir=parent) as name:
        temporary = Path(name)
        content = workspace.render(selected, resolver_pins=True)
        for relative, data in content.items():
            if relative != workspace.lock:
                tc.atomic_bytes(
                    tc.contained(temporary, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
        command = [
            "npm",
            "install",
            "--package-lock-only",
            "--ignore-scripts",
            "--no-audit",
            "--before=" + cutoff.isoformat(),
            peer_option,
        ]
        result = js.chainman.execute(
            root,
            spec.get("profile", "javascript"),
            command,
            cwd=temporary,
            env=tc.environment(root),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode:
            raise ValueError(
                f"npm resolution failed with exit {result.returncode}; no files were installed"
            )
        for relative, data in content.items():
            if (
                relative != workspace.lock
                and tc.regular_input(temporary, relative) != data
            ):
                raise ValueError(
                    "npm changed an input outside the planned dependency edits"
                )
        content = workspace.render(selected)
        for relative, data in content.items():
            if relative != workspace.lock:
                tc.atomic_bytes(
                    tc.contained(temporary, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
        path = tc.contained(temporary, workspace.lock)
        lock = inputs.table(
            json.loads(tc.regular_input(temporary, workspace.lock)), "npm lock"
        )
        inputs.npm_lock(lock)
        packages = inputs.table(lock["packages"], "npm packages")
        for manifest in workspace.manifests:
            key = (
                ""
                if Path(manifest).parent == Path(".")
                else Path(manifest).parent.as_posix()
            )
            if key not in packages:
                raise ValueError("npm resolver omitted a declared workspace")
            entry = inputs.table(packages[key], "npm workspace entry")
            for section in js.SECTIONS:
                if section in workspace.documents[manifest][0]:
                    entry[section] = workspace.documents[manifest][0][section]
            packages[key] = entry
        lock["packages"] = packages
        tc.atomic_bytes(path, (json.dumps(lock, indent=2) + "\n").encode(), 0o644)
        checked = js.chainman.execute(
            root,
            spec.get("profile", "javascript"),
            [
                "npm",
                "ci",
                "--dry-run",
                "--ignore-scripts",
                "--offline",
                "--no-audit",
                peer_option,
            ],
            cwd=temporary,
            env=tc.environment(root),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if checked.returncode:
            raise ValueError("npm ci rejected the restored manifest ranges")
        copied = js.Workspace(temporary, {**spec, "directory": "."})
        audit(copied, before, policy, now)
        for relative, data in content.items():
            if (
                relative != workspace.lock
                and tc.regular_input(temporary, relative) != data
            ):
                raise ValueError("npm verification changed a declared resolver input")
        content[workspace.lock] = tc.regular_input(temporary, workspace.lock)
        for relative, data in workspace.original.items():
            if tc.regular_input(workspace.directory, relative) != data:
                raise ValueError("JavaScript inputs changed concurrently")
        for relative in content.keys() - workspace.original.keys():
            if tc.contained(workspace.directory, relative).exists():
                raise ValueError("JavaScript output appeared concurrently")
        changed = []
        for relative, data in content.items():
            if workspace.original.get(relative) != data:
                tc.atomic_bytes(
                    tc.contained(workspace.directory, relative),
                    data,
                    workspace.modes.get(relative, 0o644),
                )
                changed.append(
                    (workspace.directory / relative).relative_to(root).as_posix()
                )
    return {"changed_files": sorted(changed), "resolution_attempts": 1}
