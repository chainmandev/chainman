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
from typing import TypedDict

import adapter_data as ad
import manifests
import registry
import toolchain as tc


class SourcePin(TypedDict):
    file: str
    pointer: list[str | int]


class SourceRecord(TypedDict):
    version: str
    url: str
    hash: str


def declaration(tool: Mapping[str, object]) -> SourcePin | None:
    pin = tool.get("source_pin")
    if pin is None:
        return None
    if (
        tool["provider"] != "npm"
        or not isinstance(pin, dict)
        or set(pin) != {"file", "pointer"}
        or not isinstance(pin["file"], str)
        or Path(pin["file"]).suffix not in {".json", ".toml", ".yaml", ".yml"}
        or not isinstance(pin["pointer"], list)
        or not pin["pointer"]
        or any(type(p) not in (str, int) for p in pin["pointer"])
    ):
        raise ValueError("SDK source pins require npm and a parsed file/pointer")
    return {
        "file": ad.text(pin["file"], "SDK source file"),
        "pointer": manifests.pin_pointer(pin["pointer"]),
    }


def record(tool: Mapping[str, object], release: registry.Release) -> SourceRecord:
    if len(release.artifacts) != 1:
        raise ValueError("SDK source pin requires one canonical npm artifact")
    artifact = release.artifacts[0]
    package = ad.text(tool["name"], "SDK package name")
    expected = (
        f"https://registry.npmjs.org/{package}/-/"
        f"{package.rsplit('/', 1)[-1]}-{release.version}.tgz"
    )
    if artifact.url != expected or not re.fullmatch(
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
        or set(source) != {"version", "url", "hash"}
        or any(not isinstance(v, str) for v in source.values())
        or registry.version("npm", source["version"]) is None
    ):
        raise ValueError("SDK source record requires version, URL and hash strings")
    registry.artifact_url(source["url"])
    registry.digest(source["hash"], npm=True)
    return {
        "version": ad.text(source["version"], "SDK version"),
        "url": ad.text(source["url"], "SDK URL"),
        "hash": ad.text(source["hash"], "SDK hash"),
    }


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
    releases: list[registry.Release], current: str, mode: str
) -> list[registry.Release]:
    if mode not in {"aggressive", "compatible"}:
        raise ValueError("SDK source selection mode must be aggressive or compatible")
    # All npm candidates carry artifact dates. Bind their maximum date before
    # eligibility, including security-exception retirement, sees the inventory.
    return [
        registry.Release(
            r.version,
            max([r.published, *(a.published for a in r.artifacts)]),
            r.identity,
            r.python,
            r.artifacts,
        )
        for r in releases
        if mode == "aggressive" or registry.compatible("npm", r.version, "^" + current)
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
    releases = inventory(releases, source["version"], mode)
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
