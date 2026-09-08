"""Manifest, workspace and lock updates for native package managers."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from packaging.requirements import Requirement
from packaging.version import Version
import chainman
import lock_adapters
import manifests
import registry
import toolchain as tc
import updates


KINDS = {
    "rust": ("crates", "Cargo.toml"),
    "python": ("pypi", "pyproject.toml"),
    "flutter": ("pub", "pubspec.yaml"),
    "swift": ("swift", "Package.swift"),
    "gradle": ("maven", "build.gradle.kts"),
}


def members(root: Path, directory: Path, kind: str) -> list[str]:
    filename = KINDS[kind][1]
    path = tc.contained(root, str((directory / filename).relative_to(root)))
    result = {path}
    if kind not in {"rust", "python", "flutter"}:
        return [str(path.relative_to(root))]
    document = manifests.document(path)[0]
    table = (
        document.get("workspace", {})
        if kind == "rust"
        else document.get("tool", {}).get("uv", {}).get("workspace", {})
        if kind == "python"
        else {}
    )
    patterns = (
        document.get("workspace", []) if kind == "flutter" else table.get("members", [])
    )
    excluded = table.get("exclude", [])
    if not isinstance(patterns, list) or not isinstance(excluded, list):
        raise ValueError("Workspace members and exclusions must be declared path lists")
    removed = set()
    for collection, destination in ((patterns, result), (excluded, removed)):
        for pattern in collection:
            tc.contained(directory, pattern)
            for child in directory.glob(pattern):
                candidate = tc.contained(
                    root, str((child / filename).relative_to(root))
                )
                if candidate.is_file():
                    destination.add(candidate)
    return [str(path.relative_to(root)) for path in sorted(result - removed)]


def specifications(root: Path, spec: dict) -> dict:
    kind = spec["adapter"]
    ecosystem, _ = KINDS[kind]
    directories = spec.get("directories", [spec.get("directory", ".")])
    if (
        not isinstance(directories, list)
        or not directories
        or len(set(directories)) != len(directories)
    ):
        raise ValueError(
            "Native dependency adapters require distinct workspace directories"
        )
    result = {}
    for index, relative in enumerate(directories):
        directory = tc.contained(root, relative)
        if not directory.is_dir():
            raise ValueError("Declared dependency workspace does not exist")
        inputs = spec.get("manifests") or members(root, directory, kind)
        if not isinstance(inputs, list):
            raise ValueError("Native manifest inputs must be a path list")
        for path in inputs:
            tc.contained(root, path)
        name = f"{kind}-{index}"
        result[name] = {
            **deepcopy(spec),
            "name": name,
            "ecosystem": ecosystem,
            "directory": relative,
            "inputs": inputs,
        }
    return result


def snapshot(root: Path, spec: dict) -> dict:
    specs = specifications(root, spec)
    bootstrap = spec.get("bootstrap_verification", False)
    if type(bootstrap) is not bool or (bootstrap and spec["adapter"] != "gradle"):
        raise ValueError("bootstrap_verification is a Gradle-only boolean")
    adopted = {}
    for name, member in specs.items():
        directory = tc.contained(root, member["directory"])
        if bootstrap:
            locks = lock_adapters.paths(root, directory, "*.lockfile")
            metadata = tc.contained(
                root,
                str((directory / "gradle/verification-metadata.xml").relative_to(root)),
            )
            if not locks and not metadata.exists():
                continue
            if not locks or not metadata.is_file():
                raise ValueError(
                    "Gradle adoption cannot ignore a partial lock/verification baseline"
                )
        adopted[name] = member
    identities = updates.lock_identities(root, list(adopted), specs=adopted)
    requirements = [
        {
            "provider": pin["provider"],
            "name": pin["name"],
            "range": pin.get("bound", old_requirement(root, pin)),
        }
        for pin in pins(root, spec, specs)
    ]
    return {
        "identities": [list(item) for item in sorted(identities)],
        "requirements": requirements,
    }


def swift_pins(root: Path, specs: dict) -> list[dict]:
    result = []
    for spec in specs.values():
        for name in spec["inputs"]:
            if not name.endswith("Package.swift"):
                continue
            body = tc.regular_input(root, name).decode()
            for match in re.finditer(r"\.package\s*\(([^)]*)\)", body, re.DOTALL):
                declaration = match.group(1)
                if re.search(r"\bpath\s*:", declaration):
                    local = re.search(r'\bpath\s*:\s*"([^"\\]+)"', declaration)
                    if not local:
                        raise ValueError(
                            "Computed Swift package paths need an explicit source declaration"
                        )
                    tc.local_source(root, (root / name).parent, local.group(1))
                    continue
                url = re.search(r'\burl\s*:\s*"([^"\\]+)"', declaration)
                value = re.search(r'\b(from|exact)\s*:\s*"([^"\\]+)"', declaration)
                if not url or not value:
                    raise ValueError(
                        "Swift remote dependencies require literal from/exact releases or explicit pins"
                    )
                repository = lock_adapters.swift_repository(url.group(1))
                # Escape the declaration context, leaving exactly one named version group.
                prefix = match.group(0)[
                    : match.start(1) - match.start(0) + value.start(2)
                ]
                suffix = match.group(0)[
                    match.start(1) - match.start(0) + value.end(2) :
                ]
                result.append(
                    {
                        "provider": "swift",
                        "name": repository,
                        "file": name,
                        "format": "regex",
                        "pattern": re.escape(prefix)
                        + r'(?P<value>[^"\n]+)'
                        + re.escape(suffix),
                        "bound": value.group(2)
                        if value.group(1) == "exact"
                        else "^" + value.group(2),
                    }
                )
    return result


def gradle_pins(root: Path, spec: dict) -> list[dict]:
    result = []
    local = lock_adapters.local_gradle_projects(root, spec)
    for name in spec.get("catalogs", []):
        document = manifests.document(tc.contained(root, name))[0]
        grouped = {}
        for section in ("libraries", "plugins"):
            for alias, item in document.get(section, {}).items():
                if not isinstance(item, Mapping):
                    raise ValueError(
                        "Gradle catalogs require structured library/plugin entries"
                    )
                package = item.get("module") or (
                    f"{item['group']}:{item['name']}"
                    if "group" in item
                    else f"{item['id']}:{item['id']}.gradle.plugin"
                    if "id" in item
                    else None
                )
                if not package:
                    raise ValueError(
                        "Gradle catalog entry lacks a dependency coordinate"
                    )
                if package in local:
                    continue
                value = item.get("version")
                if isinstance(value, Mapping) and set(value) == {"ref"}:
                    pointer = ["versions", value["ref"]]
                elif isinstance(value, str):
                    pointer = [section, alias, "version"]
                elif value is None:
                    continue  # Platform/BOM-managed version, checked in the resolved lock.
                else:
                    raise ValueError(
                        "Rich Gradle version declarations need explicit dependency pins"
                    )
                key = tuple(pointer)
                grouped.setdefault(key, []).append(package)
        for pointer, packages in grouped.items():
            result.append(
                {
                    "provider": "maven",
                    "name": packages[0],
                    "coordinated": sorted(set(packages)),
                    "file": name,
                    "pointer": list(pointer),
                }
            )
    return result


def pins(root: Path, spec: dict, specs: dict) -> list[dict]:
    kind = spec["adapter"]
    result = manifests.discover(root, list(specs), specs=specs)
    if kind == "swift" and not spec.get("pins"):
        result.extend(swift_pins(root, specs))
    if kind == "gradle":
        result.extend(gradle_pins(root, spec))
    result.extend(deepcopy(spec.get("pins", [])))
    seen = set()
    for pin in result:
        key = (pin["file"], json.dumps(pin.get("pointer", pin.get("pattern"))))
        if key in seen:
            raise ValueError(
                "Dependency target has multiple authoritative declarations"
            )
        seen.add(key)
    return result


def old_requirement(root: Path, pin: dict) -> str:
    if pin.get("format") == "regex":
        matches = list(
            re.finditer(
                pin["pattern"],
                tc.regular_input(root, pin["file"]).decode(),
                re.MULTILINE,
            )
        )
        if len(matches) != 1:
            raise ValueError("Explicit dependency pin must match exactly once")
        return matches[0].group("value")
    value = manifests.lookup(
        manifests.document(tc.contained(root, pin["file"]))[0], pin["pointer"]
    )
    if pin.get("representation") == "requirement":
        return str(Requirement(value).specifier)
    return value


def accepts(provider: str, version: str, requirement: str) -> bool:
    if provider in {"pypi", "maven"}:
        if provider == "maven" and re.fullmatch(r"\d+(?:\.\d+)*", requirement):
            requirement = "==" + requirement
        return registry.compatible(provider, version, requirement)
    if provider == "crates" and re.fullmatch(r"\d+(?:\.\d+){0,2}", requirement):
        requirement = "^" + requirement
    return registry.compatible(provider, version, requirement)


def choose(root: Path, pin: dict, spec: dict, policy: dict, now: datetime):
    provider, package = pin["provider"], pin["name"]
    requirement = old_requirement(root, pin)
    coordinates = pin.get("coordinated", [package])
    inventories = []
    for coordinate in coordinates:
        candidates = (
            registry.maven_releases(
                coordinate, lock_adapters.maven_repository(spec, coordinate)
            )
            if provider == "maven"
            else registry.releases(provider, coordinate)
        )
        if spec.get("mode", "aggressive") == "compatible":
            candidates = [
                item
                for item in candidates
                if accepts(provider, item.version, pin.get("bound", requirement))
            ]
        candidates = [
            item
            for item in candidates
            if registry.compatible(
                provider,
                item.version,
                registry.constraint(provider, policy, coordinate),
            )
        ]
        inventories.append(candidates)
    common = set.intersection(
        *({item.version for item in values} for values in inventories)
    )
    dates = {
        value: max(
            item.published
            for inventory in inventories
            for item in inventory
            if item.version == value
        )
        for value in common
    }
    eligible = [
        registry.eligible(
            provider,
            [
                registry.Release(
                    item.version,
                    dates[item.version],
                    item.identity,
                    item.python,
                    item.artifacts,
                )
                for item in inventory
                if item.version in common
            ],
            policy,
            coordinate,
            now,
        )
        for coordinate, inventory in zip(coordinates, inventories, strict=True)
    ]
    common = set.intersection(
        *({item.version for item in values} for values in eligible)
    )
    candidates = [item for item in eligible[0] if item.version in common]
    if not candidates:
        raise ValueError(
            f"No eligible version satisfies the declared policy for {provider}:{package}"
        )
    chosen = max(candidates, key=lambda item: registry.version(provider, item.version))
    lower = re.search(
        r"(?<![\w.])v?(\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]+)?)", requirement
    )
    if lower:
        try:
            if Version(chosen.version.removeprefix("v")) < Version(lower.group(1)):
                return None
        except ValueError:
            raise ValueError(
                "Cannot establish the current dependency version order"
            ) from None
    return chosen


def gradle_graph(root: Path, spec: dict, directory: Path, *, write: bool) -> dict:
    """Ask Gradle for actual local identities; final inspection never rewrites locks."""
    executable = "./gradlew" if (directory / "gradlew").is_file() else "gradle"
    command = [
        executable,
        "--no-daemon",
        "--init-script",
        str(tc.RUNTIME / "scripts/gradle-resolve.init.gradle"),
    ]
    command += (
        [
            "chainmanResolveAll",
            "--write-locks",
            "--write-verification-metadata",
            "sha256",
        ]
        if write
        else ["--offline", "--dependency-verification", "strict", "chainmanInspect"]
    )
    with tempfile.TemporaryDirectory(prefix="chainman-gradle-") as reports:
        env = {
            **tc.environment(root),
            "TOOLCHAIN_FRESH": "1",
            "CHAINMAN_GRADLE_REPORT_DIR": reports,
        }
        chainman.execute(
            root, spec.get("profile", "gradle"), command, cwd=directory, env=env
        )
        values = [
            json.loads(path.read_text())
            for path in sorted(Path(reports).glob("*.json"))
        ]
        return lock_adapters.validate_gradle_projects(root, spec, values)


@contextmanager
def pub_resolution_pins(root: Path, planned: list, extra: dict | None = None):
    """Keep public ranges while binding native resolution to the chosen releases."""
    documents = {}

    def document(name):
        if name not in documents:
            before = tc.regular_input(root, name)
            path = tc.contained(root, name)
            value, render = manifests.document(path, body=before.decode())
            documents[name] = (before, stat.S_IMODE(path.stat().st_mode), value, render)
        return documents[name][2]

    for pin, chosen in planned:
        if pin["provider"] != "pub" or chosen is None:
            continue
        if "pointer" not in pin:
            raise ValueError("Pub resolution requires structured dependency pointers")
        name = pin["file"]
        manifests.assign(document(name), pin["pointer"], chosen.version)
    for (name, package), version in (extra or {}).items():
        value = document(name)
        if any(
            package in value.get(section, {})
            for section in ("dependencies", "dev_dependencies")
        ):
            raise ValueError(
                "Pub transitive constraints cannot replace a declared direct dependency"
            )
        # Ordinary constraints participate in Pub's complete native solve. An
        # override here would bypass a parent's range and is deliberately avoided.
        value.setdefault("dev_dependencies", {})[package] = version
    written = {}
    try:
        for name, (before, mode, _, render) in documents.items():
            path = tc.contained(root, name)
            if (
                tc.regular_input(root, name) != before
                or stat.S_IMODE(path.stat().st_mode) != mode
            ):
                raise ValueError(
                    "Pub manifest changed before temporary resolution pins"
                )
            expected = render().encode()
            path.write_bytes(expected)
            written[name] = (before, mode, expected)
        yield
    finally:
        drift = []
        for name, (before, mode, expected) in written.items():
            try:
                path = tc.contained(root, name)
                if (
                    tc.regular_input(root, name) != expected
                    or stat.S_IMODE(path.stat().st_mode) != mode
                ):
                    drift.append(name)
                    continue
                path.write_bytes(before)
            except (OSError, ValueError):
                drift.append(name)
        if drift:
            raise ValueError(
                "Pub resolver changed a temporarily pinned manifest; preserve and inspect its changes"
            )


PUB_SOLVER_STATES = 64


def pub_resolve(
    root: Path,
    spec: dict,
    specs: dict,
    planned: list,
    before: dict,
    policy: dict,
    now: datetime,
) -> None:
    """Repair newly ineligible transitives with bounded, ordinary Pub constraints."""
    commands = deepcopy(spec.get("resolve", [["flutter", "pub", "get"]]))
    if (
        not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(command, list)
            or not command
            or any(not isinstance(argument, str) for argument in command)
            for command in commands
        )
    ):
        raise ValueError("Native resolution requires explicit argument-array commands")
    standard = all(
        isinstance(command, list)
        and len(command) >= 3
        and Path(command[0]).name in {"dart", "flutter"}
        and command[1:3] == ["pub", "get"]
        for command in commands
    )
    watched = set()
    for member in specs.values():
        watched.update(
            str(Path(member["directory"]) / name)
            for name in ("pubspec.yaml", "pubspec_overrides.yaml")
        )
        for name in member["inputs"]:
            if name.endswith("pubspec.yaml"):
                watched.update(
                    (name, str(Path(name).with_name("pubspec_overrides.yaml")))
                )

    def manifest_state():
        result = {}
        for name in watched:
            path = tc.contained(root, name)
            result[name] = (
                (tc.regular_input(root, name), stat.S_IMODE(path.stat().st_mode))
                if path.exists()
                else None
            )
        return result

    expected = manifest_state()

    def unchanged():
        try:
            equal = manifest_state() == expected
        except (OSError, ValueError):
            equal = False
        if not equal:
            raise ValueError(
                "Pub manifest or override changed during resolution; preserve and inspect its changes"
            )

    def identities():
        return {
            name: updates.lock_identities(root, [name], specs=specs) for name in specs
        }

    def run(*, retry=False, offline=False):
        for member in specs.values():
            directory = tc.contained(root, member["directory"])
            for command in commands:
                argv = (
                    [*command, "--offline"]
                    if offline and "--offline" not in command
                    else command
                )
                try:
                    result = chainman.execute(
                        root,
                        spec.get("profile", "flutter"),
                        argv,
                        cwd=directory,
                        env={**tc.environment(root), "TOOLCHAIN_FRESH": "1"},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except subprocess.CalledProcessError as error:
                    output = error.stdout or ""
                    print(output, end="")
                    if (
                        retry
                        and standard
                        and error.returncode in {1, 65}
                        and "version solving failed" in output.lower()
                    ):
                        return False
                    raise
                if result is not None:
                    print(result.stdout or "", end="")
        return True

    baseline = {tuple(item) for item in before["identities"]}
    inventories = {}

    def inventory(package):
        if package not in inventories:
            inventories[package] = registry.releases("pub", package)
        return inventories[package]

    def issue(graph):
        for name, current in graph.items():
            for identity in sorted(current):
                provider, package, version, url, digest = identity
                if provider != "pub":
                    raise ValueError(
                        "Pub resolution produced another registry identity"
                    )
                releases = inventory(package)
                artifacts = [
                    a
                    for r in releases
                    if r.version == version
                    for a in r.artifacts
                    if a.digest == digest and (not url or a.url == url)
                ]
                if not artifacts:
                    raise ValueError(
                        "Pub artifact identity is absent from registry evidence"
                    )
                if max(a.published for a in artifacts) > now:
                    raise ValueError("Future Pub artifact publication age")
                allowed = registry.maturity(
                    "pub", releases, policy, package, now
                ) + registry.active_exceptions("pub", releases, policy, package, now)
                bound = registry.constraint("pub", policy, package)
                safe = registry.minimum_safe("pub", policy, package)

                def retainable(value):
                    return registry.compatible("pub", value, bound) and (
                        safe is None or registry.version("pub", value) >= safe
                    )

                if (identity in baseline and retainable(version)) or version in {
                    r.version for r in allowed
                }:
                    continue
                # Only exact observed artifacts receive baseline retention. The
                # final generic audit still checks their floors and constraints.
                retained = [
                    r
                    for r in releases
                    if any(
                        old[:3] == ("pub", package, r.version)
                        and any(
                            a.digest == old[4] and (not old[3] or a.url == old[3])
                            for a in r.artifacts
                        )
                        for old in baseline
                    )
                    and retainable(r.version)
                ]
                values = sorted(
                    {r.version for r in [*allowed, *retained]},
                    key=lambda value: registry.version("pub", value),
                    reverse=True,
                )
                manifest = str(Path(specs[name]["directory"]) / "pubspec.yaml")
                return (manifest, package), values
        return None

    visited = set()
    frontier = [iter([{}])]
    last = ""

    def branches(state, target, values):
        for value in values:
            yield {**state, target: value}

    while frontier:
        try:
            state = next(frontier[-1])
        except StopIteration:
            frontier.pop()
            continue
        key = tuple(sorted(state.items()))
        if key in visited:
            continue
        if len(visited) >= PUB_SOLVER_STATES:
            raise ValueError(
                f"Pub eligibility solver exhausted its {PUB_SOLVER_STATES}-state bound: {last}"
            )
        visited.add(key)
        unchanged()
        try:
            with pub_resolution_pins(root, planned, state):
                if not run(retry=bool(state)):
                    continue
                graph = identities()
                conflict = issue(graph)
        finally:
            unchanged()
        if conflict:
            target, values = conflict
            last = f"no eligible native graph for pub:{target[1]} in {target[0]}"
            if not standard:
                raise ValueError(
                    "Pub eligibility repair requires native pub get commands"
                )
            if target in state:
                raise ValueError(
                    "Pub did not honor an ordinary eligibility constraint; inspect declared overrides"
                )
            frontier.append(branches(state, target, values))
            continue
        if state:
            # Removing synthetic direct constraints changes lock dependency roles.
            # Normalize only during resolution, from the already fetched cache,
            # and never accept any artifact substitution during that operation.
            try:
                run(offline=True)
            finally:
                unchanged()
            if identities() != graph:
                raise ValueError(
                    "Pub normalization changed the selected immutable artifact graph"
                )
        return
    raise ValueError(
        f"No eligible Pub transitive graph satisfies native constraints: {last}"
    )


def resolve(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    if spec.get("mode", "aggressive") not in {"aggressive", "compatible"}:
        raise ValueError("Native update policy must be aggressive or compatible")
    specs = specifications(root, spec)
    selected = list(specs)
    before = snapshot(root, spec)
    manifests.configure_build_dependencies(
        root, selected, specs=specs, validate_only=True
    )
    planned = [
        (pin, choose(root, pin, spec, policy, now)) for pin in pins(root, spec, specs)
    ]
    changed = []
    project_graphs = []
    for pin, chosen in planned:
        if chosen and manifests.replace(pin, chosen, root):
            changed.append(pin["file"])
    manifests.configure_build_dependencies(root, selected, specs=specs)
    if spec["adapter"] == "flutter":
        pub_resolve(root, spec, specs, planned, before, policy, now)
    if spec["adapter"] != "flutter":
        for member in specs.values():
            directory = tc.contained(root, member["directory"])
            kind = spec["adapter"]
            default = {
                "rust": [["cargo", "update"]],
                "python": [["uv", "lock", "--upgrade"]],
                "swift": [["swift", "package", "update"]],
                "gradle": [
                    [
                        "gradle",
                        "--no-daemon",
                        "dependencies",
                        "--write-locks",
                        "--write-verification-metadata",
                        "sha256",
                    ]
                ],
            }[kind]
            commands = deepcopy(spec.get("resolve", default))
            if not isinstance(commands, list) or not commands:
                raise ValueError(
                    "Native resolution requires explicit argument-array commands"
                )
            if kind == "python":
                options = updates.uv_resolution_options(policy, now)
                if any(argv[:2] != ["uv", "lock"] for argv in commands):
                    raise ValueError(
                        "Python resolution must use uv lock for artifact-age policy"
                    )
                manifest = directory / "pyproject.toml"
                lock = directory / "uv.lock"
                old_manifest, old_lock = (
                    manifest.read_text(),
                    lock.read_text() if lock.exists() else None,
                )
                configured = updates.configure_uv(root, member, options)
                commands = [argv + options for argv in commands]
            env = tc.environment(root)
            env["TOOLCHAIN_FRESH"] = "1"
            for command in commands:
                chainman.execute(
                    root, spec.get("profile", kind), command, cwd=directory, env=env
                )
            if kind == "gradle":
                # The ordinary dependencies task visits only one project. Traverse all
                # resolvable project and buildscript configurations before final audit.
                project_graphs.append(gradle_graph(root, spec, directory, write=True))
            if kind == "python":
                updates.retain_uv_noop(root, member, old_manifest, old_lock, configured)
    audit(root, spec, before, policy, now)
    return {
        "changed_manifests": sorted(set(changed)),
        "project_graphs": project_graphs,
        "pins": [
            {"pin": pin, "value": old_requirement(root, pin)} for pin, _ in planned
        ],
    }


def audit(root: Path, spec: dict, before: dict, policy: dict, now: datetime):
    specs = specifications(root, spec)
    if spec.get("bootstrap_verification"):
        for member in specs.values():
            if not lock_adapters.paths(
                root, tc.contained(root, member["directory"]), "*.lockfile"
            ):
                raise ValueError(
                    "Gradle resolution did not produce its required component locks"
                )
    for item in before.get("resolution", {}).get("pins", []):
        if old_requirement(root, item["pin"]) != item["value"]:
            raise ValueError("A project hook changed a selected dependency pin")
    if spec.get("mode", "aggressive") == "compatible":
        for provider, package, value, _, _ in updates.lock_identities(
            root, list(specs), specs=specs
        ):
            for bound in before.get("requirements", []):
                if (
                    bound["provider"] == provider
                    and registry.package_name(provider, bound["name"]) == package
                    and not accepts(provider, value, bound["range"])
                ):
                    raise ValueError(
                        "Resolved dependency escaped its original compatible range"
                    )
    updates.audit_locks(
        root,
        list(specs),
        {tuple(item) for item in before["identities"]},
        policy,
        now,
        specs=specs,
    )
    if spec["adapter"] == "gradle":
        for member in specs.values():
            gradle_graph(
                root, spec, tc.contained(root, member["directory"]), write=False
            )
