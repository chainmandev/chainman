"""Discover ordinary workspace dependencies without erasing manifest structure."""

from __future__ import annotations

from collections.abc import Mapping
import io
import json
from pathlib import Path
import re
import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from ruamel.yaml import YAML

from toolchain import contained, module


def document(path: Path, *, body: str | None = None):
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

    def render():
        buffer = io.StringIO()
        yaml.dump(value, buffer)
        return buffer.getvalue()

    return value, render


def lookup(value, pointer):
    for component in pointer:
        value = value[component]
    return value


def assign(value, pointer, replacement):
    if not pointer:
        raise ValueError("A dependency pointer cannot replace an entire document")
    target = lookup(value, pointer[:-1])
    target[pointer[-1]] = replacement


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


def discover(root: Path, selected: list[str]) -> list[dict]:
    pins = []

    def add(pin):
        if pin:
            pins.append(pin)

    for name in selected:
        spec = module(name, root)
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
                                    if requirement.get("path") or requirement.get(
                                        "workspace"
                                    ):
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
            path = directory / "pubspec.yaml"
            rel = path.relative_to(root).as_posix()
            content = document(path)[0]
            for section in ("dependencies", "dev_dependencies", "dependency_overrides"):
                for dependency, requirement in content.get(section, {}).items():
                    if isinstance(requirement, Mapping):
                        if requirement.get("sdk") or requirement.get("path"):
                            continue
                        raise ValueError(
                            "Non-hosted Dart dependency needs an explicit release pin"
                        )
                    pins.append(
                        {
                            "provider": "pub",
                            "name": dependency,
                            "file": rel,
                            "pointer": [section, dependency],
                            "prefix": "^",
                        }
                    )
    return pins


def configure_build_dependencies(
    root: Path, selected: list[str], *, check: bool = False, validate_only: bool = False
) -> None:
    """Put declared Python build requirements into the ordinary audited workspace lock."""
    for name in selected:
        spec = module(name, root)
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
