"""Discover ordinary workspace dependencies without erasing manifest structure."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, MutableSequence, Sequence
import io
import json
from pathlib import Path
import re
import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from ruamel.yaml import YAML

from toolchain import contained, local_source, module, regular_input


def document(
    path: Path, *, body: str | None = None
) -> tuple[object, Callable[[], str]]:
    body = path.read_text() if body is None else body
    suffix = path.suffix
    if suffix == ".json":
        value = json.loads(body)
        return value, lambda: json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if suffix == ".toml":
        value = tomlkit.parse(body)
        return value, lambda: tomlkit.dumps(value)
    yaml = YAML()
    yaml.preserve_quotes = True
    value = yaml.load(body)

    def render() -> str:
        buffer = io.StringIO()
        yaml.dump(value, buffer)
        return buffer.getvalue()

    return value, render


def lookup(value: object, pointer: Sequence[str | int]) -> object:
    for component in pointer:
        if isinstance(value, Mapping):
            value = value[component]
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if not isinstance(component, int):
                raise ValueError("A manifest sequence requires an integer index")
            value = value[component]
        else:
            raise ValueError("A manifest pointer must traverse mappings or sequences")
    return value


def assign(value: object, pointer: Sequence[str | int], replacement: object) -> None:
    if not pointer:
        raise ValueError("A dependency pointer cannot replace an entire document")
    target = lookup(value, pointer[:-1])
    component = pointer[-1]
    if isinstance(target, MutableMapping):
        target[component] = replacement
    elif isinstance(target, MutableSequence):
        if not isinstance(component, int):
            raise ValueError("A manifest sequence requires an integer index")
        target[component] = replacement
    else:
        raise ValueError("A manifest pointer must select a mutable container")


def js_pin(file: str, pointer: list, name: str, value: str):
    if not isinstance(value, str):
        raise ValueError("Expected a dependency version string")
    if value.startswith(("workspace:", "catalog:", "file:", "link:")):
        return None
    prefix = ""
    if value.startswith("npm:"):
        match = re.fullmatch(r"npm:(.+)@([^@]+)", value)
        if not match:
            raise ValueError("Unsupported npm alias")
        name, value = match.groups()
        prefix = f"npm:{name}@"
    if not re.fullmatch(r"@?[^@ >]+(?:/[^@ >]+)?", name):
        raise ValueError("Complex override selectors need an explicit dependency pin")
    if value.startswith(("https:", "git:", "github:", "git+", "patch:")):
        raise ValueError(
            "Remote/patch dependency needs an explicit immutable pin and age source"
        )
    return {
        "provider": "npm",
        "name": name,
        "file": file,
        "pointer": pointer,
        "prefix": prefix + (value[0] if value[:1] in ("^", "~") else ""),
    }


def pub_workspace(root: Path, directory: Path) -> dict:
    """Read one native Pub resolution group without importing dependency overrides."""
    guarded = set()
    documents = {}

    def read(path):
        name = path.relative_to(root).as_posix()
        guarded.add(name)
        value = document(path, body=regular_input(root, name).decode())[0]
        if not isinstance(value, Mapping):
            raise ValueError(f"Pub manifest must be a mapping: {name}")
        return value

    def effective(path):
        if path not in documents:
            value = dict(read(path))
            owners = {key: path for key in value}
            sibling = contained(
                root, str(path.with_name("pubspec_overrides.yaml").relative_to(root))
            )
            guarded.add(sibling.relative_to(root).as_posix())
            if sibling.exists():
                override = read(sibling)
                if set(override) - {"dependency_overrides", "workspace", "resolution"}:
                    raise ValueError("Unsupported Pub override-file attribute")
                value.update(override)
                owners.update({key: sibling for key in override})
            if value.get("resolution") not in (None, "workspace"):
                raise ValueError("Unsupported Pub resolution attribute")
            documents[path] = value, owners
        return documents[path]

    def members(path):
        value, _ = effective(path)
        entries = value.get("workspace", [])
        if not isinstance(entries, list) or any(
            not isinstance(item, str) or not item or any(c in item for c in "*?[")
            for item in entries
        ):
            raise ValueError("Pub workspace requires literal member directories")
        found = [path]
        for item in entries:
            child = contained(
                root,
                str((contained(path.parent, item) / "pubspec.yaml").relative_to(root)),
            )
            member, _ = effective(child)
            if child in found or member.get("resolution") != "workspace":
                raise ValueError("Pub workspace has duplicate or non-workspace members")
            if member.get("workspace"):
                raise ValueError("Nested Pub workspaces are not supported")
            found.append(child)
        return found

    entry = contained(root, str((directory / "pubspec.yaml").relative_to(root)))
    value, _ = effective(entry)
    owner = entry
    if value.get("resolution") == "workspace":
        owner = None
        for parent in directory.parents:
            if not parent.is_relative_to(root):
                break
            candidate = contained(
                root, str((parent / "pubspec.yaml").relative_to(root))
            )
            guarded.add(candidate.relative_to(root).as_posix())
            if candidate.exists() and entry in members(candidate):
                owner = candidate
                break
        if owner is None:
            raise ValueError(
                "Pub workspace member has no declared containing workspace"
            )
    paths = members(owner)
    overrides = {}
    local_members = {}
    for path in paths:
        value, owners = effective(path)
        package = value.get("name")
        if not isinstance(package, str) or not package or package in local_members:
            raise ValueError("Pub workspace requires distinct package names")
        local_members[package] = path.parent
        declared = value.get("dependency_overrides", {})
        if not isinstance(declared, Mapping):
            raise ValueError("Pub dependency_overrides must be a mapping")
        for package, requirement in declared.items():
            if package in overrides:
                raise ValueError("Duplicate effective Pub workspace override")
            overrides[package] = (requirement, owners["dependency_overrides"])

    sources = {}
    pins = []

    def classify(package, requirement, path, pointer, *, override=False):
        if not isinstance(package, str) or not package:
            raise ValueError("Pub dependency requires a package name")
        if isinstance(requirement, Mapping):
            if set(requirement) == {"path"}:
                relative = requirement["path"]
                if not isinstance(relative, str) or not relative:
                    raise ValueError("Pub path dependency requires a nonempty path")
                target = local_source(root, path.parent, relative)
                target_manifest = target / "pubspec.yaml"
                if read(target_manifest).get("name") != package:
                    raise ValueError("Pub local dependency package identity mismatch")
                source = {"kind": "path", "path": target.relative_to(root).as_posix()}
            elif set(requirement) == {"sdk"} and requirement["sdk"] == "flutter":
                source = {"kind": "sdk", "sdk": "flutter"}
            else:
                raise ValueError(
                    "Unsupported Pub dependency source; expected hosted range, contained path or Flutter SDK"
                )
            if package in sources and sources[package] != source:
                raise ValueError("Conflicting Pub local dependency sources")
            sources[package] = source
            return
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("Pub hosted dependency requires a version range")
        pins.append(
            {
                "provider": "pub",
                "name": package,
                "file": path.relative_to(root).as_posix(),
                "pointer": pointer,
                "prefix": "^",
                "pub_directory": owner.parent.relative_to(root).as_posix(),
                **({"pub_override": True, "bound": requirement} if override else {}),
            }
        )

    for package, (requirement, path) in overrides.items():
        classify(
            package, requirement, path, ["dependency_overrides", package], override=True
        )
    for path in paths:
        value, _ = effective(path)
        for section in ("dependencies", "dev_dependencies"):
            table = value.get(section, {})
            if not isinstance(table, Mapping):
                raise ValueError("Pub dependency sections must be mappings")
            for package, requirement in table.items():
                if package in overrides:
                    continue
                if (
                    len(paths) > 1
                    and package in local_members
                    and isinstance(requirement, str)
                ):
                    sources[package] = {
                        "kind": "workspace",
                        "path": local_members[package].relative_to(root).as_posix(),
                    }
                    continue
                classify(package, requirement, path, [section, package])
    return {
        "directory": owner.parent.relative_to(root).as_posix(),
        "inputs": [path.relative_to(root).as_posix() for path in paths],
        "guarded_inputs": sorted(guarded),
        "sources": sources,
        "pins": pins,
    }


def discover(
    root: Path, selected: list[str], *, specs: dict | None = None
) -> list[dict]:
    pins = []

    def add(pin):
        if pin:
            pins.append(pin)

    for name in selected:
        spec = module(name, root) if specs is None else specs[name]
        kind = spec.get("ecosystem")
        directory = contained(root, spec["directory"])
        if kind == "npm":
            manifests = {directory / "package.json"}
            workspace_file = directory / "pnpm-workspace.yaml"
            workspace = {}
            if workspace_file.exists():
                workspace = document(workspace_file)[0]
                patterns = workspace.get("packages", [])
            else:
                patterns = json.loads((directory / "package.json").read_text()).get(
                    "workspaces", []
                )
                if isinstance(patterns, dict):
                    patterns = patterns.get("packages", [])
            included, excluded = set(), set()
            for pattern in patterns:
                negative = pattern.startswith("!")
                pattern = pattern.removeprefix("!")
                contained(directory, pattern)
                found = set(directory.glob(pattern + "/package.json"))
                (excluded if negative else included).update(found)
            manifests.update(included - excluded)
            for path in sorted(manifests):
                rel = path.relative_to(root).as_posix()
                contained(root, rel)
                content = document(path)[0]
                for section in (
                    "dependencies",
                    "devDependencies",
                    "optionalDependencies",
                    "peerDependencies",
                ):
                    for dependency, requirement in content.get(section, {}).items():
                        add(js_pin(rel, [section, dependency], dependency, requirement))
                for dependency, requirement in (
                    content.get("pnpm", {}).get("overrides", {}).items()
                ):
                    add(
                        js_pin(
                            rel,
                            ["pnpm", "overrides", dependency],
                            dependency,
                            requirement,
                        )
                    )
            if workspace:
                rel = workspace_file.relative_to(root).as_posix()
                for dependency, requirement in workspace.get("catalog", {}).items():
                    add(js_pin(rel, ["catalog", dependency], dependency, requirement))
                for catalog, entries in workspace.get("catalogs", {}).items():
                    for dependency, requirement in entries.items():
                        add(
                            js_pin(
                                rel,
                                ["catalogs", catalog, dependency],
                                dependency,
                                requirement,
                            )
                        )
                for dependency, requirement in workspace.get("overrides", {}).items():
                    add(js_pin(rel, ["overrides", dependency], dependency, requirement))
        elif kind == "crates":
            # Only explicitly declared manifest globs; never walk downloaded/vendor source.
            paths = set()
            for pattern in spec["inputs"]:
                paths.update(p for p in root.glob(pattern) if p.name == "Cargo.toml")
            for path in sorted(paths):
                rel = path.relative_to(root).as_posix()
                content = document(contained(root, rel))[0]

                def visit(table, pointer):
                    for key, value in table.items():
                        if key in (
                            "dependencies",
                            "dev-dependencies",
                            "build-dependencies",
                        ):
                            for dependency, requirement in value.items():
                                location = pointer + [key, dependency]
                                actual = dependency
                                if isinstance(requirement, Mapping):
                                    if requirement.get("path"):
                                        local_source(
                                            root, path.parent, requirement["path"]
                                        )
                                        continue
                                    if requirement.get("workspace"):
                                        continue
                                    if requirement.get("git"):
                                        raise ValueError(
                                            "Git Cargo dependencies need an explicit release pin"
                                        )
                                    actual = requirement.get("package", dependency)
                                    location.append("version")
                                    requirement = requirement.get("version")
                                if not isinstance(requirement, str):
                                    raise ValueError("Cargo dependency lacks a version")
                                pins.append(
                                    {
                                        "provider": "crates",
                                        "name": actual,
                                        "file": rel,
                                        "pointer": location,
                                        "prefix": "",
                                    }
                                )
                        elif isinstance(value, Mapping):
                            visit(value, pointer + [key])

                visit(content, [])
        elif kind == "pypi":
            paths = set()
            for pattern in spec["inputs"]:
                paths.update(
                    p for p in root.glob(pattern) if p.name == "pyproject.toml"
                )
            for path in sorted(paths):
                rel = path.relative_to(root).as_posix()
                content = document(contained(root, rel))[0]
                groups = [
                    (
                        content.get("project", {}).get("dependencies", []),
                        ["project", "dependencies"],
                    )
                ]
                groups.append(
                    (
                        content.get("build-system", {}).get("requires", []),
                        ["build-system", "requires"],
                    )
                )
                for key, values in (
                    content.get("project", {}).get("optional-dependencies", {}).items()
                ):
                    groups.append((values, ["project", "optional-dependencies", key]))
                for key, values in content.get("dependency-groups", {}).items():
                    if key == spec.get("build_dependency_group"):
                        continue  # Derived from build-system.requires, never another authoritative pin.
                    groups.append((values, ["dependency-groups", key]))
                for values, prefix in groups:
                    for index, requirement in enumerate(values):
                        if isinstance(requirement, Mapping) and set(requirement) == {
                            "include-group"
                        }:
                            continue
                        parsed = Requirement(requirement)
                        if parsed.url:
                            raise ValueError(
                                "Python direct URLs need an explicit artifact pin"
                            )
                        sources = (
                            content.get("tool", {}).get("uv", {}).get("sources", {})
                        )
                        if parsed.name in sources and (
                            sources[parsed.name].get("workspace")
                            or sources[parsed.name].get("path")
                        ):
                            if sources[parsed.name].get("path"):
                                local_source(
                                    root, path.parent, sources[parsed.name]["path"]
                                )
                            continue
                        pins.append(
                            {
                                "provider": "pypi",
                                "name": parsed.name,
                                "file": rel,
                                "pointer": prefix + [index],
                                "representation": "requirement",
                            }
                        )
        elif kind == "pub":
            group = pub_workspace(root, directory)
            declared = set()
            for pattern in spec.get("inputs", []):
                contained(root, pattern)
                declared.update(
                    str(p.relative_to(root))
                    for p in root.glob(pattern)
                    if p.name == "pubspec.yaml"
                )
            if declared and declared != set(group["inputs"]):
                raise ValueError(
                    "Pub manifest inputs must cover exactly one complete native resolution group"
                )
            pins.extend(group["pins"])
    return pins


def configure_build_dependencies(
    root: Path,
    selected: list[str],
    *,
    check: bool = False,
    validate_only: bool = False,
    specs: dict | None = None,
) -> None:
    """Put declared Python build requirements into the ordinary audited workspace lock."""
    for name in selected:
        spec = module(name, root) if specs is None else specs[name]
        group = spec.get("build_dependency_group")
        if not group:
            continue
        if spec.get("ecosystem") != "pypi" or not re.fullmatch(
            r"[a-z][a-z0-9-]+", group
        ):
            raise ValueError("Unsupported derived build dependency group")
        requirements, paths = {}, set()
        for pattern in spec["inputs"]:
            paths.update(p for p in root.glob(pattern) if p.name == "pyproject.toml")
        for path in sorted(paths):
            content = document(contained(root, str(path.relative_to(root))))[0]
            for raw in content.get("build-system", {}).get("requires", []):
                parsed = Requirement(raw)
                if parsed.url or parsed.marker:
                    raise ValueError(
                        "Build dependency groups require unconditional registry requirements"
                    )
                key = canonicalize_name(parsed.name)
                parsed.name = key
                requirement = str(parsed)
                if key in requirements and requirements[key] != requirement:
                    raise ValueError(
                        "Conflicting per-package Python build requirements"
                    )
                requirements[key] = requirement
        path = contained(root, spec["directory"] + "/pyproject.toml")
        if str(path.relative_to(root)) not in spec.get("update_outputs", []):
            raise ValueError("Derived build group must be a declared update output")
        if validate_only:
            continue
        content, render = document(path)
        old = content.get("dependency-groups", {}).get(group)
        expected = [requirements[key] for key in sorted(requirements)]
        if old != expected:
            if check:
                raise ValueError(
                    "Derived Python build dependency group disagrees with build-system.requires"
                )
            content.setdefault("dependency-groups", {})[group] = expected
            path.write_text(render())


def replace(pin: dict, release, root: Path) -> bool:
    path = contained(root, pin["file"])
    if pin.get("format") == "regex":
        body = path.read_text()
        matches = list(re.finditer(pin["pattern"], body, re.MULTILINE))
        if len(matches) != 1 or "value" not in matches[0].groupdict():
            raise ValueError("Explicit pin must match exactly one named value group")
        match = matches[0]
        replacement = (
            release.identity
            if pin.get("identity")
            else (
                release.version
                if pin["provider"] == "go"
                else release.version.removeprefix("v")
            )
        )
        if pin.get("representation") == "action":
            replacement = release.identity + " # " + release.version
        old = match.group("value")
        body = body[: match.start("value")] + replacement + body[match.end("value") :]
        if body != path.read_text():
            path.write_text(body)
        return old != replacement
    value, render = document(path)
    old = lookup(value, pin["pointer"])
    if pin.get("representation") == "requirement":
        parsed = Requirement(old)
        extras = "[" + ",".join(sorted(parsed.extras)) + "]" if parsed.extras else ""
        replacement = f"{parsed.name}{extras}=={release.version}"
        if parsed.marker:
            replacement += f"; {parsed.marker}"
    else:
        replacement = pin.get("prefix", "") + release.version.removeprefix("v")
    if replacement == old:
        return False
    assign(value, pin["pointer"], replacement)
    path.write_text(render())
    return True
