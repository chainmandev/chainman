"""Synchronize declared pins with dated tool versions supplied by a Nix profile."""

from __future__ import annotations

import json
from collections.abc import Mapping
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import NotRequired, TypedDict

import adapter_data as ad
import chainman
import manifests
import registry
import source_toolchain_pin as source_pin
import toolchain as tc
from packaging.version import Version


class Tool(TypedDict):
    provider: str
    name: str
    command: list[str]
    version_pattern: str
    pins: list[ad.Table]
    source_pin: NotRequired[source_pin.SourcePin]


class Evidence(TypedDict):
    provider: str
    name: str
    version: str
    release: str
    identity: str
    published: str
    artifacts: list[list[str]]


class SelectedTool(Evidence):
    pins: list[str]
    source: NotRequired[source_pin.SourceRecord]


class Snapshot(TypedDict):
    adapter: str
    pins: list[list[str]]
    tools: list[Evidence]
    sources: list[source_pin.SourceRecord | None]
    source_files: dict[str, str]


class Resolution(TypedDict):
    changed: list[str]
    tools: list[SelectedTool]


class Baseline(TypedDict):
    pins: list[list[str]]
    tools: list[ad.Table | None]
    sources: list[source_pin.SourceRecord | None]


def read_baseline(value: Mapping[str, object], declarations: list[Tool]) -> Baseline:
    count = len(declarations)
    pins = [
        ad.strings(item, "SDK baseline pins")
        for item in ad.array(value["pins"], "SDK baseline pin inventory")
    ]
    if len(pins) != count:
        raise ValueError("Toolchain declaration changed during resolution")
    if any(
        len(pin) != len(tool["pins"])
        for pin, tool in zip(pins, declarations, strict=True)
    ):
        raise ValueError("Toolchain output inventory changed during resolution")
    tools = ad.array(value.get("tools", [None] * count), "SDK baseline tools")
    if len(tools) != count:
        raise ValueError("Toolchain observation inventory changed during resolution")
    sources = ad.array(value.get("sources", [None] * count), "SDK baseline sources")
    if len(sources) != count:
        raise ValueError("Toolchain source inventory changed during resolution")
    if any(
        "source_pin" in tool and source is None
        for tool, source in zip(declarations, sources, strict=True)
    ):
        raise ValueError("SDK source observation is missing from its baseline")
    return {
        "pins": pins,
        "tools": [
            None if item is None else ad.table(item, "SDK baseline tool")
            for item in tools
        ],
        "sources": [
            None if item is None else source_pin.decode(item) for item in sources
        ],
    }


def tools(spec: Mapping[str, object]) -> list[Tool]:
    values = spec.get("tools")
    if not isinstance(values, list) or not values:
        raise ValueError("Toolchain synchronization requires declared tools")
    seen = set()
    source_paths: list[tuple[str, list[str | int]]] = []
    result: list[Tool] = []
    for raw in values:
        tool = ad.table(raw, "SDK tool")
        if tool.get("provider") not in {"npm", "pypi", "crates", "github"}:
            raise ValueError("Unsupported toolchain registry evidence provider")
        if not isinstance(tool.get("name"), str) or not tool["name"]:
            raise ValueError("Toolchain tool requires a public registry identity")
        command = tool.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(a, str) or "\0" in a for a in command)
        ):
            raise ValueError("Toolchain probes require literal argument arrays")
        if not isinstance(tool.get("pins"), list) or not tool["pins"]:
            raise ValueError("A synchronized tool requires declared output pins")
        version_pattern = ad.text(tool["version_pattern"], "SDK version pattern")
        pattern = re.compile(version_pattern, re.MULTILINE)
        if "version" not in pattern.groupindex:
            raise ValueError("Toolchain probe pattern requires a named version group")
        source = source_pin.declaration(tool)
        if source is not None:
            source_paths.append((source["file"], source["pointer"]))
        pins = [
            ad.table(pin, "SDK output pin")
            for pin in ad.array(tool["pins"], "SDK output pins")
        ]
        for pin in pins:
            if set(pin) - {"file", "pointer", "format", "pattern", "value"}:
                raise ValueError(
                    "Toolchain pins use file/pointer or regex plus a value template"
                )
            key = (
                ad.text(pin["file"], "SDK output file"),
                json.dumps(pin.get("pointer", pin.get("pattern"))),
            )
            if key in seen:
                raise ValueError("A toolchain pin has multiple owners")
            seen.add(key)
        projected: Tool = {
            "provider": ad.text(tool["provider"], "SDK provider"),
            "name": ad.text(tool["name"], "SDK package"),
            "command": ad.strings(command, "SDK command"),
            "version_pattern": version_pattern,
            "pins": pins,
        }
        if source is not None:
            projected["source_pin"] = source
        result.append(projected)
    for index, (file, pointer) in enumerate(source_paths):
        others = source_paths[index + 1 :] + [
            (
                ad.text(pin["file"], "SDK output file"),
                manifests.pin_pointer(pin.get("pointer", [])),
            )
            for item in result
            for pin in item["pins"]
        ]
        if any(
            file == other_file
            and pointer[: min(len(pointer), len(other))]
            == other[: min(len(pointer), len(other))]
            for other_file, other in others
        ):
            raise ValueError("SDK source pin overlaps another source or output pin")
    return result


def pin_value(root: Path, pin: Mapping[str, object]) -> str:
    file = ad.text(pin["file"], "SDK output file")
    body = tc.regular_input(root, file).decode()
    value: object
    if pin.get("format") == "regex":
        matches = list(
            re.finditer(
                ad.text(pin["pattern"], "SDK output pattern"), body, re.MULTILINE
            )
        )
        if len(matches) != 1 or "value" not in matches[0].groupdict():
            raise ValueError("Toolchain pin must match exactly one named value")
        value = matches[0]["value"]
    else:
        value = manifests.lookup(
            manifests.document(tc.contained(root, file))[0],
            manifests.pin_pointer(pin["pointer"]),
        )
    if not isinstance(value, str):
        raise ValueError("Toolchain output pins must be strings")  # noqa: TRY004 - decoded external data
    return value


def snapshot(root: Path, spec: Mapping[str, object]) -> Snapshot:
    registry.fetch.cache_clear()
    values = tools(spec)
    observed = []
    sources = []
    for tool in values:
        value = probe(root, spec, tool)
        chosen, _ = observe(tool, value)
        source = source_pin.read(root, tool)
        if source is not None and source != source_pin.record(tool, chosen):
            raise ValueError(
                "Nix SDK source pin differs from its observed registry artifact"
            )
        sources.append(source)
        observed.append(evidence(tool, chosen, value))
    return {
        "adapter": "toolchain",
        "pins": [[pin_value(root, pin) for pin in tool["pins"]] for tool in values],
        "tools": observed,
        "sources": sources,
        "source_files": source_pin.files(root, values),
    }


def probe(root: Path, spec: Mapping[str, object], tool: Mapping[str, object]) -> str:
    env = tc.environment(root)
    env["TOOLCHAIN_FRESH"] = "1"
    with tempfile.TemporaryDirectory(prefix="chainman SDK probe ") as temporary:
        result = chainman.execute(
            root,
            ad.text(spec.get("profile", "core"), "SDK profile"),
            ad.strings(tool["command"], "SDK command"),
            cwd=Path(temporary),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    matches = list(
        re.finditer(
            ad.text(tool["version_pattern"], "SDK version pattern"),
            result.stdout + "\n" + result.stderr,
            re.MULTILINE,
        )
    )
    if (
        len(matches) != 1
        or registry.version(
            ad.text(tool["provider"], "SDK provider"), matches[0]["version"]
        )
        is None
    ):
        raise ValueError("Nix tool probe lacks one stable version identity")
    return matches[0]["version"].removeprefix("v")


def observe(
    tool: Mapping[str, object], value: str
) -> tuple[registry.Release, list[registry.Release]]:
    """Bind one observed SDK version without granting it release-age eligibility."""
    provider, package = (
        ad.text(tool["provider"], "SDK provider"),
        ad.text(tool["name"], "SDK package"),
    )
    releases = registry.releases(provider, package)
    rank = registry.version(provider, value)
    selected = [r for r in releases if registry.version(provider, r.version) == rank]
    if not selected:
        raise ValueError(
            f"Nix tool {provider}:{package}@{value} lacks dated registry evidence"
        )
    chosen = max(selected, key=lambda r: r.published)
    if provider == "github":
        import source_updates

        commit = registry.github_commit(package, chosen.version)
        source_updates.revision(commit)
        published = max(chosen.published, source_updates.commit_time(package, commit))
        bound = registry.Release(chosen.version, published, commit, chosen.python)
    else:
        if not chosen.artifacts:
            raise ValueError(
                f"Nix tool {provider}:{package}@{value} lacks immutable release artifacts"
            )
        for item in chosen.artifacts:
            registry.artifact_url(item.url)
            if provider == "npm":
                if not isinstance(item.digest, str) or not re.fullmatch(
                    r"sha1:[a-f0-9]{40}|sha256:[a-f0-9]{64}|sha384:[a-f0-9]{96}|sha512:[a-f0-9]{128}",
                    item.digest,
                ):
                    raise ValueError(
                        "Nix tool has malformed canonical npm artifact evidence"
                    )
            else:
                registry.digest(item.digest)
        bound = registry.Release(
            chosen.version,
            max(chosen.published, *(a.published for a in chosen.artifacts)),
            chosen.identity,
            chosen.python,
            chosen.artifacts,
        )
    registry.timestamp(bound.published.isoformat())
    return bound, [bound if r.version == chosen.version else r for r in releases]


def evidence(
    tool: Mapping[str, object], release: registry.Release, actual_version: str
) -> Evidence:
    return {
        "provider": ad.text(tool["provider"], "SDK provider"),
        "name": ad.text(tool["name"], "SDK package"),
        "version": actual_version,
        "release": release.version,
        "identity": release.identity,
        "published": release.published.isoformat(),
        "artifacts": sorted(
            [[a.url, a.digest, a.published.isoformat()] for a in release.artifacts]
        ),
    }


def eligible_tool(
    tool: Mapping[str, object],
    chosen: registry.Release,
    releases: list[registry.Release],
    policy: Mapping[str, object],
    now: datetime,
    *,
    retained: bool,
) -> None:
    provider, package = (
        ad.text(tool["provider"], "SDK provider"),
        ad.text(tool["name"], "SDK package"),
    )
    # Retention waives only age: constraints, known safe floors and malformed or
    # expired exceptions remain operative even when no newer release is selected.
    mature = registry.maturity(provider, releases, policy, package, now)
    exceptions = registry.active_exceptions(provider, releases, policy, package, now)
    rank = registry.version(provider, chosen.version)
    safe = registry.minimum_safe(provider, policy, package)
    if (
        chosen.python == "unsupported"
        or not registry.compatible(
            provider, chosen.version, registry.constraint(provider, policy, package)
        )
        or (safe is not None and rank < safe)
        or chosen.published > now
    ):
        raise ValueError(
            f"Nix tool {provider}:{package}@{chosen.version} violates its active compatibility or security policy"
        )
    if not retained and chosen not in mature + exceptions:
        raise ValueError(
            f"Nix tool {provider}:{package}@{chosen.version} lacks eligible dated registry evidence (published {chosen.published.isoformat()})"
        )


def render(pin: Mapping[str, object], value: str) -> str:
    parts = value.split(".")
    return ad.text(pin.get("value", "{version}"), "SDK output template").format(
        version=value,
        major=parts[0],
        minor=parts[1] if len(parts) > 1 else "0",
        patch=parts[2] if len(parts) > 2 else "0",
    )


def plan(
    root: Path,
    spec: Mapping[str, object],
    before: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> list[SelectedTool]:
    values = tools(spec)
    baseline = read_baseline(before, values)
    registry.fetch.cache_clear()
    result: list[SelectedTool] = []
    for index, tool in enumerate(values):
        value = probe(root, spec, tool)
        chosen, releases = observe(tool, value)
        source = source_pin.read(root, tool)
        if source is not None and source != source_pin.record(tool, chosen):
            raise ValueError(
                "Nix SDK source pin differs from its observed registry artifact"
            )
        if source is not None:
            previous_source = baseline["sources"][index]
            if previous_source is None:
                raise ValueError("SDK source observation is missing from its baseline")
            releases = source_pin.inventory(
                releases,
                previous_source["version"],
                ad.text(spec.get("mode", "aggressive"), "SDK selection mode"),
            )
        observed = evidence(tool, chosen, value)
        pins = []
        for pin, old in zip(tool["pins"], baseline["pins"][index], strict=True):
            floor = re.search(r"\d+(?:\.\d+)*", old)
            if floor and Version(floor[0]) > Version(value):
                raise ValueError("Refreshed Nix tool would downgrade an existing pin")
            pins.append(render(pin, value))
        retained = (
            observed == baseline["tools"][index]
            and pins == baseline["pins"][index]
            and source == baseline["sources"][index]
        )
        eligible_tool(tool, chosen, releases, policy, now, retained=retained)
        selected: SelectedTool = {
            "provider": observed["provider"],
            "name": observed["name"],
            "version": observed["version"],
            "release": observed["release"],
            "identity": observed["identity"],
            "published": observed["published"],
            "artifacts": observed["artifacts"],
            "pins": pins,
        }
        if source is not None:
            selected["source"] = source
        result.append(selected)
    return result


def resolve(
    root: Path,
    spec: Mapping[str, object],
    policy: ad.Table,
    now: datetime,
    *,
    before: Mapping[str, object] | None = None,
) -> Resolution:
    before = snapshot(root, spec) if before is None else before
    values = tools(spec)
    baseline = read_baseline(before, values)
    changed = []
    if source_pin.files(root, values) != before.get("source_files", {}):
        raise ValueError("SDK source files changed concurrently before resolution")
    registry.fetch.cache_clear()
    # Write all selected source records before entering the newly evaluated Nix
    # profile. Its actual binary version must then agree with these identities.
    for index, tool in enumerate(values):
        if source_pin.declaration(tool) is None:
            continue
        expected = baseline["sources"][index]
        if expected is None:
            raise ValueError("SDK source observation is missing from its baseline")
        selected_source = source_pin.select(
            tool,
            baseline["tools"][index],
            expected,
            baseline["pins"][index],
            policy,
            now,
            mode=ad.text(spec.get("mode", "aggressive"), "SDK selection mode"),
        )
        if source_pin.write(root, tool, expected, selected_source):
            changed.append(tool["source_pin"]["file"])
    selected = plan(root, spec, before, policy, now)
    for tool, result in zip(values, selected, strict=True):
        for pin, value in zip(tool["pins"], result["pins"], strict=True):
            if pin_value(root, pin) == value:
                continue
            rewrite = {
                **pin,
                "provider": "go",
                "identity": pin.get("format") == "regex",
                "prefix": "v" if value.startswith("v") else "",
            }
            if manifests.replace(
                rewrite,
                registry.Release(value, registry.timestamp(result["published"]), value),
                root,
            ):
                changed.append(ad.text(pin["file"], "SDK output file"))
    return {"changed": sorted(set(changed)), "tools": selected}


def audit(
    root: Path,
    spec: Mapping[str, object],
    before: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> None:
    selected = plan(root, spec, before, policy, now)
    if "resolution" not in before and any(
        item.get("source") != old
        for item, old in zip(
            selected,
            ad.array(
                before.get("sources", [None] * len(selected)), "SDK baseline sources"
            ),
            strict=True,
        )
    ):
        raise ValueError("SDK source changed without a recorded selection")
    if (
        "resolution" in before
        and selected != ad.table(before["resolution"], "SDK resolution")["tools"]
    ):
        raise ValueError("Selected Nix tool changed after resolution")
    expected = [item["pins"] for item in selected]
    actual_pins = [
        [pin_value(root, pin) for pin in tool["pins"]] for tool in tools(spec)
    ]
    if actual_pins != expected:
        raise ValueError("Toolchain pins differ from the dated selected tools")
