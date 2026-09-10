"""Exact Go, SwiftPM, and Gradle identities and their public evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlparse
import xml.etree.ElementTree as ET

import registry
from toolchain import (
    contained,
    environment,
    managed_run,
    entry_command,
    local_source,
    regular_input,
)


def paths(root: Path, directory: Path, filename: str) -> list[Path]:
    return [
        contained(root, str(p.relative_to(root)))
        for p in sorted(directory.rglob(filename))
    ]


def native(
    root: Path, profile: str, argv: list[str], *, workspace: bool = False
) -> dict:
    env = environment(root)
    env.update(
        TOOLCHAIN_FRESH="1",
        GO111MODULE="on",
        GOFLAGS="",
        GOPROXY="https://proxy.golang.org",
        GOSUMDB="sum.golang.org",
        GOPRIVATE="",
        GONOSUMDB="",
        GONOPROXY="",
        GOVCS="*:off",
    )
    if not workspace:
        env["GOWORK"] = "off"
    with tempfile.TemporaryDirectory(prefix="toolchain-evidence-") as tmp:
        result = managed_run(
            [
                *entry_command(root, profile),
                "sh",
                "-eu",
                "-c",
                'cd "$1"; shift; exec "$@"',
                "sh",
                tmp,
                *argv,
            ],
            cwd=root,
            env=env,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    return json.loads(result.stdout)


def go_query(root: Path, package: str, value: str) -> dict:
    registry.go_path(package)
    if value != "latest" and not registry.go_version(value):
        raise ValueError("Invalid Go query version")
    flags = ["-versions"] if value == "latest" else ["-retracted"]
    item = native(
        root, "go", ["go", "list", "-m", "-json", *flags, package + "@" + value]
    )
    if (
        item.get("Path") != package
        or item.get("Error")
        or (value != "latest" and item.get("Version") != value)
    ):
        raise ValueError(
            "Go returned a different module identity or incomplete evidence"
        )
    return item


def go_candidates(root: Path, package: str, **selection) -> list[registry.Release]:
    # Go itself interprets the latest module's retract directives; never parse
    # Go syntax or infer retraction status from the public proxy's version list.
    available = go_query(root, package, "latest").get("Versions")
    if not isinstance(available, list):
        raise ValueError("Go did not return its unretracted version inventory")
    return registry.go_releases(package, available, **selection)


def swift_repository(url: str) -> str:
    registry.artifact_url(url)
    parsed = urlparse(url)
    if (
        parsed.netloc != "github.com"
        or parsed.query
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", parsed.path)
    ):
        raise ValueError(
            "SwiftPM source must be a credential-free public GitHub HTTPS repository"
        )
    repository = parsed.path[1:].removesuffix(".git")
    if any(part in (".", "..", "") for part in repository.split("/")):
        raise ValueError("Invalid SwiftPM GitHub repository")
    return repository


def swift_declarations(root: Path, name: str, *, explicit: bool = False) -> list[dict]:
    """Read only literal package calls; native evaluation verifies their inventory."""
    body = regular_input(root, name).decode()
    # Hide comments and string contents when locating calls, preserving offsets.
    tokens = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"', re.DOTALL)
    masked = tokens.sub(lambda m: " " * len(m[0]), body)
    result, seen = [], set()
    for call in re.finditer(r"\.package\b", masked):
        opening = re.match(r"\s*\(", masked[call.end() :])
        if opening is None:
            raise ValueError("Swift dependencies require literal package calls")
        start = call.end() + opening.end()
        end = masked.find(")", start)
        if end < 0:
            raise ValueError("Unclosed Swift dependency declaration")
        arguments = tokens.sub(
            lambda m: m[0] if m[0].startswith('"') else " " * len(m[0]),
            body[start:end],
        )
        local = re.fullmatch(
            r'\s*(?:name\s*:\s*"(?P<name>[^"\\\n]+)"\s*,\s*)?'
            r'path\s*:\s*"(?P<path>[^"\\\n]+)"\s*,?\s*',
            arguments,
        )
        remote = re.fullmatch(
            r'\s*url\s*:\s*"(?P<url>[^"\\\n]+)"\s*,\s*'
            r'(?P<style>from|exact)\s*:\s*"(?P<version>[^"\\\n]+)"\s*,?\s*',
            arguments,
        )
        if local:
            target = local_source(root, (root / name).parent, local["path"])
            item = {"kind": "local", "path": str(target), "name": local["name"]}
            key = ("local", str(target))
        elif remote:
            package = swift_repository(remote["url"])
            value = remote["version"]
            if (
                not re.fullmatch(r"\d+\.\d+\.\d+", value)
                or registry.version("swift", value) is None
            ):
                raise ValueError("Swift dependencies require a stable release version")
            upper = str(int(value.split(".")[0]) + 1) + ".0.0"
            item = {
                "kind": "remote",
                "package": package,
                "version": value,
                "style": remote["style"],
                "bound": value if remote["style"] == "exact" else f">={value} <{upper}",
                "requirement": {"exact": [value]}
                if remote["style"] == "exact"
                else {"range": [{"lowerBound": value, "upperBound": upper}]},
                "value_start": start + remote.start("version"),
                "value_end": start + remote.end("version"),
                "style_start": start + remote.start("style"),
                "style_end": start + remote.end("style"),
            }
            key = ("remote", package)
        elif explicit and (
            configured := re.fullmatch(
                r'\s*url\s*:\s*(?:[A-Za-z_]\w*|"[^"\\\n]+")\s*,\s*'
                r"exact\s*:\s*(?:(?P<reference>[A-Za-z_]\w*)|"
                r'"(?P<value>[^"\\\n]+)")\s*,?\s*',
                arguments,
            )
        ):
            # Existing explicit pins may own evaluated exact remote declarations.
            # Native inventory and the configured field, not this call text, bind them.
            item = {
                "kind": "explicit",
                "reference": configured["reference"],
                "value_span": (
                    start + configured.start("value"),
                    start + configured.end("value"),
                )
                if configured["value"] is not None
                else None,
            }
            key = ("explicit", call.start())
        else:
            raise ValueError(
                "Swift dependencies require literal from/exact GitHub releases or contained paths"
            )
        if key in seen:
            raise ValueError("Duplicate Swift dependency declaration")
        seen.add(key)
        result.append({**item, "start": call.start(), "end": end + 1})
    return result


def swift_explicit_requirements(root: Path, spec: dict) -> dict:
    result = {}
    name = str((contained(root, spec["directory"]) / "Package.swift").relative_to(root))
    body = regular_input(root, name).decode()
    # A named exact argument must refer to the configured literal initializer;
    # coincidentally equal, unused variables do not own a native dependency.
    visible = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"',
        lambda m: m[0] if m[0].startswith('"') else " " * len(m[0]),
        body,
        flags=re.DOTALL,
    )
    for pin in spec.get("pins", []):
        if pin.get("file") != name:
            continue
        package = pin.get("name")
        if (
            pin.get("provider") != "swift"
            or not isinstance(package, str)
            or swift_repository("https://github.com/" + package) != package
        ):
            raise ValueError("Explicit Swift pin requires its canonical repository")
        if pin.get("format") != "regex" or not isinstance(pin.get("pattern"), str):
            raise ValueError("Explicit Swift pin requires an owning manifest regex")
        matches = list(re.finditer(pin["pattern"], body, re.MULTILINE))
        if (
            len(matches) != 1
            or "value" not in matches[0].groupdict()
            or package in result
        ):
            raise ValueError("Ambiguous explicit Swift pin ownership")
        value = matches[0]["value"]
        if (
            not re.fullmatch(r"\d+\.\d+\.\d+", value)
            or registry.version("swift", value) is None
        ):
            raise ValueError("Explicit Swift pin must own one stable release version")
        span = matches[0].span("value")
        if any(
            span[0] < item["span"][1] and item["span"][0] < span[1]
            for item in result.values()
        ):
            raise ValueError("Overlapping explicit Swift pin ownership")
        references = [
            match["name"]
            for match in re.finditer(
                r"\b(?:let|var)\s+(?P<name>[A-Za-z_]\w*)\s*"
                r"(?::\s*(?:PackageDescription\.)?Version\s*)?"
                r'=\s*"(?P<value>\d+\.\d+\.\d+)"',
                visible,
            )
            if match.span("value") == span
        ]
        result[package] = {"value": value, "span": span, "references": references}
    return result


def swift_local_manifests(root: Path, spec: dict) -> dict:
    """Read the project-local closure without expanding configured pin ownership."""
    pending = [contained(root, spec["directory"]) / "Package.swift"]
    result = {}
    while pending:
        path = pending.pop()
        name = str(path.relative_to(root))
        if name in result:
            continue
        declarations = swift_declarations(root, name, explicit=bool(spec.get("pins")))
        result[name] = declarations
        pending.extend(
            Path(item["path"]) / "Package.swift"
            for item in declarations
            if item["kind"] == "local"
        )
    return result


def swift_manifest_state(root: Path, spec: dict) -> dict:
    return {
        name: [
            hashlib.sha256(regular_input(root, name)).hexdigest(),
            contained(root, name).stat().st_mode,
        ]
        for name in swift_local_manifests(root, spec)
    }


def swift_lock_path(root: Path, spec: dict) -> Path:
    return contained(
        root,
        str(
            (contained(root, spec["directory"]) / "Package.resolved").relative_to(root)
        ),
    )


def swift_validation_state(root: Path, spec: dict) -> dict:
    result = swift_manifest_state(root, spec)
    path = swift_lock_path(root, spec)
    name = str(path.relative_to(root))
    result[name] = (
        [hashlib.sha256(regular_input(root, name)).hexdigest(), path.stat().st_mode]
        if path.exists()
        else None
    )
    return result


def maven_repository(spec: dict, package: str) -> str:
    declared = spec.get("maven_repositories", ["central"])
    if not isinstance(declared, list) or set(declared) - {
        "central",
        "google",
        "plugins",
    }:
        raise ValueError("Unsupported Maven repository declaration")
    name = (
        "google"
        if package.split(":")[0].startswith(("androidx.", "com.android."))
        else "central"
    )
    plugins = spec.get("maven_plugin_packages", [])
    if not isinstance(plugins, list) or any(not isinstance(p, str) for p in plugins):
        raise ValueError("Plugin Portal routing requires an exact coordinate list")
    for plugin in plugins:
        registry.maven_prefix(plugin, "plugins")
    if package in plugins:
        name = "plugins"
    if name not in declared:
        raise ValueError("Maven component requires an undeclared official repository")
    return name


def local_gradle_projects(root: Path, spec: dict) -> dict:
    declared = spec.get("local_projects", {})
    if not isinstance(declared, dict):
        raise ValueError(
            "Local Gradle projects require exact coordinate-to-directory bindings"
        )
    for package, relative in declared.items():
        registry.maven_prefix(package)
        if not isinstance(relative, str) or not relative:
            raise ValueError(
                "Local Gradle project directories must be nonempty relative paths"
            )
        directory = contained(root, relative)
        if not any(
            contained(root, str((directory / name).relative_to(root))).is_file()
            for name in ("build.gradle", "build.gradle.kts")
        ):
            raise ValueError("Local Gradle project lacks its declared build source")
    return declared


def validate_gradle_projects(root: Path, spec: dict, reports: list) -> dict:
    """Join native build-tree identities to actual paths before admitting locals."""
    local = local_gradle_projects(root, spec)
    projects, edges = {}, []
    if not reports:
        raise ValueError("Gradle resolution did not report its native project graph")
    for report in reports:
        if (
            report.get("schema") != 1
            or not isinstance(report.get("projects"), list)
            or not isinstance(report.get("edges"), list)
        ):
            raise ValueError("Malformed Gradle project graph evidence")
        for project in report["projects"]:
            identity, location = project.get("id"), project.get("directory")
            if (
                not isinstance(identity, str)
                or not identity.startswith(":")
                or not isinstance(location, str)
            ):
                raise ValueError("Malformed native Gradle project identity")
            path = Path(location)
            if not path.is_absolute() or not path.is_relative_to(root):
                raise ValueError(
                    "Native Gradle project source escapes the adopted project"
                )
            relative = str(path.relative_to(root))
            contained(root, relative)
            if identity in projects and projects[identity] != relative:
                raise ValueError("Ambiguous native Gradle build-tree identity")
            projects[identity] = relative
        edges.extend(report["edges"])
    for edge in edges:
        selected, coordinate = edge.get("selected"), edge.get("coordinate")
        if selected not in projects:
            raise ValueError("Gradle selected an unreported native project")
        if coordinate is not None:
            if coordinate not in local or projects[selected] != local[coordinate]:
                raise ValueError(
                    "Gradle module substitution differs from its declared local project binding"
                )
    return {"projects": projects, "edges": edges}


def identities(root: Path, spec: dict) -> set[tuple[str, str, str, str, str]]:
    directory = contained(root, spec["directory"])
    kind, result = spec["ecosystem"], set()
    if kind == "go":
        for path in paths(root, directory, "go.sum") + paths(
            root, directory, "go.work.sum"
        ):
            for line in path.read_text().splitlines():
                parts = line.split()
                if len(parts) != 3:
                    raise ValueError("Malformed Go checksum lock entry")
                package, raw, checksum = parts
                value = raw.removesuffix("/go.mod")
                if not registry.go_version(value):
                    raise ValueError("Noncanonical Go lock version")
                suffix = ".mod" if raw.endswith("/go.mod") else ".zip"
                url = f"https://proxy.golang.org/{registry.go_path(package)}/@v/{quote(value, safe='')}{suffix}"
                result.add((kind, package, value, url, registry.go_digest(checksum)))
    elif kind == "swift":
        for path in [swift_lock_path(root, spec)]:
            if not path.exists():
                continue
            content = json.loads(regular_input(root, str(path.relative_to(root))))
            schema = content.get("version")
            if schema == 1:
                pins = content.get("object", {}).get("pins")
            elif schema in (2, 3):
                pins = content.get("pins")
            else:
                raise ValueError("Unsupported SwiftPM resolved schema")
            if not isinstance(pins, list):
                raise ValueError("SwiftPM lock lacks its pin inventory")
            seen = set()
            for item in pins:
                if schema != 1 and item.get("kind") != "remoteSourceControl":
                    raise ValueError("Unsupported SwiftPM lock source")
                url = item.get("repositoryURL" if schema == 1 else "location")
                package = swift_repository(url)
                state = item.get("state", {})
                value, revision = state.get("version"), state.get("revision", "")
                if (
                    registry.version("swift", value or "") is None
                    or state.get("branch")
                    or not re.fullmatch(r"[a-f0-9]{40}", revision)
                ):
                    raise ValueError(
                        "SwiftPM lock requires an exact stable release and revision"
                    )
                if package in seen:
                    raise ValueError("Duplicate SwiftPM source identity")
                seen.add(package)
                result.add((kind, package, value, url, "git:" + revision))
    elif kind == "maven":
        local = local_gradle_projects(root, spec)
        required = [
            p for p in spec.get("artifacts", []) if p.endswith((".lockfile", ".xml"))
        ]
        for name in required:
            if not contained(root, name).is_file():
                raise ValueError(
                    "Missing declared Gradle lock or verification metadata"
                )
        locked = set()
        for path in paths(root, directory, "*.lockfile"):
            for line in path.read_text().splitlines():
                if not line or line.startswith("#"):
                    continue
                component, equal, configurations = line.partition("=")
                if not equal or (component != "empty" and not configurations):
                    raise ValueError("Malformed Gradle dependency lock entry")
                if component == "empty":
                    continue
                fields = component.split(":")
                if len(fields) != 3 or registry.version("maven", fields[2]) is None:
                    raise ValueError("Unsupported Gradle lock coordinates")
                package = ":".join(fields[:2])
                if package in local:
                    raise ValueError(
                        "Declared local Gradle project resolved as an external module"
                    )
                registry.maven_prefix(package, maven_repository(spec, package))
                locked.add((package, fields[2]))
        metadata = contained(
            root,
            str((directory / "gradle/verification-metadata.xml").relative_to(root)),
        )
        if not metadata.is_file():
            raise ValueError("Missing Gradle artifact verification metadata")
        document = ET.fromstring(metadata.read_bytes())
        ns = "{https://schema.gradle.org/dependency-verification}"
        if document.tag != ns + "verification-metadata":
            raise ValueError("Unsupported Gradle verification schema")
        configuration = document.find(ns + "configuration")
        if (
            configuration is None
            or configuration.findtext(ns + "verify-metadata") != "true"
        ):
            raise ValueError("Gradle dependency metadata must be verified")
        if configuration.find(ns + "trusted-artifacts") is not None:
            raise ValueError(
                "Gradle trusted-artifact bypasses cannot replace checksum evidence"
            )
        covered = set()
        seen = set()
        for component in document.findall(f"{ns}components/{ns}component"):
            package = component.get("group", "") + ":" + component.get("name", "")
            if package in local:
                raise ValueError(
                    "Declared local Gradle project has external artifact metadata"
                )
            value = component.get("version", "")
            repository = maven_repository(spec, package)
            prefix = registry.maven_prefix(package, repository)
            if registry.version("maven", value) is None:
                raise ValueError("Unsupported Maven verification version")
            artifacts = component.findall(ns + "artifact")
            if not artifacts:
                raise ValueError("Maven component lacks artifact checksums")
            for artifact in artifacts:
                filename = artifact.get("name", "")
                if not re.fullmatch(r"[A-Za-z0-9_.+-]+", filename):
                    raise ValueError("Unsafe Maven verification artifact name")
                sha = artifact.findall(ns + "sha256")
                if len(sha) != 1 or list(sha[0]):
                    raise ValueError(
                        "Maven artifact requires exactly one SHA256 identity"
                    )
                url = f"{prefix}/{value}/{filename}"
                if url in seen:
                    raise ValueError("Duplicate Maven verification artifact")
                seen.add(url)
                result.add(
                    (
                        kind,
                        package,
                        value,
                        url,
                        registry.digest("sha256:" + sha[0].get("value", "")),
                    )
                )
            covered.add((package, value))
        if locked - covered:
            raise ValueError(
                "Gradle locked component lacks artifact verification evidence"
            )
    return result


def evidence(
    root: Path, provider: str, package: str, items: set
) -> list[registry.Release]:
    values = {i[2] for i in items}
    if provider == "go":
        result = []
        for value in values:
            if go_query(root, package, value).get("Retracted"):
                raise ValueError("Locked Go module version is retracted")
            artifacts = registry.go_artifacts(package, value)
            result.append(
                registry.Release(
                    value, max(a.published for a in artifacts), artifacts=artifacts
                )
            )
        return result
    if provider == "swift":
        import source_updates

        result = []
        for release in registry.releases(provider, package):
            if release.version in values:
                revision = registry.github_commit(package, release.identity)
                published = max(
                    release.published, source_updates.commit_time(package, revision)
                )
                artifacts = tuple(
                    registry.Artifact(url, "git:" + revision, published)
                    for _, _, value, url, _ in items
                    if value == release.version
                )
                result.append(
                    registry.Release(release.version, published, artifacts=artifacts)
                )
        return result
    if provider == "maven":
        result = []
        for value in values:
            artifacts = []
            for _, _, locked, url, _ in items:
                if locked != value:
                    continue
                repository = maven_source(url)
                artifact = registry.maven_artifact(
                    package, value, url.rsplit("/", 1)[1], repository
                )
                if artifact.url != url:
                    raise ValueError(
                        "Maven lock source differs from its official evidence"
                    )
                artifacts.append(artifact)
            result.append(
                registry.Release(
                    value,
                    max(a.published for a in artifacts),
                    artifacts=tuple(artifacts),
                )
            )
        return result
    raise ValueError("Unsupported lock evidence adapter")


def maven_source(url: str) -> str:
    for prefix, source in (
        ("https://dl.google.com/", "google"),
        ("https://plugins.gradle.org/", "plugins"),
        ("https://repo.maven.apache.org/", "central"),
    ):
        if url.startswith(prefix):
            return source
    raise ValueError("Unrecognized Maven artifact source")


def validate_go_sources(root: Path, spec: dict, items: set) -> None:
    directory = contained(root, spec["directory"])
    manifests = [
        (p, native(root, "go", ["go", "mod", "edit", "-json", str(p)]))
        for p in paths(root, directory, "go.mod")
    ]

    def local_path(base: Path, value: str) -> Path:
        path = base / value
        # Check the original chain before normalization can erase symlink hops.
        for item in (path, *path.parents):
            if item.is_symlink():
                raise ValueError("Symlink Go local source")
        location = path.resolve()
        relative = location.relative_to(directory.resolve())
        contained(directory, str(relative))
        if not location.is_dir():
            raise ValueError("Invalid Go local source")
        return location

    # Resolve the actual workspace from the same configured cwd as module commands,
    # preserving GOWORK for this query. A recursive work file is not automatically
    # active, and an inherited/ancestor workspace cannot silently escape the project.
    selection = native(
        root,
        "go",
        ["go", "-C", str(directory), "env", "-json", "GOWORK"],
        workspace=True,
    )
    active = selection.get("GOWORK")
    if not isinstance(active, str):
        raise ValueError("Go did not identify the active command workspace")
    workpath, work = None, {}
    if active not in ("", "off"):
        candidate = Path(active)
        if not candidate.is_absolute() or not candidate.is_relative_to(root):
            raise ValueError("Active Go workspace is outside the adopted project root")
        workpath = contained(root, str(candidate.relative_to(root)))
        if not workpath.is_file():
            raise ValueError("Active Go workspace does not exist")
        work = native(root, "go", ["go", "work", "edit", "-json", str(workpath)])
    used = {
        local_path(workpath.parent, entry["DiskPath"])
        for entry in work.get("Use") or []
    }
    by_directory = {path.parent.resolve(): (path, body) for path, body in manifests}
    if used - by_directory.keys():
        raise ValueError("Go workspace use entry lacks a declared module")

    def replacements(path: Path, body: dict) -> dict:
        result = {}
        for entry in body.get("Replace") or []:
            old = entry["Old"]
            key = (old["Path"], old.get("Version") or "")
            target = entry["New"]
            # Normalize local target identities for cross-member conflict checks;
            # scope/existence is checked only for the effective replacement.
            value = (
                (
                    str((path.parent / target["Path"]).resolve()),
                    "",
                    path.parent,
                    target["Path"],
                )
                if not target.get("Version")
                else (target["Path"], target["Version"], None, None)
            )
            if key in result and result[key][:2] != value[:2]:
                raise ValueError("Conflicting Go replacement directives")
            result[key] = value
        return result

    def matching(mapping: dict, package: str, value: str):
        return mapping.get((package, value), mapping.get((package, "")))

    workspace_replacements = replacements(workpath, work) if workpath else {}
    member_replacements = {}
    for path, body in (by_directory[member] for member in used):
        for key, target in replacements(path, body).items():
            if matching(workspace_replacements, *key) is not None:
                continue
            if (
                key in member_replacements
                and member_replacements[key][:2] != target[:2]
            ):
                raise ValueError(
                    "Conflicting Go workspace member replacements need a go.work override"
                )
            member_replacements[key] = target
    local = {by_directory[member][1]["Module"]["Path"] for member in used}
    for path, body in manifests:
        member = path.parent.resolve() in used
        own = member_replacements if member else replacements(path, body)
        for entry in body.get("Require") or []:
            package, value = entry["Path"], entry["Version"]
            if member and package in local:
                continue
            replacement = (
                matching(workspace_replacements, package, value) if member else None
            )
            if replacement is None:
                replacement = matching(own, package, value)
            if replacement is not None:
                if replacement[1]:
                    raise ValueError(
                        "Go remote replacement needs an explicit supported source contract"
                    )
                location = local_path(replacement[2], replacement[3])
                if location not in by_directory:
                    raise ValueError("Go local replacement lacks a declared module")
                continue
            if not any(i[1:3] == (package, value) for i in items):
                raise ValueError("Go requirement lacks a checksum lock identity")


def swift_declared_sources(
    root: Path, spec: dict, items: set, *, require_locked: bool = True
) -> set:
    directory = contained(root, spec["directory"])
    declarations = swift_declarations(
        root,
        str((directory / "Package.swift").relative_to(root)),
        explicit=bool(spec.get("pins")),
    )
    configured = swift_explicit_requirements(root, spec)
    body = native(
        root,
        spec.get("profile", "swift"),
        ["swift", "package", "--package-path", str(directory), "dump-package"],
    )
    dependencies = body.get("dependencies") if isinstance(body, dict) else None
    if not isinstance(dependencies, list):
        raise ValueError("SwiftPM native dependency inventory is missing or malformed")
    expected = {
        (item["kind"], item.get("package", item.get("path"))): item
        for item in declarations
        if item["kind"] != "explicit"
    }
    explicit_count = sum(item["kind"] == "explicit" for item in declarations)
    explicit_seen = set()
    seen = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict) or len(dependency) != 1:
            raise ValueError("Unsupported SwiftPM native dependency source")
        kind = next(iter(dependency))
        sources = dependency[kind]
        if (
            not isinstance(sources, list)
            or len(sources) != 1
            or not isinstance(sources[0], dict)
        ):
            raise ValueError("Malformed SwiftPM native dependency source")
        source = sources[0]
        if kind == "fileSystem":
            path = source.get("path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise ValueError(
                    "SwiftPM local source requires an absolute native path"
                )
            key = ("local", path)
            declared = expected.get(key)
            if (
                declared is None
                or source.get("nameForTargetDependencyResolutionOnly")
                != declared["name"]
            ):
                raise ValueError(
                    "SwiftPM local source differs from its declared path or name"
                )
        elif kind == "sourceControl":
            location = source.get("location")
            remote = location.get("remote") if isinstance(location, dict) else None
            if (
                not isinstance(remote, list)
                or len(remote) != 1
                or not isinstance(remote[0], dict)
            ):
                raise ValueError("SwiftPM dependency requires a public GitHub remote")
            package = swift_repository(remote[0].get("urlString"))
            key = ("remote", package)
            declared = expected.get(key)
            if declared is None and package in configured:
                owner = configured[package]
                calls = [
                    item
                    for item in declarations
                    if item["kind"] == "explicit"
                    and (
                        item["value_span"] == owner["span"]
                        or item["reference"] in owner["references"]
                    )
                ]
                if len(calls) != 1 or calls[0]["start"] in explicit_seen:
                    raise ValueError(
                        "Explicit Swift pin lacks unique declaration ownership"
                    )
                explicit_seen.add(calls[0]["start"])
                requirement = source.get("requirement")
                exact = (
                    requirement.get("exact") if isinstance(requirement, dict) else None
                )
                if (
                    not isinstance(exact, list)
                    or len(exact) != 1
                    or set(requirement) != {"exact"}
                    or not isinstance(exact[0], str)
                    or not re.fullmatch(r"\d+\.\d+\.\d+", exact[0])
                    or registry.version("swift", exact[0]) is None
                ):
                    raise ValueError(
                        "Explicit Swift dependencies require evaluated exact releases"
                    )
                declared = {"bound": exact[0], "requirement": requirement}
            if declared is None or source.get("requirement") != declared["requirement"]:
                raise ValueError(
                    "SwiftPM native requirement differs from its literal declaration"
                )
            if package in configured:
                if declared.get("kind") == "remote" and configured[package]["span"] != (
                    declared["value_start"],
                    declared["value_end"],
                ):
                    raise ValueError(
                        "Explicit Swift pin does not own its literal declaration"
                    )
                value = declared["requirement"].get("exact", [None])[0]
                if value is None:
                    value = declared["requirement"]["range"][0]["lowerBound"]
                if value != configured[package]["value"]:
                    raise ValueError(
                        "Swift native requirement differs from its configured pin"
                    )
            locked = [item for item in items if item[:2] == ("swift", package)]
            if require_locked and (
                len(locked) != 1
                or not registry.compatible("swift", locked[0][2], declared["bound"])
            ):
                raise ValueError(
                    "SwiftPM manifest dependency lacks its required resolved identity"
                )
        else:
            raise ValueError("Unsupported SwiftPM native dependency source")
        if key in seen:
            raise ValueError("Duplicate SwiftPM native dependency source")
        seen.add(key)
    if (
        not set(expected).issubset(seen)
        or len(seen) != len(expected) + explicit_count
        or len(explicit_seen) != explicit_count
        or not {("remote", package) for package in configured}.issubset(seen)
    ):
        raise ValueError(
            "SwiftPM native dependency inventory differs from its declarations"
        )
    return seen


def validate_swift_graph(root: Path, spec: dict, items: set, edges: dict) -> None:
    directory = contained(root, spec["directory"])
    key = hashlib.sha256(str(directory).encode()).hexdigest()
    work = Path(environment(root)["TOOLCHAIN_WORK"])
    scratch = contained(root, str((work / "swift-audit" / key).relative_to(root)))
    graph = native(
        root,
        spec.get("profile", "swift"),
        [
            "swift",
            "package",
            "--package-path",
            str(directory),
            "--scratch-path",
            str(scratch),
            "--skip-update",
            "--force-resolved-versions",
            "show-dependencies",
            "--format",
            "json",
        ],
    )
    remote, local, names = set(), set(), {}

    def identity(node):
        if (
            not isinstance(node, dict)
            or set(node)
            != {"identity", "name", "url", "version", "path", "dependencies"}
            or any(
                not isinstance(node[field], str) or not node[field]
                for field in ("identity", "name", "url", "version", "path")
            )
            or not isinstance(node["dependencies"], list)
        ):
            raise ValueError("Malformed SwiftPM resolved graph node")
        path = Path(node["path"])
        if not path.is_absolute():
            raise ValueError("SwiftPM graph requires an absolute source path")
        if node["url"] in edges:
            if node["path"] != node["url"] or node["version"] != "unspecified":
                raise ValueError("SwiftPM graph changed a project-local source")
            contained(root, str(path.relative_to(root)))
            return ("local", node["url"])
        package = swift_repository(node["url"])
        if registry.version(
            "swift", node["version"]
        ) is None or not path.is_relative_to(scratch / "checkouts"):
            raise ValueError("Unsupported SwiftPM resolved graph source")
        contained(root, str(path.relative_to(root)))
        return ("remote", package)

    if identity(graph) != ("local", str(directory)):
        raise ValueError("SwiftPM resolved graph has a different command root")
    pending = [graph]
    while pending:
        node = pending.pop()
        kind, source = identity(node)
        resolved = (kind, source, node["version"])
        if node["identity"] in names and names[node["identity"]] != resolved:
            raise ValueError("Conflicting SwiftPM graph package identity")
        names[node["identity"]] = resolved
        children = [identity(child) for child in node["dependencies"]]
        if len(children) != len(set(children)):
            raise ValueError("Duplicate SwiftPM graph dependency edge")
        if kind == "local":
            local.add(source)
            if set(children) != edges[source]:
                raise ValueError("SwiftPM graph omits or changes a declared dependency")
        else:
            remote.add((source, node["version"]))
        pending.extend(node["dependencies"])
    locked = {(item[1], item[2]) for item in items if item[0] == "swift"}
    if local != set(edges) or remote != locked:
        raise ValueError(
            "SwiftPM resolved graph differs from the command-root lock inventory"
        )


def validate_swift_sources(
    root: Path, spec: dict, items: set, *, require_locked: bool = True
) -> None:
    before = swift_validation_state(root, spec)
    original = None
    try:
        edges = {}
        for name in swift_local_manifests(root, spec):
            directory = str(Path(name).parent)
            edges[str(contained(root, directory))] = swift_declared_sources(
                root,
                {**spec, "directory": directory},
                items,
                require_locked=require_locked,
            )
        if require_locked:
            # Caller-supplied or recursively aggregated identities cannot discharge
            # this command root's lock obligation.
            actual = identities(root, spec)
            if actual != items:
                raise ValueError(
                    "SwiftPM audit identities differ from the command-root lock"
                )
            validate_swift_graph(root, spec, actual, edges)
    except BaseException as error:
        original = error
        raise
    finally:
        try:
            if swift_validation_state(root, spec) != before:
                raise ValueError(
                    "unexpected manifest closure, lock bytes or full modes"
                )
        except (OSError, ValueError) as error:
            message = (
                f"Swift validation changed guarded inputs; changes preserved: {error}"
            )
            if original is None:
                raise ValueError(message) from error
            print(message, file=sys.stderr)
            original.add_note(message)
