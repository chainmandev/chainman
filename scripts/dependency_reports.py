"""Read-only dependency coverage and policy reports from the actual adapters."""

import fnmatch
import hashlib
import json
from pathlib import Path
import tomllib

import dependency_api
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


def tracked(root):
    return updates.git(root, "ls-files", "-z").split("\0")


def inputs(root, spec):
    kind = spec["adapter"]
    if kind == "javascript":
        import javascript_updates

        base = Path(spec.get("directory", "."))
        directory = tc.contained(root, str(base))
        manifest = json.loads(tc.regular_input(directory, "package.json"))
        workspace = spec.get("workspace", "pnpm-workspace.yaml")
        settings = {}
        if spec.get("manager", "pnpm") == "pnpm" and (directory / workspace).exists():
            from ruamel.yaml import YAML

            settings = YAML(typ="safe").load(tc.regular_input(directory, workspace))
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
            for name in member["inputs"]
        }
    if kind == "go":
        return {
            str(Path(directory) / "go.mod")
            for directory in spec.get("directories", [spec.get("directory", ".")])
        }
    if kind == "nix":
        return {
            str(Path(entry.get("directory", ".")) / "flake.lock")
            for entry in spec.get("inputs", [])
        }
    if kind == "actions":
        return {
            name
            for name in tracked(root)
            if any(
                fnmatch.fnmatchcase(name, pattern) for pattern in spec.get("files", [])
            )
        }
    if kind == "toolchain":
        return {
            pin["file"]
            for tool in spec.get("tools", [])
            for pin in tool.get("pins", [])
        }
    if kind in {"oci", "artifact"}:
        return (
            {spec["file"]}
            if "file" in spec
            else {entry["file"] for entry in spec.get("entries", [])}
        )
    return set()


def coverage(root):
    settings = dependency_api.inspection_policy(root)
    owners = {}
    for name in settings.get("adapters", {}):
        for path in inputs(root, dependency_api.configured(root, name, settings)):
            owners.setdefault(path, []).append(name)
    spec = tc.config(root).get("dependencies", {})
    if not isinstance(spec, dict) or set(spec) - {"exclusions", "pins"}:
        raise ValueError("Dependency coverage supports exclusions and pins")
    exclusions = spec.get("exclusions", [])
    for entry in exclusions:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"pattern", "reason"}
            or not all(
                isinstance(value, str) and value.strip() for value in entry.values()
            )
        ):
            raise ValueError("Dependency exclusions require pattern and reason")
        tc.contained(root, entry["pattern"])
    records = {}
    for path in tracked(root):
        kind = MANIFESTS.get(Path(path).name)
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            kind = "actions"
        if kind:
            records[path] = {"path": path, "kind": kind}
    for path in owners:
        records.setdefault(path, {"path": path, "kind": "declared-input"})
    for entry in spec.get("pins", []):
        if set(entry) != {"path", "owner", "reason"} or not entry["reason"].strip():
            raise ValueError("Additional tool pins require path, owner and reason")
        tc.regular_input(root, entry["path"])
        records[entry["path"]] = dict(
            path=entry["path"], kind="toolchain", rationale=entry["reason"]
        )
        if entry["owner"] == "runtime" and entry["path"] in __import__(
            "chainman_updates"
        ).managed_paths(root):
            owners.setdefault(entry["path"], []).append("runtime")
        elif entry["owner"] not in owners.get(entry["path"], []):
            raise ValueError("Declared pin owner does not manage that file")
    for path, record in records.items():
        matches = [
            entry for entry in exclusions if fnmatch.fnmatchcase(path, entry["pattern"])
        ]
        if path in owners:
            record.update(status="managed", adapters=sorted(owners[path]))
        elif matches:
            record.update(status="excluded", reason=matches[0]["reason"])
        else:
            record.update(status="unmanaged")
    rows = [records[key] for key in sorted(records)]
    return dict(
        schema=1,
        complete=all(row["status"] != "unmanaged" for row in rows),
        inputs=rows,
    )


def report(root, arguments):
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
            for name in settings.get("adapters", {})
        },
        constraints=settings.get("constraints", {}),
        exceptions=settings.get("exceptions", []),
        audit_exceptions=tc.config(root).get("audits", {}).get("exceptions", {}),
        target_groups=settings.get("target_groups", {}),
        declarations=declarations(root),
        coverage=coverage(root),
    )


def declarations(root):
    """Report committed package policy and wrapper provenance without executing tools."""
    result = {}
    for name in sorted(filter(None, tracked(root))):
        path = Path(name)
        if path.name == "package.json":
            data = json.loads(tc.regular_input(root, name))
            if "packageManager" in data:
                result[name] = {"packageManager": data["packageManager"]}
        elif path.name == "pnpm-workspace.yaml":
            from ruamel.yaml import YAML

            data = YAML(typ="safe").load(tc.regular_input(root, name)) or {}
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
                "advisory_ignores": data.get("advisories", {}).get("ignore", []),
                "duplicate_skips": data.get("bans", {}).get("skip", []),
                "duplicate_tree_skips": data.get("bans", {}).get("skip-tree", []),
            }
        elif path.name == "gradle-wrapper.properties":
            data = dict(
                line.split("=", 1)
                for line in tc.regular_input(root, name).decode().splitlines()
                if "=" in line and not line.lstrip().startswith(("#", "!"))
            )
            jar = str(path.with_name("gradle-wrapper.jar"))
            result[name] = {
                "distribution_url": data.get("distributionUrl", ""),
                "distribution_sha256": data.get("distributionSha256Sum", ""),
                "wrapper_sha256": hashlib.sha256(
                    tc.regular_input(root, jar)
                ).hexdigest()
                if tc.contained(root, jar).exists()
                else None,
            }
    return result


def run(root, action, args):
    import recipes

    if action == "deps-coverage":
        if args:
            raise ValueError("deps-coverage takes no arguments")
        result = coverage(root)
    else:
        result = report(root, recipes.selection_options(args))
    print(json.dumps(result, indent=2))
    return 0 if result.get("complete", True) else 1
