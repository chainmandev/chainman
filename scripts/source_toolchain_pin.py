"""Select explicit npm SDK sources before refreshing their Nix derivations."""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
import hashlib
import json
import re
import stat
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import quote

import adapter_data as ad
import manifests
import registry
import toolchain as tc


class SourcePin(TypedDict):
    file: str
    pointer: list[str | int]
    platform_packages: NotRequired[dict[str, str]]


class ArtifactRecord(TypedDict):
    version: str
    url: str
    hash: str


class PlatformRecord(ArtifactRecord):
    package: str


class SourceRecord(ArtifactRecord):
    platforms: NotRequired[dict[str, PlatformRecord]]


def platform_packages(value: object) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(system, str)
            or re.fullmatch(r"[a-z0-9_]+-[a-z0-9_]+", system) is None
            or not isinstance(package, str)
            or len(package) > 214
            or re.fullmatch(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", package)
            is None
            for system, package in value.items()
        )
    ):
        raise ValueError("SDK platform packages require system-to-npm-package names")
    return dict(value)


def declaration(tool: Mapping[str, object]) -> SourcePin | None:
    pin = tool.get("source_pin")
    if pin is None:
        return None
    if (
        tool["provider"] != "npm"
        or not isinstance(pin, dict)
        or set(pin) - {"file", "pointer", "platform_packages"}
        or not {"file", "pointer"} <= set(pin)
        or not isinstance(pin["file"], str)
        or Path(pin["file"]).suffix not in {".json", ".toml", ".yaml", ".yml"}
        or not isinstance(pin["pointer"], list)
        or not pin["pointer"]
        or any(type(p) not in (str, int) for p in pin["pointer"])
    ):
        raise ValueError("SDK source pins require npm and a parsed file/pointer")
    result: SourcePin = {
        "file": ad.text(pin["file"], "SDK source file"),
        "pointer": manifests.pin_pointer(pin["pointer"]),
    }
    if "platform_packages" in pin:
        result["platform_packages"] = platform_packages(pin["platform_packages"])
        if tool["name"] in result["platform_packages"].values():
            raise ValueError("SDK platform package cannot be its parent package")
    return result


def packages(tool: Mapping[str, object]) -> dict[str, str]:
    pin = declaration(tool)
    return {} if pin is None else pin.get("platform_packages", {})


def artifact_record(package: str, release: registry.Release) -> ArtifactRecord:
    expected = (
        f"https://registry.npmjs.org/{package}/-/"
        f"{package.rsplit('/', 1)[-1]}-{release.version}.tgz"
    )
    matching = [a for a in release.artifacts if a.url == expected]
    if len(matching) != 1:
        raise ValueError("SDK source pin requires one canonical npm artifact")
    artifact = matching[0]
    if not re.fullmatch(
        r"sha256:[a-f0-9]{64}|sha384:[a-f0-9]{96}|sha512:[a-f0-9]{128}",
        artifact.digest,
    ):
        raise ValueError(
            "SDK source pin requires the canonical npm URL and strong hash"
        )
    algorithm, digest = artifact.digest.split(":")
    return {
        "version": release.version,
        "url": artifact.url,
        "hash": algorithm + "-" + base64.b64encode(bytes.fromhex(digest)).decode(),
    }


def record(tool: Mapping[str, object], release: registry.Release) -> SourceRecord:
    package = ad.text(tool["name"], "SDK package name")
    platforms = packages(tool)
    if len(release.artifacts) != 1 + len(set(platforms.values())):
        raise ValueError("SDK source pin requires its canonical npm artifact inventory")
    parent = artifact_record(package, release)
    result: SourceRecord = {
        "version": parent["version"],
        "url": parent["url"],
        "hash": parent["hash"],
    }
    if platforms:
        result["platforms"] = {
            system: {"package": name, **artifact_record(name, release)}
            for system, name in sorted(platforms.items())
        }
    return result


def npm_info(
    package: str, version: str, documents: dict[str, ad.Table] | None = None
) -> ad.Table:
    documents = {} if documents is None else documents
    if package not in documents:
        documents[package] = ad.table(
            registry.data(f"https://registry.npmjs.org/{quote(package, safe='')}"),
            "SDK npm package",
        )
    body = documents[package]
    info = ad.table(
        ad.table(body["versions"], "SDK npm versions").get(version),
        "SDK npm version",
    )
    if info.get("name") != package or info.get("version") != version:
        raise ValueError("SDK npm package identity mismatch")
    return info


def bind(
    tool: Mapping[str, object],
    release: registry.Release,
    inventories: dict[str, list[registry.Release]] | None = None,
    documents: dict[str, ad.Table] | None = None,
) -> registry.Release:
    """Bind exact optional native packages to their parent's dated artifacts."""
    platforms = packages(tool)
    if not platforms:
        return release
    documents = {} if documents is None else documents
    package = ad.text(tool["name"], "SDK package name")
    info = npm_info(package, release.version, documents)
    optional = ad.table(info.get("optionalDependencies", {}), "SDK optional packages")
    parent = artifact_record(package, release)
    artifacts = [a for a in release.artifacts if a.url == parent["url"]]
    dates = [release.published, *(a.published for a in artifacts)]
    inventories = {} if inventories is None else inventories
    for name in sorted(set(platforms.values())):
        if optional.get(name) != release.version:
            raise ValueError(
                "SDK parent must reference each platform package at its exact version"
            )
        if name not in inventories:
            inventories[name] = registry.releases("npm", name)
        matching = [r for r in inventories[name] if r.version == release.version]
        if len(matching) != 1:
            raise ValueError("SDK platform package lacks one matching dated release")
        child = matching[0]
        npm_info(name, child.version, documents)
        if len(child.artifacts) != 1:
            raise ValueError("SDK platform package requires one canonical npm artifact")
        artifact_record(name, child)
        artifacts.extend(child.artifacts)
        dates.extend([child.published, *(a.published for a in child.artifacts)])
    return registry.Release(
        release.version,
        max(dates),
        release.identity,
        release.python,
        tuple(artifacts),
        release.deprecated,
    )


def unique(pairs: list[tuple[str, object]]) -> ad.Table:
    result: ad.Table = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate SDK source JSON key")
        result[key] = value
    return result


def document(root: Path, pin: SourcePin) -> tuple[bytes, object, Callable[[], str]]:
    body = tc.regular_input(root, pin["file"])
    path = tc.contained(root, pin["file"])
    if path.suffix == ".json":
        json.loads(body, object_pairs_hook=unique)
    value, render = manifests.document(path, body=body.decode())
    return body, value, render


def read(root: Path, tool: Mapping[str, object]) -> SourceRecord | None:
    pin = declaration(tool)
    if pin is None:
        return None
    _, value, _ = document(root, pin)
    source = manifests.lookup(value, pin["pointer"])
    return decode(source)


def decode(source: object) -> SourceRecord:
    if (
        not isinstance(source, dict)
        or set(source) - {"version", "url", "hash", "platforms"}
        or not {"version", "url", "hash"} <= set(source)
        or any(not isinstance(source[k], str) for k in ("version", "url", "hash"))
        or registry.version("npm", source["version"]) is None
    ):
        raise ValueError("SDK source record requires version, URL and hash strings")
    registry.artifact_url(source["url"])
    registry.digest(source["hash"], npm=True)
    result: SourceRecord = {
        "version": ad.text(source["version"], "SDK version"),
        "url": ad.text(source["url"], "SDK URL"),
        "hash": ad.text(source["hash"], "SDK hash"),
    }
    if "platforms" in source:
        values = ad.table(source["platforms"], "SDK platform source records")
        names = platform_packages(
            {
                system: ad.table(value, "SDK platform source record").get("package")
                for system, value in values.items()
            }
        )
        records = {}
        for system, name in names.items():
            value = ad.table(values[system], "SDK platform source record")
            if set(value) != {"package", "version", "url", "hash"}:
                raise ValueError(
                    "SDK platform source record requires package and source fields"
                )
            child = decode({key: value[key] for key in ("version", "url", "hash")})
            if child["version"] != result["version"]:
                raise ValueError("SDK platform version differs from its parent")
            if not registry.digest(child["hash"], npm=True).startswith(
                ("sha256:", "sha384:", "sha512:")
            ):
                raise ValueError("SDK platform source requires a strong hash")
            records[system] = PlatformRecord(
                package=name,
                version=child["version"],
                url=child["url"],
                hash=child["hash"],
            )
        result["platforms"] = records
    return result


def files(root: Path, tools: Sequence[Mapping[str, object]]) -> dict[str, str]:
    return {
        pin["file"]: hashlib.sha256(tc.regular_input(root, pin["file"])).hexdigest()
        for tool in tools
        if (pin := declaration(tool)) is not None
    }


def write(
    root: Path,
    tool: Mapping[str, object],
    expected: SourceRecord,
    selected: SourceRecord,
) -> bool:
    pin = declaration(tool)
    if pin is None:
        raise ValueError("SDK source update requires a declared source pin")
    body, value, render = document(root, pin)
    if manifests.lookup(value, pin["pointer"]) != expected:
        raise ValueError("SDK source changed concurrently before its update")
    if expected == selected:
        return False
    path = tc.contained(root, pin["file"])
    mode = stat.S_IMODE(path.stat().st_mode)
    manifests.assign(value, pin["pointer"], selected)
    updated = render().encode()
    if tc.regular_input(root, pin["file"]) != body:
        raise ValueError("SDK source document changed concurrently during its update")
    tc.atomic_bytes(tc.contained(root, pin["file"]), updated, mode)
    return True


def inventory(
    releases: list[registry.Release],
    current: str,
    mode: str,
    *,
    tool: Mapping[str, object] | None = None,
) -> list[registry.Release]:
    if mode not in {"aggressive", "compatible"}:
        raise ValueError("SDK source selection mode must be aggressive or compatible")
    # All npm candidates carry artifact dates. Bind their maximum date before
    # eligibility, including security-exception retirement, sees the inventory.
    platforms = {} if tool is None else packages(tool)
    inventories: dict[str, list[registry.Release]] = {}
    documents: dict[str, ad.Table] = {}
    values = []
    for release in releases:
        if mode == "compatible" and not registry.compatible(
            "npm", release.version, "^" + current
        ):
            continue
        if tool is not None and platforms:
            if registry.stable_version(
                "npm", release.version
            ) < registry.stable_version("npm", current):
                optional = ad.table(
                    npm_info(
                        ad.text(tool["name"], "SDK package name"),
                        release.version,
                        documents,
                    ).get("optionalDependencies", {}),
                    "SDK optional packages",
                )
                # Older versions may predate this native platform inventory.
                # Fully bound older releases remain policy evidence: their
                # maturity can retire an exception for the current release.
                if any(
                    optional.get(name) != release.version for name in platforms.values()
                ):
                    continue
            release = bind(tool, release, inventories, documents)
        values.append(release)
    return [
        registry.Release(
            r.version,
            max([r.published, *(a.published for a in r.artifacts)]),
            r.identity,
            r.python,
            r.artifacts,
            r.deprecated,
        )
        for r in values
    ]


def select(
    tool: Mapping[str, object],
    before: Mapping[str, object] | None,
    source: SourceRecord,
    pins: list[str],
    policy: ad.Table,
    now: datetime,
    *,
    mode: str = "aggressive",
) -> SourceRecord:
    import source_toolchain as sdk

    observed, releases = sdk.observe(tool, source["version"])
    releases = inventory(releases, source["version"], mode, tool=tool)
    package = ad.text(tool["name"], "SDK package name")
    candidates = registry.maturity("npm", releases, policy, package, now)
    candidates += registry.active_exceptions("npm", releases, policy, package, now)
    latest = max(
        candidates,
        key=lambda r: registry.stable_version("npm", r.version),
        default=None,
    )
    if latest is None or registry.stable_version(
        "npm", latest.version
    ) < registry.stable_version("npm", source["version"]):
        latest = observed
    retained = (
        sdk.evidence(tool, latest, latest.version) == before
        and record(tool, latest) == source
        and [
            sdk.render(ad.table(pin, "SDK output pin"), latest.version)
            for pin in ad.array(tool["pins"], "SDK output pins")
        ]
        == pins
    )
    sdk.eligible_tool(tool, latest, releases, policy, now, retained=retained)
    return record(tool, latest)
