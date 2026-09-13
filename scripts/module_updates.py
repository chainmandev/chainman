"""Translate the optional example modules into the shared dependency adapters."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from collections.abc import Mapping

import dependency_api
import sdk_versions
import source_updates
import toolchain as tc
from adapter_data import Table, array, table, text


def nix_spec(policy: Mapping[str, object]) -> Table | None:
    declared = table(policy.get("nix", {}), "Nix update policy")
    if not declared.get("enabled", True):
        return None
    if "inputs" in declared:
        if any(key in declared for key in ("input", "repository", "branch")):
            raise ValueError("Nix input lists cannot also declare a single input")
        inputs = [
            {"directory": declared.get("directory", "nix"), **table(item, "Nix input")}
            for item in array(declared["inputs"], "Nix inputs")
        ]
        if not inputs:
            raise ValueError("Nix updates require at least one declared input")
    else:
        inputs = [
            {
                "directory": declared.get("directory", "nix"),
                "input": declared.get("input", "nixpkgs"),
                "repository": declared.get("repository", "NixOS/nixpkgs"),
                "branch": declared.get("branch", "nixos-unstable"),
            }
        ]
    return {
        "adapter": "nix",
        "profile": "core",
        "inputs": inputs,
    }


def adapters(root: Path, selected: list[str], policy: Table) -> dict[str, Table]:
    configured: dict[str, Table] = {}
    explicit = [
        table(pin, "Module pin") for pin in array(policy.get("pins", []), "Module pins")
    ]
    actions = set()
    for pin in explicit:
        if pin.get("module"):
            continue
        if pin.get("provider") != "github" or pin.get("representation") != "action":
            raise ValueError(
                "Non-module pins require an explicitly configured shared adapter"
            )
        actions.add(text(pin["file"], "Action pin file"))
    if actions:
        configured["actions"] = {
            "adapter": "actions",
            "profile": "core",
            "files": sorted(actions),
        }
    kinds = {
        "npm": "javascript",
        "crates": "rust",
        "pypi": "python",
        "pub": "flutter",
        "swift": "swift",
        "maven": "gradle",
        "go": "go",
    }
    for name in selected:
        module = table(tc.module(name, root), "Example module")
        ecosystem = module.get("ecosystem")
        if ecosystem is None:
            continue
        if not isinstance(ecosystem, str) or ecosystem not in kinds:
            raise ValueError(
                "Example module needs a supported shared ecosystem adapter"
            )
        kind = kinds[ecosystem]
        spec = {**module, "adapter": kind}
        pins = [pin for pin in explicit if pin.get("module") == name]
        if pins:
            spec["pins"] = pins
        if kind == "javascript":
            if pins:
                raise ValueError(
                    "JavaScript module policy belongs in scoped adapter constraints"
                )
            spec["manager"] = "pnpm"
        elif kind == "go":
            if pins:
                raise ValueError(
                    "Go module updates discover exact declarations; remove duplicate explicit pins"
                )
            spec["directories"] = [module["directory"]]
        else:
            commands = table(module.get("commands", {}), "Module commands")
            if commands.get("resolve"):
                spec["resolve"] = commands["resolve"]
            if kind == "gradle" and not pins:
                raise ValueError(
                    "The Compose module must declare coordinated version catalog pins"
                )
        configured[name] = spec
    return configured


def image_snapshot(root: Path, policy: Table) -> dict[str, str] | None:
    spec = table(policy.get("docker", {}), "Docker update policy")
    if not spec.get("enabled", True):
        return None
    reference = tc.regular_input(root, "nix/container-image.txt").decode().strip()
    repository = text(spec.get("repository", "nixos/nix"), "Docker repository")
    match = re.fullmatch(
        r"docker\.io/([^:@]+):([^@]+)@(sha256:[a-f0-9]{64})", reference
    )
    if not match or match[1] != repository:
        raise ValueError(
            "Runtime image must match its declared repository and immutable identity"
        )
    bootstrap = tc.regular_input(root, "bootstrap/chainman.sh").decode()
    if re.findall(r"(?m)^image=(\S+)$", bootstrap) != [reference]:
        raise ValueError("Bootstrap image and declared runtime image disagree")
    return {
        "repository": match[1],
        "tag": match[2],
        "digest": match[3],
        "versionSource": "dockerHub",
    }


def resolve(root: Path, selected: list[str], policy: Table, now: datetime) -> None:
    configured = adapters(root, selected, policy)
    before = {
        name: table(
            dependency_api.implementation(spec).snapshot(root, spec), "Adapter snapshot"
        )
        for name, spec in configured.items()
    }
    image_before = image_snapshot(root, policy)
    with dependency_api.transaction_environment(root, now):
        # Selected Nix inputs are already refreshed in the enclosing phase. These
        # bindings preserve language contracts without choosing another toolchain.
        sdk_versions.synchronize(root, selected)
        for name, spec in configured.items():
            result = dependency_api.implementation(spec).resolve(
                root, spec, policy, now
            )
            before[name]["resolution"] = result
        if image_before is not None:
            chosen = source_updates.select_oci(image_before, {}, policy, now)
            image = (
                f"docker.io/{chosen['repository']}:{chosen['tag']}@{chosen['digest']}"
            )
            path = root / "nix/container-image.txt"
            path.write_text(image + "\n")
            bootstrap = root / "bootstrap/chainman.sh"
            body, count = re.subn(
                r"(?m)^image=\S+$",
                lambda _: "image=" + image,
                bootstrap.read_bytes().decode(),
            )
            if count != 1:
                raise ValueError(
                    "Bootstrap image declaration changed during resolution"
                )
            bootstrap.write_bytes(body.encode())
        # All module reconciliation is complete before any final identity audit.
        for name, spec in configured.items():
            dependency_api.implementation(spec).audit(
                root, spec, before[name], policy, now
            )
        if image_before is not None:
            expected = source_updates.select_oci(image_before, {}, policy, now)
            actual = image_snapshot(root, policy)
            if actual is None or any(actual[key] != expected[key] for key in actual):
                raise ValueError("Runtime image drifted from its audited selection")
        sdk_versions.synchronize(root, selected, check=True)
