"""Explicit project-owned Cargo source bindings for prepublication updates.

These inputs never claim registry publication or waive registry artifact policy.
The native resolver must materialize each used binding at its declared manifest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tomllib

import adapter_data as ad
import registry
import toolchain as tc


@dataclass(frozen=True)
class Binding:
    name: str
    manifest: str
    version: str
    version_manifest: str


def document(root: Path, relative: str) -> ad.Table:
    return ad.table(
        tomllib.loads(tc.regular_input(root, relative).decode()), "Cargo manifest"
    )


def read(root: Path, spec: Mapping[str, object]) -> dict[str, Binding]:
    raw = ad.string_map(spec.get("cargo_sources", {}), "Cargo candidate sources")
    if not raw:
        return {}
    if spec.get("adapter") != "rust" or spec.get("resolve", [["cargo", "update"]]) != [
        ["cargo", "update"]
    ]:
        raise ValueError("cargo_sources requires ordinary Rust cargo update")
    result = {}
    for name, relative in raw.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name):
            raise ValueError("Cargo candidate source requires an ordinary package name")
        manifest = tc.contained(root, relative)
        if manifest.name != "Cargo.toml":
            raise ValueError("Cargo candidate source must name a Cargo.toml manifest")
        package = ad.table(
            document(root, relative).get("package"), "Cargo source package"
        )
        if package.get("name") != name:
            raise ValueError(
                "Cargo candidate source package name differs from its binding"
            )
        version = package.get("version")
        version_manifest = relative
        if (
            isinstance(version, dict)
            and set(version) == {"workspace"}
            and version["workspace"] is True
        ):
            workspace = package.get("workspace")
            if workspace is not None:
                parent = tc.local_source(
                    root, manifest.parent, ad.text(workspace, "Cargo workspace path")
                )
            else:
                parent = manifest.parent
                while "workspace" not in document(
                    root, str((parent / "Cargo.toml").relative_to(root))
                ):
                    if parent == root:
                        raise ValueError(
                            "Cargo candidate source lacks its version workspace"
                        )
                    parent = parent.parent
                    # Cargo permits intermediate directories without a manifest.
                    while not (parent / "Cargo.toml").exists() and parent != root:
                        parent = parent.parent
            version_manifest = str((parent / "Cargo.toml").relative_to(root))
            version = ad.table(
                ad.table(
                    document(root, version_manifest).get("workspace"), "Cargo workspace"
                ).get("package"),
                "Cargo workspace package",
            ).get("version")
        value = ad.text(version, "Cargo candidate source version")
        if (
            not re.fullmatch(r"\d+\.\d+\.\d+", value)
            or registry.version("crates", value) is None
        ):
            raise ValueError("Cargo candidate source requires a stable release version")
        result[name] = Binding(name, relative, value, version_manifest)
    return result


def arguments(root: Path, bindings: Mapping[str, Binding]) -> list[str]:
    return [
        argument
        for name, binding in sorted(bindings.items())
        for argument in (
            "--config",
            f"patch.crates-io.{name}.path="
            + json.dumps(str(tc.contained(root, binding.manifest).parent)),
        )
    ]


def materialized(
    root: Path, bindings: Mapping[str, Binding], metadata: object, required: set[str]
) -> list[ad.Table]:
    """Bind native package identities to project manifests, rejecting registry fallback."""
    packages = ad.array(
        ad.table(metadata, "Cargo metadata").get("packages"), "Cargo packages"
    )
    result: list[ad.Table] = []
    for name, binding in sorted(bindings.items()):
        matches = [
            ad.table(raw, "Cargo package")
            for raw in packages
            if ad.table(raw, "Cargo package").get("name") == name
        ]
        if not matches and name not in required:
            continue
        if len(matches) != 1:
            raise ValueError(
                "Cargo candidate source has a missing or ambiguous native identity"
            )
        package = matches[0]
        if (
            "source" not in package
            or package.get("source") is not None
            or package.get("version") != binding.version
            or package.get("manifest_path") != str(tc.contained(root, binding.manifest))
        ):
            raise ValueError(
                "Cargo candidate source differs from its declared native identity"
            )
        result.append(
            {"name": name, "version": binding.version, "manifest": binding.manifest}
        )
    return result
