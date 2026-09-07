"""Exact Go, SwiftPM, and Gradle identities and their public evidence."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote, urlparse
import xml.etree.ElementTree as ET

import registry
from toolchain import contained, environment, managed_run, entry_command


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


def go_candidates(root: Path, package: str) -> list[registry.Release]:
    candidates = registry.releases("go", package)
    # Go itself interprets the latest module's retract directives; never parse
    # Go syntax or infer retraction status from the public proxy's version list.
    available = go_query(root, package, "latest").get("Versions")
    if not isinstance(available, list):
        raise ValueError("Go did not return its unretracted version inventory")
    return [r for r in candidates if r.version in available]


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
        for path in paths(root, directory, "Package.resolved"):
            content = json.loads(path.read_text())
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
        result = []
        for release in registry.releases(provider, package):
            if release.version in values:
                revision = registry.github_commit(package, release.identity)
                artifacts = tuple(
                    registry.Artifact(url, "git:" + revision, release.published)
                    for _, _, value, url, _ in items
                    if value == release.version
                )
                result.append(
                    registry.Release(
                        release.version, release.published, artifacts=artifacts
                    )
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


def validate_swift_sources(root: Path, spec: dict, items: set) -> None:
    directory = contained(root, spec["directory"])
    body = native(
        root,
        "swift",
        ["swift", "package", "--package-path", str(directory), "dump-package"],
    )
    for dependency in body.get("dependencies", []):
        sources = dependency.get("sourceControl")
        if not isinstance(sources, list) or len(sources) != 1:
            raise ValueError(
                "SwiftPM dependency needs an exact public GitHub source contract"
            )
        source = sources[0]
        remote = source.get("location", {}).get("remote")
        exact = source.get("requirement", {}).get("exact")
        if (
            not isinstance(remote, list)
            or len(remote) != 1
            or not isinstance(exact, list)
            or len(exact) != 1
        ):
            raise ValueError(
                "SwiftPM direct dependency must use an exact GitHub release version"
            )
        package = swift_repository(remote[0].get("urlString"))
        if not any(i[1:3] == (package, exact[0]) for i in items):
            raise ValueError(
                "SwiftPM manifest dependency lacks its exact resolved identity"
            )
