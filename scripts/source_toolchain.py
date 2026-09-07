"""Synchronize declared pins with dated tool versions supplied by a Nix profile."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import chainman
import manifests
import registry
import toolchain as tc
from packaging.version import Version


def tools(spec: dict) -> list[dict]:
    values = spec.get("tools")
    if not isinstance(values, list) or not values:
        raise ValueError("Toolchain synchronization requires declared tools")
    seen = set()
    for tool in values:
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
        pattern = re.compile(tool["version_pattern"], re.MULTILINE)
        if "version" not in pattern.groupindex:
            raise ValueError("Toolchain probe pattern requires a named version group")
        for pin in tool["pins"]:
            if set(pin) - {"file", "pointer", "format", "pattern", "value"}:
                raise ValueError(
                    "Toolchain pins use file/pointer or regex plus a value template"
                )
            key = (pin["file"], json.dumps(pin.get("pointer", pin.get("pattern"))))
            if key in seen:
                raise ValueError("A toolchain pin has multiple owners")
            seen.add(key)
    return values


def pin_value(root: Path, pin: dict) -> str:
    body = tc.regular_input(root, pin["file"]).decode()
    if pin.get("format") == "regex":
        matches = list(re.finditer(pin["pattern"], body, re.MULTILINE))
        if len(matches) != 1 or "value" not in matches[0].groupdict():
            raise ValueError("Toolchain pin must match exactly one named value")
        value = matches[0]["value"]
    else:
        value = manifests.lookup(
            manifests.document(tc.contained(root, pin["file"]))[0], pin["pointer"]
        )
    if not isinstance(value, str):
        raise ValueError("Toolchain output pins must be strings")  # noqa: TRY004 - decoded external data
    return value


def snapshot(root: Path, spec: dict) -> dict:
    return {
        "adapter": "toolchain",
        "pins": [
            [pin_value(root, pin) for pin in tool["pins"]] for tool in tools(spec)
        ],
    }


def probe(root: Path, spec: dict, tool: dict) -> str:
    env = tc.environment(root)
    env["TOOLCHAIN_FRESH"] = "1"
    with tempfile.TemporaryDirectory(prefix="chainman SDK probe ") as temporary:
        result = chainman.execute(
            root,
            spec.get("profile", "core"),
            tool["command"],
            cwd=Path(temporary),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    matches = list(
        re.finditer(
            tool["version_pattern"], result.stdout + "\n" + result.stderr, re.MULTILINE
        )
    )
    if (
        len(matches) != 1
        or registry.version(tool["provider"], matches[0]["version"]) is None
    ):
        raise ValueError("Nix tool probe lacks one stable version identity")
    return matches[0]["version"].removeprefix("v")


def eligible_tool(
    tool: dict, value: str, policy: dict, now: datetime
) -> registry.Release:
    provider, package = tool["provider"], tool["name"]
    releases = registry.releases(provider, package)
    rank = registry.version(provider, value)
    selected = [
        r
        for r in registry.eligible(provider, releases, policy, package, now)
        if registry.version(provider, r.version) == rank
    ]
    if not selected:
        raise ValueError("Selected Nix tool lacks eligible dated registry evidence")
    chosen = max(selected, key=lambda r: r.published)
    if provider == "github":
        import source_updates

        commit = registry.github_commit(package, chosen.version)
        published = max(chosen.published, source_updates.commit_time(package, commit))
        bound = registry.Release(chosen.version, published, commit)
        alternatives = [bound if r.version == chosen.version else r for r in releases]
        if not any(
            r.version == chosen.version
            for r in registry.eligible(provider, alternatives, policy, package, now)
        ):
            raise ValueError("Selected Nix tool tag points to immature contents")
        return bound
    if not chosen.artifacts:
        raise ValueError("Selected Nix tool lacks immutable release artifacts")
    return chosen


def render(pin: dict, value: str) -> str:
    parts = value.split(".")
    return pin.get("value", "{version}").format(
        version=value,
        major=parts[0],
        minor=parts[1] if len(parts) > 1 else "0",
        patch=parts[2] if len(parts) > 2 else "0",
    )


def plan(
    root: Path, spec: dict, before: dict, policy: dict, now: datetime
) -> list[dict]:
    values = tools(spec)
    if len(before["pins"]) != len(values):
        raise ValueError("Toolchain declaration changed during resolution")
    result = []
    for index, tool in enumerate(values):
        value = probe(root, spec, tool)
        chosen = eligible_tool(tool, value, policy, now)
        pins = []
        if len(before["pins"][index]) != len(tool["pins"]):
            raise ValueError("Toolchain output inventory changed during resolution")
        for pin, old in zip(tool["pins"], before["pins"][index], strict=True):
            floor = re.search(r"\d+(?:\.\d+)*", old)
            if floor and Version(floor[0]) > Version(value):
                raise ValueError("Refreshed Nix tool would downgrade an existing pin")
            pins.append(render(pin, value))
        result.append(
            {
                "version": value,
                "identity": chosen.identity,
                "published": chosen.published.isoformat(),
                "pins": pins,
            }
        )
    return result


def resolve(root: Path, spec: dict, policy: dict, now: datetime) -> dict:
    before = snapshot(root, spec)
    selected = plan(root, spec, before, policy, now)
    changed = []
    for tool, result in zip(tools(spec), selected, strict=True):
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
                changed.append(pin["file"])
    return {"changed": sorted(set(changed)), "tools": selected}


def audit(root: Path, spec: dict, before: dict, policy: dict, now: datetime) -> None:
    selected = plan(root, spec, before, policy, now)
    if "resolution" in before and selected != before["resolution"]["tools"]:
        raise ValueError("Selected Nix tool changed after resolution")
    expected = [item["pins"] for item in selected]
    if snapshot(root, spec)["pins"] != expected:
        raise ValueError("Toolchain pins differ from the dated selected tools")
