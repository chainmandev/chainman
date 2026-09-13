"""Read-only dependency coverage and policy reports from the actual adapters."""

import fnmatch
import hashlib
import json
from pathlib import Path
import tomllib
from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict

import adapter_data as ad
import chainman_updates
import dependency_api
import source_toolchain_pin
import toolchain as tc
import updates


MANIFESTS = {
    "package.json": "javascript",
    "Cargo.toml": "rust",
    "pyproject.toml": "python",
    "go.mod": "go",
    "pubspec.yaml": "flutter",
    "Package.swift": "swift",
    "build.gradle.kts": "gradle",
    "build.gradle": "gradle",
    "flake.lock": "nix",
    "rust-toolchain.toml": "toolchain",
    "rust-toolchain": "toolchain",
    ".nvmrc": "toolchain",
    ".node-version": "toolchain",
    "gradle-wrapper.properties": "toolchain",
}


class CoverageRow(TypedDict):
    path: str
    kind: str
    status: Literal["managed", "excluded", "unmanaged"]
    adapters: NotRequired[list[str]]
    reason: NotRequired[str]
    rationale: NotRequired[str]


class Coverage(TypedDict):
    schema: Literal[1]
    complete: bool
    inputs: list[CoverageRow]


def tracked(root: Path) -> list[str]:
    return list(filter(None, updates.git(root, "ls-files", "-z").split("\0")))


def inputs(root: Path, spec: ad.Table) -> set[str]:
    kind = ad.text(spec["adapter"], "Adapter kind")
    if kind == "javascript":
        import javascript_updates

        base = Path(ad.text(spec.get("directory", "."), "Adapter directory"))
        directory = tc.contained(root, str(base))
        manifest = ad.table(
            json.loads(tc.regular_input(directory, "package.json")),
            "JavaScript manifest",
        )
        workspace = ad.text(
            spec.get("workspace", "pnpm-workspace.yaml"), "Workspace file"
        )
        settings: ad.Table = {}
        if spec.get("manager", "pnpm") == "pnpm" and (directory / workspace).exists():
            from ruamel.yaml import YAML

            settings = ad.table(
                YAML(typ="safe").load(tc.regular_input(directory, workspace)) or {},
                "JavaScript workspace",
            )
        return {
            str(base / name)
            for name in javascript_updates.manifest_paths(
                directory, spec, manifest, settings
            )
        }
    if kind in {"rust", "python", "flutter", "swift", "gradle"}:
        import ecosystem_updates

        return {
            name
            for member in ecosystem_updates.specifications(root, spec).values()
            for name in ad.strings(member["inputs"], "Adapter inputs")
        }
    if kind == "go":
        return {
            str(Path(directory) / "go.mod")
            for directory in ad.strings(
                spec.get("directories", [spec.get("directory", ".")]), "Go directories"
            )
        }
    if kind == "nix":
        return {
            str(
                Path(
                    ad.text(
                        ad.table(entry, "Nix input").get("directory", "."),
                        "Nix directory",
                    )
                )
                / "flake.lock"
            )
            for entry in ad.array(spec.get("inputs", []), "Nix inputs")
        }
    if kind == "actions":
        return {
            name
            for name in tracked(root)
            if any(
                fnmatch.fnmatchcase(name, pattern)
                for pattern in ad.strings(spec.get("files", []), "Workflow patterns")
            )
        }
    if kind == "toolchain":
        paths: set[str] = set()
        for raw in ad.array(spec.get("tools", []), "SDK tools"):
            tool = ad.table(raw, "SDK tool")
            paths.update(
                ad.text(ad.table(pin, "SDK pin")["file"], "SDK pin file")
                for pin in ad.array(tool.get("pins", []), "SDK pins")
            )
            source = source_toolchain_pin.declaration(tool)
            if source is not None:
                paths.add(source["file"])
        return paths
    if kind in {"oci", "artifact"}:
        return (
            {ad.text(spec["file"], "Adapter file")}
            if "file" in spec
            else {
                ad.text(ad.table(entry, "Adapter entry")["file"], "Adapter file")
                for entry in ad.array(spec.get("entries", []), "Adapter entries")
            }
        )
    return set()


def coverage(root: Path) -> Coverage:
    settings = dependency_api.inspection_policy(root)
    owners: dict[str, list[str]] = {}
    for name in ad.table(settings.get("adapters", {}), "Dependency adapters"):
        for path in inputs(root, dependency_api.configured(root, name, settings)):
            owners.setdefault(path, []).append(name)
    spec = ad.table(tc.config(root).get("dependencies", {}), "Dependency coverage")
    if set(spec) - {"exclusions", "pins"}:
        raise ValueError("Dependency coverage supports exclusions and pins")
    exclusions: list[dict[str, str]] = []
    for value in ad.array(spec.get("exclusions", []), "Dependency exclusions"):
        entry = ad.string_map(value, "Dependency exclusion")
        if set(entry) != {"pattern", "reason"} or not all(
            value.strip() for value in entry.values()
        ):
            raise ValueError("Dependency exclusions require pattern and reason")
        tc.contained(root, entry["pattern"])
        exclusions.append(entry)
    records: dict[str, CoverageRow] = {}
    for path in tracked(root):
        kind = MANIFESTS.get(Path(path).name)
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            kind = "actions"
        if kind:
            records[path] = {"path": path, "kind": kind, "status": "unmanaged"}
    for path in owners:
        records.setdefault(
            path, {"path": path, "kind": "declared-input", "status": "unmanaged"}
        )
    for value in ad.array(spec.get("pins", []), "Additional tool pins"):
        entry = ad.string_map(value, "Additional tool pin")
        if set(entry) != {"path", "owner", "reason"} or not entry["reason"].strip():
            raise ValueError("Additional tool pins require path, owner and reason")
        tc.regular_input(root, entry["path"])
        records[entry["path"]] = dict(
            path=entry["path"],
            kind="toolchain",
            rationale=entry["reason"],
            status="unmanaged",
        )
        if entry["owner"] == "runtime" and entry[
            "path"
        ] in chainman_updates.managed_paths(root):
            owners.setdefault(entry["path"], []).append("runtime")
        elif entry["owner"] not in owners.get(entry["path"], []):
            raise ValueError("Declared pin owner does not manage that file")
    for path, record in records.items():
        matches = [
            entry for entry in exclusions if fnmatch.fnmatchcase(path, entry["pattern"])
        ]
        if path in owners:
            record["status"] = "managed"
            record["adapters"] = sorted(owners[path])
        elif matches:
            record["status"] = "excluded"
            record["reason"] = matches[0]["reason"]
    rows = [records[key] for key in sorted(records)]
    return dict(
        schema=1,
        complete=all(row["status"] != "unmanaged" for row in rows),
        inputs=rows,
    )


def report(root: Path, arguments: list[str]) -> ad.Table:
    settings = dependency_api.inspection_policy(root)
    selected, modes = dependency_api.selection(settings, arguments)
    return dict(
        schema=1,
        minimum_age_days=settings.get("minimum_age_days", 30),
        selected=sorted(selected),
        policies=modes,
        verification=settings.get(
            "verify_tasks",
            [settings["verify_task"]] if "verify_task" in settings else [],
        ),
        adapters={
            name: {
                key: value
                for key, value in dependency_api.configured(
                    root, name, settings
                ).items()
                if key
                in {
                    "adapter",
                    "directory",
                    "directories",
                    "explicit_only",
                    "policy",
                    "held_dependencies",
                    "retained_sources",
                }
            }
            for name in ad.table(settings.get("adapters", {}), "Dependency adapters")
        },
        constraints=settings.get("constraints", {}),
        exceptions=settings.get("exceptions", []),
        audit_exceptions=ad.table(tc.config(root).get("audits", {}), "Audits").get(
            "exceptions", {}
        ),
        target_groups=settings.get("target_groups", {}),
        declarations=declarations(root),
        coverage=coverage(root),
    )


def declarations(root: Path) -> dict[str, ad.Table]:
    """Report committed package policy and wrapper provenance without executing tools."""
    result: dict[str, ad.Table] = {}
    for name in sorted(filter(None, tracked(root))):
        path = Path(name)
        if path.name == "package.json":
            data = ad.table(
                json.loads(tc.regular_input(root, name)), "JavaScript manifest"
            )
            if "packageManager" in data:
                result[name] = {"packageManager": data["packageManager"]}
        elif path.name == "pnpm-workspace.yaml":
            from ruamel.yaml import YAML

            data = ad.table(
                YAML(typ="safe").load(tc.regular_input(root, name)) or {},
                "JavaScript workspace",
            )
            result[name] = {
                key: data[key]
                for key in (
                    "catalog",
                    "catalogs",
                    "overrides",
                    "minimumReleaseAge",
                    "minimumReleaseAgeExclude",
                )
                if key in data
            }
        elif path.name == "deny.toml":
            data = tomllib.loads(tc.regular_input(root, name).decode())
            result[name] = {
                "advisory_ignores": ad.table(
                    data.get("advisories", {}), "Advisories"
                ).get("ignore", []),
                "duplicate_skips": ad.table(data.get("bans", {}), "Bans").get(
                    "skip", []
                ),
                "duplicate_tree_skips": ad.table(data.get("bans", {}), "Bans").get(
                    "skip-tree", []
                ),
            }
        elif path.name == "gradle-wrapper.properties":
            properties = dict(
                line.split("=", 1)
                for line in tc.regular_input(root, name).decode().splitlines()
                if "=" in line and not line.lstrip().startswith(("#", "!"))
            )
            jar = str(path.with_name("gradle-wrapper.jar"))
            result[name] = {
                "distribution_url": properties.get("distributionUrl", ""),
                "distribution_sha256": properties.get("distributionSha256Sum", ""),
                "wrapper_sha256": hashlib.sha256(
                    tc.regular_input(root, jar)
                ).hexdigest()
                if tc.contained(root, jar).exists()
                else None,
            }
    return result


def run(root: Path, action: str, args: list[str]) -> int:
    import recipes

    result: Mapping[str, object]
    if action == "deps-coverage":
        if args:
            raise ValueError("deps-coverage takes no arguments")
        result = coverage(root)
    else:
        result = report(root, recipes.selection_options(args))
    print(json.dumps(result, indent=2))
    return 0 if result.get("complete", True) else 1
