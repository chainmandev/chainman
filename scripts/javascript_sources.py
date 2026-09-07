"""Retain explicitly declared immutable GitHub leaf packages in pnpm locks.

Registry selection never interprets a remote reference as a registry release.
Retained sources name one manifest alias, repository, full commit, archive hash,
and reason. Their archive manifest must have no dependency or peer graph; broader
source graphs require a separately implemented and audited adapter.
"""

from datetime import timedelta
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile

import registry
import toolchain as tc


def declarations(spec):
    values = spec.get("retained_sources", [])
    if values and spec.get("manager", "pnpm") != "pnpm":
        raise ValueError("Retained GitHub leaf sources currently require pnpm")
    result, seen = [], set()
    for item in values:
        if set(item) != {
            "manifest",
            "package",
            "repository",
            "commit",
            "sha256",
            "reason",
        }:
            raise ValueError(
                "Retained sources require manifest/package/repository/commit/sha256/reason"
            )
        if (
            not str(item["reason"]).strip()
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", item["repository"])
            or any(p in (".", "..") for p in item["repository"].split("/"))
        ):
            raise ValueError(
                "Retained sources require an explicit public GitHub repository and reason"
            )
        if not re.fullmatch(r"[a-f0-9]{40}", item["commit"]) or not re.fullmatch(
            r"[a-f0-9]{64}", item["sha256"]
        ):
            raise ValueError(
                "Retained sources require a full immutable commit and SHA-256"
            )
        registry.package_name("npm", item["package"])
        key = (item["manifest"], item["package"])
        if key in seen:
            raise ValueError("Duplicate retained source manifest alias")
        seen.add(key)
        result.append(
            {
                **item,
                "specifier": f"github:{item['repository']}#{item['commit']}",
                "url": f"https://codeload.github.com/{item['repository']}/tar.gz/{item['commit']}",
                "integrity": "sha256-"
                + base64.b64encode(bytes.fromhex(item["sha256"])).decode(),
            }
        )
    return result


def manifest_entries(directory, item):
    value = json.loads(tc.regular_input(directory, item["manifest"]))
    matches = [
        table[item["package"]]
        for section in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        )
        if item["package"] in (table := value.get(section, {}))
    ]
    if not matches or any(value != item["specifier"] for value in matches):
        raise ValueError(
            "Retained source manifest declaration differs from its immutable policy"
        )


def matched(spec, manifest, alias, value):
    candidates = [
        item
        for item in declarations(spec)
        if (item["manifest"], item["package"]) == (manifest, alias)
    ]
    if candidates and value != candidates[0]["specifier"]:
        raise ValueError("Retained source declaration changed")
    return bool(candidates)


def registry_entries(root, spec, lock):
    entries = dict(lock.get("packages", {}))
    directory = tc.contained(root, spec.get("directory", "."))
    for item in declarations(spec):
        manifest_entries(directory, item)
        key = item["package"] + "@" + item["url"]
        # A new manifest can be declared before its first lock exists. The
        # completed audit requires its importer and source identity to exist.
        if key not in entries:
            continue
        resolution = entries[key].get("resolution", {})
        if (
            set(resolution) - {"tarball", "gitHosted", "integrity"}
            or resolution.get("tarball") != item["url"]
            or resolution.get("gitHosted") is not True
        ):
            raise ValueError("Retained GitHub source lock identity differs from policy")
        if resolution.get("integrity"):
            registry.digest(resolution["integrity"], npm=True)
        if registry.version("npm", entries[key].get("version")) is None:
            raise ValueError(
                "Retained source lock must declare a stable package version"
            )
        importer = str(Path(item["manifest"]).parent)
        edges = [
            table[item["package"]]
            for section in (
                "dependencies",
                "devDependencies",
                "optionalDependencies",
                "peerDependencies",
            )
            if item["package"]
            in (table := lock.get("importers", {}).get(importer, {}).get(section, {}))
        ]
        if not edges or any(
            edge != {"specifier": item["specifier"], "version": item["url"]}
            for edge in edges
        ):
            raise ValueError("Retained source importer differs from its declaration")
        if lock.get("snapshots", {}).get(key) != {}:
            raise ValueError(
                "Retained source must have an empty declared dependency graph"
            )
        del entries[key]
    return entries


def lock_identities(root, spec, lock):
    registry_entries(root, spec, lock)
    result = set()
    for item in declarations(spec):
        package = lock.get("packages", {}).get(item["package"] + "@" + item["url"])
        if package is not None:
            result.add(
                (
                    "github-source",
                    item["package"],
                    package["version"] + "@" + item["commit"],
                    item["url"],
                    "sha256:" + item["sha256"]
                    if package.get("resolution", {}).get("integrity")
                    == item["integrity"]
                    else "",
                )
            )
    return result


def is_target(spec, manifest, alias, raw):
    items = [
        item
        for item in declarations(spec)
        if (item["manifest"], item["package"]) == (manifest, alias)
    ]
    if items and raw != items[0]["url"]:
        raise ValueError("Retained source importer resolution changed")
    return bool(items)


def bind(workspace, directory):
    if not workspace.spec.get("retained_sources"):
        return
    import javascript_updates as js

    path = tc.contained(directory, workspace.lock)
    lock, render = js.document(
        path, tc.regular_input(directory, workspace.lock).decode()
    )
    registry_entries(workspace.root, workspace.spec, lock)
    for item in declarations(workspace.spec):
        key = item["package"] + "@" + item["url"]
        if key not in lock.get("packages", {}):
            raise ValueError("pnpm omitted a declared retained source")
        observed = lock["packages"][key]["resolution"].get("integrity")
        if observed and observed != item["integrity"]:
            body = registry.fetch(item["url"], "application/octet-stream")[0]
            digest = registry.digest(observed, npm=True)
            algorithm, _, encoded = digest.partition(":")
            if (
                len(body) > 8 * 1024 * 1024
                or hashlib.sha256(body).hexdigest() != item["sha256"]
                or hashlib.new(algorithm, body).hexdigest() != encoded
            ):
                raise ValueError(
                    "Resolver source archive integrity differs from pinned bytes"
                )
        lock["packages"][key]["resolution"]["integrity"] = item["integrity"]
    tc.atomic_bytes(path, render().encode(), 0o644)


def audit(workspace, before, policy, now):
    if not workspace.spec.get("retained_sources"):
        return
    import javascript_updates as js

    lock = js.document(
        Path(workspace.lock),
        tc.regular_input(workspace.directory, workspace.lock).decode(),
    )[0]
    registry_entries(workspace.root, workspace.spec, lock)
    for item in declarations(workspace.spec):
        key = item["package"] + "@" + item["url"]
        package = lock.get("packages", {}).get(key, {})
        if package.get("resolution", {}).get("integrity") != item["integrity"]:
            raise ValueError("Retained source lock must bind its declared archive hash")
        manifest = item["manifest"]
        importer = str(Path(manifest).parent)
        edges = [
            table[item["package"]]
            for section in js.SECTIONS
            if item["package"]
            in (table := lock.get("importers", {}).get(importer, {}).get(section, {}))
        ]
        if not edges or any(
            edge != {"specifier": item["specifier"], "version": item["url"]}
            for edge in edges
        ):
            raise ValueError("Retained source importer differs from its declaration")
        if lock.get("snapshots", {}).get(key) != {}:
            raise ValueError(
                "Retained source must have an empty declared dependency graph"
            )
        identity = (
            "github-source",
            item["package"],
            package["version"] + "@" + item["commit"],
            item["url"],
            "sha256:" + item["sha256"],
        )
        content = audit_identity(
            identity, {tuple(i) for i in before["identities"]}, policy, now
        )
        if content.get("version") != package.get("version"):
            raise ValueError("Retained source archive version differs from lock")


def audit_identity(identity, before, policy, now):
    provider, package, version_commit, url, digest = identity
    version, _, revision = version_commit.rpartition("@")
    match = re.fullmatch(
        r"https://codeload\.github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/tar\.gz/([a-f0-9]{40})",
        url,
    )
    if (
        provider != "github-source"
        or not match
        or match[2] != revision
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
    ):
        raise ValueError("Retained source lacks a complete immutable archive identity")
    commit = registry.data(
        f"https://api.github.com/repos/{match[1]}/commits/{revision}"
    )
    if commit.get("sha") != revision:
        raise ValueError("GitHub returned a different retained source commit")
    published = registry.timestamp(commit["commit"]["committer"]["date"])
    if published > now or (
        identity not in before
        and published > now - timedelta(days=registry.minimum_age(policy))
    ):
        raise ValueError("Retained source lacks eligible commit-age evidence")
    body = registry.fetch(url, "application/octet-stream")[0]
    if len(body) > 8 * 1024 * 1024 or hashlib.sha256(
        body
    ).hexdigest() != digest.removeprefix("sha256:"):
        raise ValueError("Retained source archive fails its size or SHA-256 contract")
    manifests = []
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for index, member in enumerate(archive):
            path = PurePosixPath(member.name)
            if index >= 10000 or path.is_absolute() or ".." in path.parts:
                raise ValueError(
                    "Retained source archive has unsafe or excessive paths"
                )
            if len(path.parts) == 2 and path.name == "package.json":
                if not member.isfile() or member.size > 1024 * 1024:
                    raise ValueError(
                        "Retained source manifest must be a bounded regular file"
                    )
                manifests.append(json.load(archive.extractfile(member)))
    if len(manifests) != 1:
        raise ValueError("Retained source requires one root package manifest")
    content = manifests[0]
    if (
        content.get("name") != package
        or content.get("version") != version
        or registry.version("npm", version) is None
    ):
        raise ValueError("Retained source archive package identity differs from lock")
    if any(
        content.get(section)
        for section in (
            "dependencies",
            "optionalDependencies",
            "peerDependencies",
            "bundledDependencies",
            "bundleDependencies",
        )
    ):
        raise ValueError(
            "Retained source dependency graphs require a dedicated adapter"
        )
    if not registry.compatible(
        "npm", content["version"], registry.constraint("npm", policy, package)
    ):
        raise ValueError("Retained source violates package compatibility")
    return content
