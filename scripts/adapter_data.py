"""Validated projections of native adapter inputs; unknown metadata stays external.

These records contain the fields used for decisions, not a replacement lockfile
serializer. Source/identity, path and version policy remain with each adapter.
"""

from dataclasses import dataclass
from typing import NotRequired, TypedDict


type Table = dict[str, object]


def table(value: object, field: str) -> Table:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be an object with string keys")
    return {key: item for key, item in value.items()}


def text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def nonempty(value: object, field: str) -> str:
    result = text(value, field)
    if not result:
        raise ValueError(f"{field} must be nonempty")
    return result


def array(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    return list(value)


def nullable_array(value: object, field: str) -> list[object]:
    # Go emits null for unused lists. Other falsey values are not empty lists.
    return [] if value is None else array(value, field)


def strings(value: object, field: str) -> list[str]:
    return [text(item, field) for item in array(value, field)]


def string_map(value: object, field: str) -> dict[str, str]:
    return {key: text(item, field) for key, item in table(value, field).items()}


class NpmPackage(TypedDict, total=False):
    name: str
    version: str
    resolved: str
    integrity: str
    link: bool
    dependencies: dict[str, str]
    devDependencies: dict[str, str]
    optionalDependencies: dict[str, str]
    peerDependencies: dict[str, str]


class NpmLock(TypedDict):
    lockfileVersion: int
    packages: dict[str, NpmPackage]


def npm_lock(value: object) -> NpmLock:
    document = table(value, "npm lock")
    version = document.get("lockfileVersion")
    if type(version) is not int or version not in (2, 3):
        raise ValueError("npm auditing requires package-lock version 2 or 3")
    packages: dict[str, NpmPackage] = {}
    for location, raw in table(document.get("packages"), "npm packages").items():
        entry = table(raw, f"npm package {location!r}")
        package: NpmPackage = {}
        for key in ("name", "version", "resolved", "integrity"):
            if key in entry:
                package[key] = text(entry[key], f"npm {key}")
        if "link" in entry:
            link = entry["link"]
            if not isinstance(link, bool):
                raise ValueError("npm link must be boolean")
            if link and set(entry) - {"resolved", "link"}:
                raise ValueError("npm link must name one declared local workspace")
            package["link"] = link
        for section in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            if section in entry:
                package[section] = string_map(entry[section], f"npm {section}")
        packages[location] = package
    return {"lockfileVersion": version, "packages": packages}


@dataclass(frozen=True)
class GoReference:
    path: str
    version: str

    @classmethod
    def decode(cls, value: object, *, version_required: bool = False) -> "GoReference":
        data = table(value, "Go module reference")
        version = text(data.get("Version", ""), "Go Version")
        if version_required and not version:
            raise ValueError("Go requirement must include its Version")
        return cls(path=nonempty(data.get("Path"), "Go Path"), version=version)


@dataclass(frozen=True)
class GoReplacement:
    old: GoReference
    new: GoReference


@dataclass(frozen=True)
class GoFile:
    module: str | None
    requires: tuple[GoReference, ...]
    replacements: tuple[GoReplacement, ...]
    uses: tuple[str, ...]

    @classmethod
    def decode(cls, value: object, *, workspace: bool = False) -> "GoFile":
        data = table(value, "Go workspace" if workspace else "Go manifest")
        module = (
            None
            if workspace
            else nonempty(
                table(data.get("Module"), "Go Module").get("Path"), "Go module Path"
            )
        )
        replacements = []
        for value in nullable_array(data.get("Replace"), "Go Replace"):
            replacement = table(value, "Go replacement")
            replacements.append(
                GoReplacement(
                    old=GoReference.decode(replacement.get("Old")),
                    new=GoReference.decode(replacement.get("New")),
                )
            )
        return cls(
            module=module,
            requires=tuple(
                GoReference.decode(item, version_required=True)
                for item in nullable_array(data.get("Require"), "Go Require")
            ),
            replacements=tuple(replacements),
            uses=tuple(
                nonempty(table(item, "Go use entry").get("DiskPath"), "Go DiskPath")
                for item in nullable_array(data.get("Use"), "Go Use")
            ),
        )


class GoQuery(TypedDict):
    Path: str
    Version: str
    Retracted: list[str]
    Versions: NotRequired[list[str]]


def go_query(value: object, package: str, version: str) -> GoQuery:
    data = table(value, "Go module query")
    if (
        data.get("Path") != package
        or data.get("Error")
        or (version != "latest" and data.get("Version") != version)
    ):
        raise ValueError(
            "Go returned a different module identity or incomplete evidence"
        )
    result: GoQuery = {
        "Path": package,
        "Version": text(data.get("Version", ""), "Go query Version"),
        "Retracted": [
            text(item, "Go retraction")
            for item in nullable_array(data.get("Retracted"), "Go Retracted")
        ],
    }
    if "Versions" in data:
        result["Versions"] = strings(data["Versions"], "Go Versions")
    return result


class SwiftNode(TypedDict):
    identity: str
    name: str
    url: str
    version: str
    path: str
    dependencies: list[object]


def swift_node(value: object) -> SwiftNode:
    node = table(value, "SwiftPM graph node")
    if set(node) != {"identity", "name", "url", "version", "path", "dependencies"}:
        raise ValueError("Malformed SwiftPM resolved graph node")
    # Decode one level at a time: the adapter traverses the graph iteratively.
    return {
        "identity": nonempty(node["identity"], "SwiftPM identity"),
        "name": nonempty(node["name"], "SwiftPM name"),
        "url": nonempty(node["url"], "SwiftPM url"),
        "version": nonempty(node["version"], "SwiftPM version"),
        "path": nonempty(node["path"], "SwiftPM path"),
        "dependencies": array(node["dependencies"], "SwiftPM dependencies"),
    }
