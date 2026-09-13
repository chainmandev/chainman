"""Manifest, workspace and lock updates for native package managers."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version
import chainman
import lock_adapters
import manifests
import registry
from dependency_identity import Identity, inventory as identity_inventory
import adapter_data as ad
import toolchain as tc
import updates


type Config = Mapping[str, object]
type Specs = Mapping[str, ad.Table]
type Plan = Sequence[tuple[Config, registry.Release | None]]
type FileImage = tuple[bytes, int]
type FileStates = dict[str, FileImage | None]
type ResolutionGraph = dict[str, set[Identity]]
type CargoNode = tuple[str, str, str]
type CargoIssue = tuple[str, Identity, list[str]]
type CargoChoices = list[tuple[str, Identity]]


KINDS = {
    "rust": ("crates", "Cargo.toml"),
    "python": ("pypi", "pyproject.toml"),
    "flutter": ("pub", "pubspec.yaml"),
    "swift": ("swift", "Package.swift"),
    "gradle": ("maven", "build.gradle.kts"),
}


def members(root: Path, directory: Path, kind: str) -> list[str]:
    if kind == "flutter":
        return manifests.pub_workspace(root, directory)["inputs"]
    filename = KINDS[kind][1]
    path = tc.contained(root, str((directory / filename).relative_to(root)))
    result = {path}
    if kind not in {"rust", "python", "flutter"}:
        return [str(path.relative_to(root))]
    document = ad.table(manifests.document(path)[0], "Workspace manifest")
    table = (
        ad.table(document.get("workspace", {}), "Cargo workspace")
        if kind == "rust"
        else ad.table(
            ad.table(
                ad.table(document.get("tool", {}), "Python tools").get("uv", {}),
                "uv settings",
            ).get("workspace", {}),
            "uv workspace",
        )
        if kind == "python"
        else {}
    )
    patterns = (
        document.get("workspace", []) if kind == "flutter" else table.get("members", [])
    )
    excluded = table.get("exclude", [])
    if not isinstance(patterns, list) or not isinstance(excluded, list):
        raise ValueError("Workspace members and exclusions must be declared path lists")
    removed: set[Path] = set()
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


def specifications(root: Path, spec: Config) -> dict[str, ad.Table]:
    kind = ad.text(spec["adapter"], "Adapter kind")
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
    directories = ad.strings(directories, "Native workspace directories")
    result = {}
    pub_directories = set()
    for index, relative in enumerate(directories):
        directory = tc.contained(root, relative)
        if not directory.is_dir():
            raise ValueError("Declared dependency workspace does not exist")
        pub = manifests.pub_workspace(root, directory) if kind == "flutter" else None
        if pub is not None:
            relative = pub["directory"]
            if relative in pub_directories:
                raise ValueError(
                    "Pub adapter directories must name distinct resolution groups"
                )
            pub_directories.add(relative)
        inputs = spec.get("manifests") or (
            pub["inputs"] if pub is not None else members(root, directory, kind)
        )
        if not isinstance(inputs, list):
            raise ValueError("Native manifest inputs must be a path list")
        inputs = ad.strings(inputs, "Native manifest inputs")
        for path in inputs:
            tc.contained(root, path)
        name = f"{kind}-{index}"
        result[name] = {
            **deepcopy(spec),
            "name": name,
            "ecosystem": ecosystem,
            "directory": relative,
            "inputs": inputs,
            **({"pub": pub} if pub is not None else {}),
        }
    return result


def snapshot(root: Path, spec: Config) -> ad.Table:
    specs = specifications(root, spec)
    bootstrap = spec.get("bootstrap_verification", False)
    if type(bootstrap) is not bool or (
        bootstrap and ad.text(spec["adapter"], "Adapter kind") != "gradle"
    ):
        raise ValueError("bootstrap_verification is a Gradle-only boolean")
    adopted = {}
    for name, member in specs.items():
        directory = tc.contained(
            root, ad.text(member["directory"], "Workspace directory")
        )
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
            "provider": ad.text(pin["provider"], "Pin provider"),
            "name": ad.text(pin["name"], "Pin name"),
            "range": pin.get("bound", old_requirement(root, pin)),
        }
        for pin in pins(root, spec, specs)
    ]
    return {
        "identities": [list(item) for item in sorted(identities)],
        "requirements": requirements,
    }


def swift_pins(root: Path, specs: Specs) -> list[ad.Table]:
    result: list[ad.Table] = []
    for spec in specs.values():
        for name in ad.strings(spec["inputs"], "Workspace inputs"):
            if not name.endswith("Package.swift"):
                continue
            body = tc.regular_input(root, name).decode()
            for item in lock_adapters.swift_declarations(root, name):
                if item["kind"] == "local":
                    continue
                assert item["kind"] == "remote"  # explicit mode was not requested.
                result.append(
                    {
                        "provider": "swift",
                        "name": item["package"],
                        "file": name,
                        "format": "regex",
                        "bound": item["bound"],
                        "pattern": re.escape(body[item["start"] : item["value_start"]])
                        + r'(?P<value>[^"\n]+)'
                        + re.escape(body[item["value_end"] : item["end"]]),
                    }
                )
    return result


def swift_input_state(root: Path, specs: Specs) -> ad.Table:
    result: ad.Table = {}
    for member in specs.values():
        result.update(lock_adapters.swift_manifest_state(root, member))
    return result


@contextmanager
def swift_resolution_pins(
    root: Path, planned: Plan, *, explicit: bool = False
) -> Iterator[None]:
    """Temporarily narrow owned requirements, restoring post-selection public bytes."""
    selected = {}
    for pin, chosen in planned:
        if ad.text(pin["provider"], "Pin provider") == "swift" and chosen is not None:
            key = (ad.text(pin["file"], "Pin file"), ad.text(pin["name"], "Pin name"))
            if key in selected:
                raise ValueError("Ambiguous selected Swift dependency")
            selected[key] = chosen.version
    before: dict[str, FileImage] = {}
    images: dict[str, FileImage] = {}
    for name, _ in selected:
        if name in before:
            continue
        state = cargo_file_state(root, name)
        if state is None:
            raise ValueError("Missing Swift input manifest")
        before[name] = state
        body = before[name][0].decode()
        edits, found = [], set()
        for item in lock_adapters.swift_declarations(root, name, explicit=explicit):
            if item["kind"] != "remote" or (name, item["package"]) not in selected:
                continue
            value = selected[name, item["package"]]
            if value != item["version"]:
                raise ValueError(
                    "Swift selected requirement changed before native resolution"
                )
            found.add(item["package"])
            edits.append((item["style_start"], item["style_end"], "exact"))
        if not explicit and found != {
            package for filename, package in selected if filename == name
        }:
            raise ValueError("Missing selected Swift declaration")
        for start, end, value in sorted(edits, reverse=True):
            body = body[:start] + value + body[end:]
        images[name] = (body.encode(), before[name][1])
    written: dict[str, FileImage] = {}
    original: BaseException | None = None
    try:
        for name, image in images.items():
            if cargo_file_state(root, name) != before[name]:
                raise ValueError("Swift manifest changed before temporary pinning")
            # Register the expected write first so a partial preparation is restored.
            written[name] = image
            tc.atomic_bytes(tc.contained(root, name), *image)
        yield
    except BaseException as error:
        original = error
        raise
    finally:
        errors = []
        for name, image in written.items():
            try:
                current = cargo_file_state(root, name)
                if current == before[name]:
                    continue
                if current != image:
                    raise ValueError("unexpected bytes, mode or source")
                tc.atomic_bytes(tc.contained(root, name), *before[name])
            except (OSError, ValueError) as error:
                errors.append(f"{name}: {error}")
        if errors:
            message = "Swift restoration failed; changes preserved: " + "; ".join(
                errors
            )
            if original is None:
                raise ValueError(message)
            print(message, file=sys.stderr)
            original.add_note(message)


def gradle_pins(root: Path, spec: Config) -> list[ad.Table]:
    result: list[ad.Table] = []
    local = lock_adapters.local_gradle_projects(root, spec)
    for name in ad.strings(spec.get("catalogs", []), "Gradle catalogs"):
        document = ad.table(
            manifests.document(tc.contained(root, name))[0], "Gradle catalog"
        )
        grouped: dict[tuple[str, ...], list[str]] = {}
        for section in ("libraries", "plugins"):
            for alias, item in ad.table(
                document.get(section, {}), "Gradle catalog entries"
            ).items():
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
                package = ad.text(package, "Gradle package coordinate")
                value = item.get("version")
                if isinstance(value, Mapping) and set(value) == {"ref"}:
                    pointer = [
                        "versions",
                        ad.text(value["ref"], "Gradle version reference"),
                    ]
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
        for grouped_pointer, packages in grouped.items():
            result.append(
                {
                    "provider": "maven",
                    "name": packages[0],
                    "coordinated": sorted(set(packages)),
                    "file": name,
                    "pointer": list(grouped_pointer),
                }
            )
    return result


def pins(root: Path, spec: Config, specs: Specs) -> list[ad.Table]:
    kind = ad.text(spec["adapter"], "Adapter kind")
    result = [dict(pin) for pin in manifests.discover(root, list(specs), specs=specs)]
    if kind == "swift" and not spec.get("pins"):
        result.extend(swift_pins(root, specs))
    if kind == "gradle":
        result.extend(gradle_pins(root, spec))
    result.extend(
        ad.table(pin, "Explicit dependency pin")
        for pin in deepcopy(ad.array(spec.get("pins", []), "Explicit dependency pins"))
    )
    seen = set()
    for pin in result:
        key = (
            ad.text(pin["file"], "Pin file"),
            json.dumps(pin.get("pointer", pin.get("pattern"))),
        )
        if key in seen:
            raise ValueError(
                "Dependency target has multiple authoritative declarations"
            )
        seen.add(key)
    return result


def old_requirement(root: Path, pin: Config) -> str:
    if pin.get("format") == "regex":
        matches = list(
            re.finditer(
                ad.text(pin["pattern"], "Pin pattern"),
                tc.regular_input(root, ad.text(pin["file"], "Pin file")).decode(),
                re.MULTILINE,
            )
        )
        if len(matches) != 1:
            raise ValueError("Explicit dependency pin must match exactly once")
        return matches[0].group("value")
    value = ad.text(
        manifests.lookup(
            manifests.document(tc.contained(root, ad.text(pin["file"], "Pin file")))[0],
            manifests.pin_pointer(pin["pointer"]),
        ),
        "Dependency requirement",
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


def choose(
    root: Path, pin: Config, spec: Config, policy: Config, now: datetime
) -> registry.Release | None:
    provider, package = (
        ad.text(pin["provider"], "Pin provider"),
        ad.text(pin["name"], "Pin name"),
    )
    requirement = old_requirement(root, pin)
    coordinates = ad.strings(pin.get("coordinated", [package]), "Coordinated packages")
    inventories = []
    for coordinate in coordinates:
        candidates = (
            registry.maven_releases(
                coordinate, lock_adapters.maven_repository(spec, coordinate)
            )
            if provider == "maven"
            else registry.releases(provider, coordinate)
        )
        if spec.get("mode", "aggressive") == "compatible" or pin.get("pub_override"):
            candidates = [
                item
                for item in candidates
                if accepts(
                    provider,
                    item.version,
                    ad.text(pin.get("bound", requirement), "Dependency bound"),
                )
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
    try:
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
    except ValueError as error:
        if pin.get("pub_override"):
            raise ValueError(
                f"Cannot satisfy declared overrides for Pub {package}: {error}"
            ) from error
        raise
    common = set.intersection(
        *({item.version for item in values} for values in eligible)
    )
    candidates = [item for item in eligible[0] if item.version in common]
    if not candidates:
        raise ValueError(
            f"No eligible version satisfies the declared policy for {provider}:{package}"
        )
    chosen = max(
        candidates, key=lambda item: registry.stable_version(provider, item.version)
    )
    if pin.get("pub_override"):
        return chosen  # An override is a constraint, not an installed-version template.
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


def gradle_graph(
    root: Path, spec: Config, directory: Path, *, write: bool
) -> lock_adapters.GradleProjects:
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
            root,
            ad.text(spec.get("profile", "gradle"), "Adapter profile"),
            command,
            cwd=directory,
            env=env,
        )
        values = [
            json.loads(path.read_text())
            for path in sorted(Path(reports).glob("*.json"))
        ]
        return lock_adapters.validate_gradle_projects(root, spec, values)


def pub_input_state(root: Path, names: Iterable[str]) -> ad.Table:
    result: ad.Table = {}
    for name in sorted(names):
        path = tc.contained(root, name)
        result[name] = (
            [
                hashlib.sha256(tc.regular_input(root, name)).hexdigest(),
                stat.S_IMODE(path.stat().st_mode),
            ]
            if path.exists()
            else None
        )
    return result


def pub_lock_packages(root: Path, directory: Path) -> ad.Table:
    path = directory / "pubspec.lock"
    if not path.exists():
        raise ValueError("Missing resolved dependency lock: pubspec.lock")
    value = manifests.document(
        path, body=tc.regular_input(root, str(path.relative_to(root))).decode()
    )[0]
    if not isinstance(value, Mapping) or not isinstance(value.get("packages"), Mapping):
        raise ValueError("Malformed Pub lock package mapping")
    return ad.table(value["packages"], "Pub lock packages")


def pub_validate_sources(root: Path, specs: Specs) -> None:
    for member in specs.values():
        directory = tc.contained(
            root, ad.text(member["directory"], "Workspace directory")
        )
        packages = pub_lock_packages(root, directory)
        for package, raw_source in ad.table(
            ad.table(member["pub"], "Pub workspace")["sources"], "Pub sources"
        ).items():
            source = ad.table(raw_source, "Pub source")
            item = packages.get(package)
            if item is None and source["kind"] == "workspace":
                continue  # Pub omits actual workspace members from the shared lock.
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"Pub resolved source is missing from its lock: {package}"
                )
            if source["kind"] == "sdk":
                valid = (
                    item.get("source") == "sdk"
                    and item.get("description") == source["sdk"]
                )
            else:
                description = item.get("description")
                valid = item.get("source") == "path" and isinstance(
                    description, Mapping
                )
                if valid and isinstance(description, Mapping):
                    actual = tc.local_source(
                        root,
                        directory,
                        ad.text(description.get("path"), "Pub path description"),
                    )
                    valid = actual == tc.contained(
                        root, ad.text(source["path"], "Pub source path")
                    )
            if not valid:
                raise ValueError(
                    f"Pub resolved source disagrees with its effective declaration: {package}"
                )


def pub_validate_overrides(
    root: Path, specs: Specs, selected: Mapping[str, Mapping[str, str]]
) -> None:
    for group, versions in selected.items():
        if group not in specs:
            raise ValueError("Selected Pub override resolution group is missing")
        directory = tc.contained(
            root, ad.text(specs[group]["directory"], "Pub directory")
        )
        packages = pub_lock_packages(root, directory)
        for package, version in versions.items():
            item = packages.get(package)
            if not isinstance(item, Mapping):
                raise ValueError(
                    f"Selected hosted Pub override is missing from its lock: {package}"
                )
            description = item.get("description")
            if (
                item.get("source") != "hosted"
                or not isinstance(description, Mapping)
                or description.get("name") != package
                or description.get("url") != "https://pub.dev"
            ):
                raise ValueError(
                    f"Selected hosted Pub override changed its resolved source: {package}"
                )
            if item.get("version") != version:
                raise ValueError(
                    "Pub did not honor the selected version within declared overrides"
                )


@contextmanager
def pub_resolution_pins(
    root: Path, planned: Plan, extra: Mapping[tuple[str, str], str] | None = None
) -> Iterator[None]:
    """Keep public ranges while binding native resolution to the chosen releases."""
    documents: dict[
        str, tuple[bytes, int, MutableMapping[str, object], Callable[[], str]]
    ] = {}

    def document(name: str) -> MutableMapping[str, object]:
        if name not in documents:
            before = tc.regular_input(root, name)
            path = tc.contained(root, name)
            value, render = manifests.document(path, body=before.decode())
            if not isinstance(value, MutableMapping):
                raise ValueError("Pub manifest must be a mutable mapping")
            documents[name] = (before, stat.S_IMODE(path.stat().st_mode), value, render)
        return documents[name][2]

    for pin, chosen in planned:
        if ad.text(pin["provider"], "Pin provider") != "pub" or chosen is None:
            continue
        if "pointer" not in pin:
            raise ValueError("Pub resolution requires structured dependency pointers")
        name = ad.text(pin["file"], "Pin file")
        if pin.get("pub_override"):
            original = manifests.lookup(
                document(name), manifests.pin_pointer(pin["pointer"])
            )
            if original != pin["bound"] or not accepts(
                "pub", chosen.version, ad.text(original, "Pub override bound")
            ):
                raise ValueError(
                    "Temporary Pub pin must stay within unchanged declared overrides"
                )
        manifests.assign(
            document(name), manifests.pin_pointer(pin["pointer"]), chosen.version
        )
    for (name, package), version in (extra or {}).items():
        value = document(name)
        if any(
            package in ad.table(value.get(section, {}), "Pub dependencies")
            for section in ("dependencies", "dev_dependencies")
        ):
            raise ValueError(
                "Pub transitive constraints cannot replace a declared direct dependency"
            )
        # Ordinary constraints participate in Pub's complete native solve. An
        # override here would bypass a parent's range and is deliberately avoided.
        dependencies = value.setdefault("dev_dependencies", {})
        if not isinstance(dependencies, MutableMapping):
            raise ValueError("Pub development dependencies must be a mapping")
        dependencies[package] = version
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
    spec: Config,
    specs: Specs,
    planned: Plan,
    before: Config,
    policy: Config,
    now: datetime,
) -> ad.Table:
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
    commands = [
        ad.strings(command, "Native resolution command") for command in commands
    ]
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
            ad.strings(
                ad.table(member["pub"], "Pub workspace")["guarded_inputs"],
                "Pub guarded inputs",
            )
        )
        watched.update(
            str(Path(ad.text(member["directory"], "Workspace directory")) / name)
            for name in ("pubspec.yaml", "pubspec_overrides.yaml")
        )
        for name in ad.strings(member["inputs"], "Workspace inputs"):
            if name.endswith("pubspec.yaml"):
                watched.update(
                    (name, str(Path(name).with_name("pubspec_overrides.yaml")))
                )

    def manifest_state() -> ad.Table:
        return pub_input_state(root, watched)

    expected = manifest_state()

    def unchanged() -> None:
        try:
            equal = manifest_state() == expected
        except (OSError, ValueError):
            equal = False
        if not equal:
            raise ValueError(
                "Pub manifest or override changed during resolution; preserve and inspect its changes"
            )

    def identities() -> ResolutionGraph:
        pub_validate_sources(root, specs)
        return {
            name: updates.lock_identities(root, [name], specs=specs) for name in specs
        }

    def run(*, retry: bool = False, offline: bool = False) -> bool:
        for member in specs.values():
            directory = tc.contained(
                root, ad.text(member["directory"], "Workspace directory")
            )
            for command in commands:
                argv = (
                    [*command, "--offline"]
                    if offline and "--offline" not in command
                    else command
                )
                try:
                    result = chainman.execute(
                        root,
                        ad.text(spec.get("profile", "flutter"), "Adapter profile"),
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

    baseline = identity_inventory(before["identities"])
    selected_overrides = {
        name: {
            ad.text(pin["name"], "Pin name"): chosen.version
            for pin, chosen in planned
            if pin.get("pub_override")
            and chosen is not None
            and pin["pub_directory"]
            == ad.text(member["directory"], "Workspace directory")
        }
        for name, member in specs.items()
    }
    inventories: dict[str, list[registry.Release]] = {}

    def inventory(package: str) -> list[registry.Release]:
        if package not in inventories:
            inventories[package] = registry.releases("pub", package)
        return inventories[package]

    def issue(graph: ResolutionGraph) -> tuple[tuple[str, str], list[str]] | None:
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

                def retainable(value: str) -> bool:
                    return registry.compatible("pub", value, bound) and (
                        safe is None or registry.stable_version("pub", value) >= safe
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
                    key=lambda value: registry.stable_version("pub", value),
                    reverse=True,
                )
                manifest = str(
                    Path(ad.text(specs[name]["directory"], "Workspace directory"))
                    / "pubspec.yaml"
                )
                return (manifest, package), values
        return None

    visited: set[tuple[tuple[tuple[str, str], str], ...]] = set()
    frontier: list[Iterator[dict[tuple[str, str], str]]] = [iter([{}])]
    last = ""

    def branches(
        state: dict[tuple[str, str], str],
        target: tuple[str, str],
        values: Sequence[str],
    ) -> Iterator[dict[tuple[str, str], str]]:
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
                pub_validate_overrides(root, specs, selected_overrides)
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
        return {"pub_inputs": expected, "pub_overrides": selected_overrides}
    raise ValueError(
        f"No eligible Pub transitive graph satisfies native constraints: {last}"
    )


CARGO_SOLVER_STATES = 64


def cargo_file_state(root: Path, name: str) -> FileImage | None:
    path = tc.contained(root, name)
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        return None
    return tc.regular_input(root, name), mode


def cargo_restore(
    root: Path,
    before: Mapping[str, FileImage | None],
    expected: FileStates,
    original: BaseException | None = None,
) -> None:
    """Restore only known owned postimages; try independent paths after errors."""
    failures = []
    for name, previous in before.items():
        try:
            current = cargo_file_state(root, name)
            if current == previous:
                expected[name] = previous
                continue
            if current != expected[name]:
                raise ValueError("unexpected postimage")
            path = tc.contained(root, name)
            if previous is None:
                path.unlink()
            else:
                tc.atomic_bytes(path, previous[0], previous[1])
            expected[name] = previous
        except (OSError, ValueError) as error:
            failures.append(f"{name}: {error}")
    if failures:
        message = "Cargo restoration failed; changes preserved: " + "; ".join(failures)
        if original is not None:
            print(message, file=sys.stderr)
            original.add_note(message)
        else:
            raise ValueError(message)


@contextmanager
def cargo_resolution_pins(root: Path, planned: Plan) -> Iterator[None]:
    """Narrow existing direct fields, letting Cargo enforce their exact identity."""
    documents: dict[str, tuple[FileImage, object, Callable[[], str]]] = {}
    for pin, chosen in planned:
        if ad.text(pin["provider"], "Pin provider") != "crates" or chosen is None:
            continue
        if "pointer" not in pin or pin.get("format") == "regex":
            raise ValueError(
                "Cargo resolution requires existing structured direct pins"
            )
        name = ad.text(pin["file"], "Pin file")
        if name not in documents:
            image = cargo_file_state(root, name)
            if image is None:
                raise ValueError("Missing Cargo direct manifest")
            value, render = manifests.document(
                tc.contained(root, name), body=image[0].decode()
            )
            documents[name] = (image, value, render)
        value = documents[name][1]
        if not isinstance(
            manifests.lookup(value, manifests.pin_pointer(pin["pointer"])), str
        ):
            raise ValueError("Cargo direct pin does not name an existing version field")
        manifests.assign(
            value, manifests.pin_pointer(pin["pointer"]), "=" + chosen.version
        )
    before = {name: entry[0] for name, entry in documents.items()}
    expected: FileStates = {}
    original: BaseException | None = None
    try:
        for name, (previous, _, render) in documents.items():
            if cargo_file_state(root, name) != previous:
                raise ValueError(
                    "Cargo manifest changed before temporary direct pinning"
                )
            image = (render().encode(), previous[1])
            expected[name] = image
            tc.atomic_bytes(tc.contained(root, name), *image)
        yield
    except BaseException as error:
        original = error
        raise
    finally:
        cargo_restore(root, {n: before[n] for n in expected}, expected, original)


def cargo_input_state(root: Path, specs: Specs, lock_names: set[str]) -> ad.Table:
    """Cargo input closure; Git projects additionally guard all visible sources."""
    names = set()
    pending = []
    for member in specs.values():
        pending.append(
            str(
                Path(ad.text(member["directory"], "Workspace directory")) / "Cargo.toml"
            )
        )
        for pattern in ad.strings(member["inputs"], "Workspace inputs"):
            tc.contained(root, pattern)
            pending.extend(str(p.relative_to(root)) for p in root.glob(pattern))
    seen = set()

    sources: dict[str, tuple[str, bytes, int] | tuple[str, int] | None] = {}

    def add_tree(path: Path) -> None:
        relative = str(path.relative_to(root))
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            sources[relative] = None
            return
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            sources[relative] = ("symlink", os.fsencode(path.readlink()), mode)
        elif stat.S_ISDIR(metadata.st_mode):
            sources[relative] = ("directory", mode)
            for child in sorted(path.iterdir()):
                add_tree(child)
        elif stat.S_ISREG(metadata.st_mode):
            sources[relative] = ("file", tc.regular_input(root, relative), mode)
        else:
            sources[relative] = ("special", metadata.st_mode)

    def add_source(directory: Path, relative: object) -> None:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("Cargo source paths must be project-relative")
        current = directory
        for part in Path(relative).parts:
            if part == "..":
                if current == root:
                    raise ValueError("Cargo source path escapes the project")
                current = current.parent
            else:
                if part == ".git":
                    raise ValueError("Cargo source path enters Git administration")
                current = current / part
            if current.is_symlink():
                add_tree(current)
                return
        add_tree(current)

    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        path = tc.contained(root, name)
        state = cargo_file_state(root, name)
        if state is None:
            raise ValueError(f"Missing Cargo input manifest: {name}")
        names.add(name)
        document = ad.table(
            manifests.document(path, body=state[0].decode())[0], "Cargo manifest"
        )
        directory = path.parent
        parent = directory
        while True:
            names.update(
                str((parent / ".cargo" / filename).relative_to(root))
                for filename in ("config", "config.toml")
            )
            if parent == root:
                break
            parent = parent.parent
        if "package" in document:
            add_source(directory, "src")
            add_source(directory, "build.rs")
            build = ad.table(document["package"], "Cargo package").get("build")
            if isinstance(build, str):
                add_source(directory, build)
            for section in ("lib", "bin", "example", "test", "bench"):
                entries = document.get(section, [])
                for raw_target in (
                    [entries]
                    if isinstance(entries, Mapping)
                    else ad.array(entries, "Cargo targets")
                ):
                    target = ad.table(raw_target, "Cargo target")
                    if "path" in target:
                        add_source(directory, target["path"])

        def visit(table: Config) -> None:
            for key, value in table.items():
                if key in (
                    "dependencies",
                    "dev-dependencies",
                    "build-dependencies",
                    "patch",
                    "replace",
                ) and isinstance(value, Mapping):

                    def paths(values: Config) -> None:
                        for entry in values.values():
                            if isinstance(entry, Mapping):
                                if "path" in entry:
                                    local = tc.local_source(
                                        root, directory, entry["path"]
                                    )
                                    pending.append(
                                        str((local / "Cargo.toml").relative_to(root))
                                    )
                                else:
                                    paths(ad.table(entry, "Cargo dependency table"))

                    paths(ad.table(value, "Cargo dependency table"))
                elif isinstance(value, Mapping):
                    visit(ad.table(value, "Cargo manifest table"))

        visit(document)
    states: ad.Table = {
        name: cargo_file_state(root, name) for name in sorted(names - lock_names)
    }
    states["cargo-sources"] = sources
    # A non-Git public deps-resolve keeps the explicit Cargo closure above. Do
    # not invent ignore rules or require Git merely to use that public API.
    top = updates.git(root, "rev-parse", "--show-toplevel", check=False)
    if top and Path(top).resolve() == root.resolve():
        states["git-visible"] = {
            n: value
            for n, value in updates.snapshot(root).items()
            if n not in lock_names
        }
    return states


def cargo_lock_graph(body: bytes) -> dict[CargoNode, set[CargoNode]]:
    """Resolve exact native lock edges, including version and source collisions."""
    entries = tomllib.loads(body.decode()).get("package", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid Cargo lock package graph")
    nodes: dict[CargoNode, ad.Table] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid Cargo lock package graph")
        key = (entry.get("name"), entry.get("version"), entry.get("source", ""))
        if (
            not all(isinstance(part, str) for part in key)
            or not key[0]
            or not key[1]
            or key in nodes
        ):
            raise ValueError("Invalid or duplicate Cargo lock package identity")
        typed_key = (
            ad.text(key[0], "Cargo package name"),
            ad.text(key[1], "Cargo package version"),
            ad.text(key[2], "Cargo package source"),
        )
        nodes[typed_key] = ad.table(entry, "Cargo lock package")
    edges: dict[CargoNode, set[CargoNode]] = {}
    for node_key, node_entry in nodes.items():
        dependencies = node_entry.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise ValueError("Invalid Cargo lock dependency graph")
        edges[node_key] = set()
        for dependency in dependencies:
            match = (
                re.fullmatch(
                    r"([^\s()]+)(?: ([^\s()]+)(?: \(([^()\s]+)\))?)?",
                    dependency,
                )
                if isinstance(dependency, str)
                else None
            )
            if match is None:
                raise ValueError("Invalid Cargo lock dependency identity")
            name, version, source = match.groups()
            targets = [
                node
                for node in nodes
                if node[0] == name
                and (version is None or node[1] == version)
                and (source is None or node[2] == source)
            ]
            if version is None and len({node[1] for node in targets}) != 1:
                raise ValueError("Missing or ambiguous Cargo lock dependency version")
            if source is None:
                local = [node for node in targets if not node[2]]
                if local:
                    targets = local
            if len(targets) != 1:
                raise ValueError("Missing or ambiguous Cargo lock dependency source")
            edges[node_key].add(targets[0])
    return edges


def cargo_repair_order(
    issues: Sequence[CargoIssue], locks: Mapping[str, bytes]
) -> list[CargoIssue]:
    """Try ineligible locked dependents before the prerequisites they constrain."""
    ancestors: list[set[int]] = [set() for _ in issues]
    for workspace in dict.fromkeys(issue[0] for issue in issues):
        edges = cargo_lock_graph(locks[workspace])
        problems = {}
        for index, (name, identity, _) in enumerate(issues):
            if name != workspace:
                continue
            key = (
                identity[1],
                identity[2],
                "registry+https://github.com/rust-lang/crates.io-index",
            )
            if key not in edges:
                raise ValueError("Cargo repair identity is absent from the lock graph")
            problems[key] = index
        for origin, index in problems.items():
            seen = {origin}
            pending = list(edges[origin])
            while pending:
                target = pending.pop()
                if target in seen:
                    continue
                seen.add(target)
                if target in problems:
                    ancestors[problems[target]].add(index)
                pending.extend(edges[target] - seen)
    # Ancestor sets strictly grow downstream between strongly connected groups.
    # Members of a cycle tie, preserving the original deterministic order. The
    # graph is only a search heuristic: native constraints and audits still rule.
    return [
        issues[index]
        for index in sorted(range(len(issues)), key=lambda index: len(ancestors[index]))
    ]


def cargo_repair_peers(identity: Identity, body: bytes) -> list[CargoNode]:
    """Same-registry parents sharing an exact direct child of this target."""
    edges = cargo_lock_graph(body)
    target = (
        identity[1],
        identity[2],
        "registry+https://github.com/rust-lang/crates.io-index",
    )
    if target not in edges:
        raise ValueError("Cargo repair identity is absent from the lock graph")
    return sorted(
        node
        for node, children in edges.items()
        if node != target and node[2] == target[2] and children & edges[target]
    )


def cargo_resolve(
    root: Path,
    spec: Config,
    specs: Specs,
    planned: Plan,
    before: Config,
    policy: Config,
    now: datetime,
    max_attempts: int,
) -> ad.Table:
    """Bounded native lock repair, with exact selected direct manifest constraints."""
    command = ad.strings(
        ad.array(spec.get("resolve", [["cargo", "update"]]), "Cargo commands")[0],
        "Cargo command",
    )
    lock_names = {
        name: str(
            Path(ad.text(member["directory"], "Workspace directory")) / "Cargo.lock"
        )
        for name, member in specs.items()
    }
    initial = {path: cargo_file_state(root, path) for path in lock_names.values()}
    expected = dict(initial)
    public_inputs = cargo_input_state(root, specs, set(initial))
    baseline = identity_inventory(before["identities"])
    inventories: dict[str, list[registry.Release]] = {}
    attempts = 0
    visited = set()
    last = ""

    def graph() -> ResolutionGraph:
        for path in lock_names.values():
            if cargo_file_state(root, path) is None:
                raise ValueError("Missing resolved dependency lock: Cargo.lock")
        return {
            name: updates.lock_identities(root, [name], specs=specs) for name in specs
        }

    def inspect(current: ResolutionGraph) -> list[CargoIssue]:
        issues = []
        # Validate every identity before proposing a repair, even when an age
        # issue sorts before a different package's bad checksum or future date.
        for name, identities in current.items():
            for identity in sorted(identities):
                provider, package, value, url, digest = identity
                if provider != "crates":
                    raise ValueError("Cargo produced an unsupported registry identity")
                if package not in inventories:
                    inventories[package] = registry.releases(provider, package)
                releases = inventories[package]
                artifacts = [
                    a
                    for r in releases
                    if r.version == value
                    for a in r.artifacts
                    if a.digest == digest and (not url or a.url == url)
                ]
                if not artifacts:
                    raise ValueError(
                        f"Cargo artifact identity is absent from registry evidence: {package}@{value}"
                    )
                if max(a.published for a in artifacts) > now:
                    raise ValueError("Future Cargo artifact publication age")
                allowed = registry.maturity(
                    provider, releases, policy, package, now
                ) + registry.active_exceptions(provider, releases, policy, package, now)
                bound = registry.constraint(provider, policy, package)
                safe = registry.minimum_safe(provider, policy, package)

                def retainable(version: str) -> bool:
                    return registry.compatible(provider, version, bound) and (
                        safe is None
                        or registry.stable_version(provider, version) >= safe
                    )

                if (identity in baseline and retainable(value)) or any(
                    r.version == value for r in allowed
                ):
                    continue
                retained = [
                    r
                    for r in releases
                    if retainable(r.version)
                    and any(
                        old[:3] == (provider, package, r.version)
                        and any(
                            a.digest == old[4] and (not old[3] or a.url == old[3])
                            for a in r.artifacts
                        )
                        for old in baseline
                    )
                ]
                values = sorted(
                    {r.version for r in [*allowed, *retained]},
                    key=lambda v: registry.stable_version(provider, v),
                    reverse=True,
                )
                issues.append((name, identity, values))
        return issues

    def check_inputs() -> None:
        if cargo_input_state(root, specs, set(initial)) != pinned_inputs:
            raise ValueError(
                "Cargo resolution changed non-lock inputs; preserve and inspect"
            )

    def restore(checkpoint: Mapping[str, FileImage | None]) -> None:
        check_inputs()
        cargo_restore(root, checkpoint, expected)

    def run(name: str, argv: list[str], *, retry: bool = False) -> bool:
        check_inputs()
        old = dict(expected)
        for path, state in old.items():
            if cargo_file_state(root, path) != state:
                raise ValueError(
                    "Cargo lock changed before native resolution; preserve and inspect"
                )
        failure = None
        try:
            result = chainman.execute(
                root,
                ad.text(spec.get("profile", "rust"), "Adapter profile"),
                argv,
                cwd=tc.contained(
                    root, ad.text(specs[name]["directory"], "Workspace directory")
                ),
                env={**tc.environment(root), "TOOLCHAIN_FRESH": "1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if result is not None:
                print(result.stdout or "", end="")
        except subprocess.CalledProcessError as error:
            print(error.stdout or "", end="")
            failure = error
        try:
            check_inputs()
            current = {path: cargo_file_state(root, path) for path in initial}
            for path, state in current.items():
                if path != lock_names[name] and state != old[path]:
                    raise ValueError(
                        "Cargo changed another workspace lock; preserve and inspect"
                    )
                if old[path] is not None and state is None:
                    raise ValueError("Cargo lock disappeared; preserve and inspect")
                previous = old[path]
                if (
                    previous is not None
                    and state is not None
                    and previous[1] != state[1]
                ):
                    raise ValueError("Cargo lock mode changed; preserve and inspect")
            if failure is not None:
                if current != old:
                    raise ValueError(
                        "Failed Cargo command changed lock; preserve and inspect"
                    )
            else:
                expected.update(current)
        except (OSError, ValueError) as error:
            if failure is None:
                raise
            print(str(error), file=sys.stderr)
            failure.add_note(str(error))
            raise failure from error
        if failure is not None:
            first = next(
                (
                    line
                    for line in (failure.stdout or "").splitlines()
                    if line.startswith("error:")
                ),
                "",
            )
            if (
                retry
                and failure.returncode == 101
                and re.match(
                    r"error: failed to select a version for (?:the requirement )?`",
                    first,
                )
            ):
                return False
            raise failure
        return True

    def trial(name: str, argv: list[str]) -> bool:
        nonlocal attempts
        if attempts >= max_attempts:
            raise ValueError(
                f"Cargo eligibility solver exhausted its {max_attempts}-state bound: {last}"
            )
        attempts += 1
        return run(name, argv, retry=True)

    def search(current: ResolutionGraph, choices: CargoChoices) -> bool:
        nonlocal last
        issues = inspect(current)
        if any(identity not in current[name] for name, identity in choices):
            return False
        if not issues:
            return True
        key = (
            tuple((name, tuple(sorted(values))) for name, values in current.items()),
            tuple(choices),
        )
        if key in visited:
            last = "repeated Cargo artifact graph and repair choices"
            return False
        visited.add(key)
        images: dict[str, bytes] = {}
        for name, path in lock_names.items():
            image = expected[path]
            assert image is not None  # graph() requires every lock to exist.
            images[name] = image[0]
        name, identity, values = cargo_repair_order(issues, images)[0]
        package, observed = identity[1:3]
        checkpoint = dict(expected)
        kept = {chosen[1:3] for workspace, chosen in choices if workspace == name}
        peers = [
            peer
            for peer in cargo_repair_peers(identity, images[name])
            if peer[:2] not in kept
        ]
        for value in values:
            last = f"{name}: {package}@{observed} -> {value}"
            restore(checkpoint)
            argv = [
                command[0],
                "update",
                "-p",
                f"registry+https://github.com/rust-lang/crates.io-index#{package}@{observed}",
                "--precise",
                value,
            ]
            if not trial(name, argv):
                if not peers:
                    continue
                restore(checkpoint)
                # Cargo keeps one precise hint per registry source. Put the
                # requested target first; peers are unlocked, not individually
                # pinned. Verify the actual precise identity below even on exit0.
                cohort = [
                    argument
                    for peer, version, source in peers
                    for argument in ("-p", f"{source}#{peer}@{version}")
                ]
                if not trial(name, [*argv[:-2], *cohort, *argv[-2:]]):
                    continue
            candidate = graph()
            inspect(candidate)
            chosen = [
                item
                for item in candidate[name]
                if item[:3] == ("crates", package, value)
            ]
            if len(chosen) == 1:
                following = [*choices, (name, chosen[0])]
            else:
                survivors = {
                    item for item in candidate[name] if item[:2] == identity[:2]
                }
                previous = {item for item in current[name] if item[:2] == identity[:2]}
                if chosen or not survivors or not survivors <= previous - {identity}:
                    raise ValueError(
                        "Cargo did not materialize the requested precise registry identity"
                    )
                # Cargo can merge the target into an already present identity.
                # Keep prior choices, but let ineligible survivors be repaired.
                following = choices
            if search(candidate, following):
                return True
        restore(checkpoint)
        return False

    original = None
    try:
        with cargo_resolution_pins(root, planned):
            pinned_inputs = cargo_input_state(root, specs, set(initial))
            for name in specs:
                run(name, command)
            if not search(graph(), []):
                raise ValueError(
                    f"No eligible Cargo graph found within native repair search: {last}"
                )
            result = graph()
        if cargo_input_state(root, specs, set(initial)) != public_inputs:
            raise ValueError(
                "Cargo public inputs changed after temporary pin restoration"
            )
        updates.audit_locks(root, list(specs), baseline, policy, now, specs=specs)
        return {
            name: [list(item) for item in sorted(values)]
            for name, values in result.items()
        }
    except BaseException as error:
        original = error
        raise
    finally:
        if original is not None:
            cargo_restore(root, initial, expected, original)


def cargo_resolution_settings(spec: Config) -> tuple[bool, int]:
    """Validate repair effort before any adapter planning or registry work."""
    cargo_commands = spec.get("resolve", [["cargo", "update"]])
    repair_cargo = (
        ad.text(spec["adapter"], "Adapter kind") == "rust"
        and isinstance(cargo_commands, list)
        and len(cargo_commands) == 1
        and isinstance(cargo_commands[0], list)
        and len(cargo_commands[0]) == 2
        and all(isinstance(a, str) for a in cargo_commands[0])
        and Path(cargo_commands[0][0]).name == "cargo"
        and cargo_commands[0][1] == "update"
    )
    cargo_max_attempts = spec.get("cargo_max_attempts", CARGO_SOLVER_STATES)
    if "cargo_max_attempts" in spec and not repair_cargo:
        raise ValueError("cargo_max_attempts requires ordinary Rust cargo update")
    if repair_cargo and (
        type(cargo_max_attempts) is not int or not 1 <= cargo_max_attempts <= 512
    ):
        raise ValueError("cargo_max_attempts must be an integer from 1 to 512")
    assert isinstance(cargo_max_attempts, int)  # validated above or the default.
    return bool(repair_cargo), cargo_max_attempts


def resolve(root: Path, spec: Config, policy: Config, now: datetime) -> ad.Table:
    if spec.get("mode", "aggressive") not in {"aggressive", "compatible"}:
        raise ValueError("Native update policy must be aggressive or compatible")
    repair_cargo, cargo_max_attempts = cargo_resolution_settings(spec)
    specs = specifications(root, spec)
    selected = list(specs)
    if ad.text(spec["adapter"], "Adapter kind") == "swift":
        owned = {
            str(
                (
                    tc.contained(
                        root, ad.text(member["directory"], "Workspace directory")
                    )
                    / "Package.swift"
                ).relative_to(root)
            )
            for member in specs.values()
        }
        if any(
            ad.table(pin, "Swift pin").get("file") not in owned
            for pin in ad.array(spec.get("pins", []), "Swift pins")
        ):
            raise ValueError("Explicit Swift pin is not owned by a selected manifest")
        for member in specs.values():
            lock_adapters.validate_swift_sources(
                root, member, set(), require_locked=False
            )
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
        if (
            chosen
            and not pin.get("pub_override")
            and manifests.replace(pin, chosen, root)
        ):
            changed.append(ad.text(pin["file"], "Pin file"))
    manifests.configure_build_dependencies(root, selected, specs=specs)
    pub_resolution = (
        pub_resolve(root, spec, specs, planned, before, policy, now)
        if ad.text(spec["adapter"], "Adapter kind") == "flutter"
        else {}
    )
    cargo_identities = (
        cargo_resolve(
            root, spec, specs, planned, before, policy, now, cargo_max_attempts
        )
        if repair_cargo
        else None
    )
    swift_resolution: ad.Table = {}
    swift_selected: dict[str, dict[str, str]] = {}
    if ad.text(spec["adapter"], "Adapter kind") == "swift":
        if spec.get("pins"):
            for member in specs.values():
                lock_adapters.validate_swift_sources(
                    root, member, set(), require_locked=False
                )
        swift_resolution["swift_inputs"] = swift_input_state(root, specs)
        swift_selected = {
            name: {
                ad.text(pin["name"], "Pin name"): chosen.version
                for pin, chosen in planned
                if ad.text(pin["file"], "Pin file")
                in ad.strings(member["inputs"], "Workspace inputs")
                and chosen is not None
            }
            for name, member in specs.items()
        }
        swift_resolution["swift_selected"] = swift_selected
    with (
        swift_resolution_pins(root, planned, explicit=bool(spec.get("pins")))
        if ad.text(spec["adapter"], "Adapter kind") == "swift"
        else nullcontext()
    ):
        if ad.text(spec["adapter"], "Adapter kind") != "flutter" and not repair_cargo:
            for member in specs.values():
                directory = tc.contained(
                    root, ad.text(member["directory"], "Workspace directory")
                )
                kind = ad.text(spec["adapter"], "Adapter kind")
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
                commands = [
                    ad.strings(command, "Native resolution command")
                    for command in commands
                ]
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
                        root,
                        ad.text(spec.get("profile", kind), "Adapter profile"),
                        command,
                        cwd=directory,
                        env=env,
                    )
                if kind == "gradle":
                    # The ordinary dependencies task visits only one project. Traverse all
                    # resolvable project and buildscript configurations before final audit.
                    project_graphs.append(
                        gradle_graph(root, spec, directory, write=True)
                    )
                if kind == "python":
                    updates.retain_uv_noop(
                        root, member, old_manifest, old_lock, configured
                    )
    if swift_resolution:
        swift_resolution["swift_identities"] = {
            name: [
                list(item)
                for item in sorted(lock_adapters.identities(root, member))
                if item[1] in swift_selected[name]
            ]
            for name, member in specs.items()
        }
        before = {**before, "resolution": swift_resolution}
    audit(root, spec, before, policy, now)
    return {
        **pub_resolution,
        **swift_resolution,
        **(
            {"cargo_identities": cargo_identities}
            if cargo_identities is not None
            else {}
        ),
        "changed_manifests": sorted(set(changed)),
        "project_graphs": project_graphs,
        "pins": [
            {"pin": pin, "value": old_requirement(root, pin)} for pin, _ in planned
        ],
    }


def audit(
    root: Path, spec: Config, before: Config, policy: Config, now: datetime
) -> None:
    specs = specifications(root, spec)
    resolution = ad.table(before.get("resolution", {}), "Native resolution")
    if ad.text(spec["adapter"], "Adapter kind") == "swift":
        if (
            "swift_inputs" in resolution
            and swift_input_state(root, specs) != resolution["swift_inputs"]
        ):
            raise ValueError(
                "A native resolver or project hook changed guarded Swift inputs"
            )
        for name, raw_selected in ad.table(
            resolution.get("swift_selected", {}), "Swift selections"
        ).items():
            selected = ad.string_map(raw_selected, "Swift selected versions")
            current = lock_adapters.identities(root, specs[name])
            for package, version in selected.items():
                matching = [item for item in current if item[1] == package]
                if len(matching) != 1 or matching[0][2] != version:
                    raise ValueError(
                        "SwiftPM did not retain the selected direct release"
                    )
            expected = identity_inventory(
                ad.table(
                    resolution.get("swift_identities", {}), "Swift identities"
                ).get(name, [])
            )
            if {item for item in current if item[1] in selected} != expected:
                raise ValueError(
                    "A project hook changed a selected Swift artifact identity"
                )
    if ad.text(spec["adapter"], "Adapter kind") == "flutter":
        expected_pub = resolution.get("pub_inputs")
        if (
            expected_pub is not None
            and pub_input_state(root, ad.table(expected_pub, "Pub input state"))
            != expected_pub
        ):
            raise ValueError(
                "A project hook changed a guarded Pub manifest or override"
            )
        pub_validate_sources(root, specs)
        pub_validate_overrides(
            root,
            specs,
            {
                name: ad.string_map(value, "Pub selected overrides")
                for name, value in ad.table(
                    resolution.get("pub_overrides", {}), "Pub overrides"
                ).items()
            },
        )
    if spec.get("bootstrap_verification"):
        for member in specs.values():
            if not lock_adapters.paths(
                root,
                tc.contained(root, ad.text(member["directory"], "Workspace directory")),
                "*.lockfile",
            ):
                raise ValueError(
                    "Gradle resolution did not produce its required component locks"
                )
    for raw_item in ad.array(resolution.get("pins", []), "Native resolution pins"):
        item = ad.table(raw_item, "Native resolution pin")
        if (
            old_requirement(root, ad.table(item["pin"], "Dependency pin"))
            != item["value"]
        ):
            raise ValueError("A project hook changed a selected dependency pin")
    for name, expected_cargo in ad.table(
        resolution.get("cargo_identities", {}), "Cargo identities"
    ).items():
        if name not in specs or updates.lock_identities(
            root, [name], specs=specs
        ) != identity_inventory(expected_cargo):
            raise ValueError("A project hook changed the selected Cargo artifact graph")
    if spec.get("mode", "aggressive") == "compatible":
        for provider, package, value, _, _ in updates.lock_identities(
            root, list(specs), specs=specs
        ):
            for raw_bound in ad.array(
                before.get("requirements", []), "Original requirements"
            ):
                bound = ad.table(raw_bound, "Original requirement")
                if (
                    bound["provider"] == provider
                    and registry.package_name(
                        provider, ad.text(bound["name"], "Package name")
                    )
                    == package
                    and not accepts(
                        provider, value, ad.text(bound["range"], "Package range")
                    )
                ):
                    raise ValueError(
                        "Resolved dependency escaped its original compatible range"
                    )
    updates.audit_locks(
        root,
        list(specs),
        identity_inventory(before["identities"]),
        policy,
        now,
        specs=specs,
    )
    if ad.text(spec["adapter"], "Adapter kind") == "gradle":
        for member in specs.values():
            gradle_graph(
                root,
                spec,
                tc.contained(root, ad.text(member["directory"], "Workspace directory")),
                write=False,
            )
