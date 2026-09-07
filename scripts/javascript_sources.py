"""Retain explicitly declared immutable GitHub packages in pnpm locks.

Registry selection never interprets a remote reference as a registry release.
Retained sources name one manifest alias, repository, full commit, archive hash,
and reason. Transitive declarations also bind an exact registry parent and its
original source declaration, with a mandatory immutable parent-scoped override.
Source graphs may contain registry children; bundled or nested sources fail.
"""

from datetime import timedelta
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from urllib.parse import quote

from semantic_version import NpmSpec, Version

import registry
import toolchain as tc


def declarations(spec):
    values = spec.get("retained_sources", [])
    if values and spec.get("manager", "pnpm") != "pnpm":
        raise ValueError("Retained GitHub sources currently require pnpm")
    result, seen = [], set()
    for item in values:
        required = {
            "manifest",
            "package",
            "repository",
            "commit",
            "sha256",
            "reason",
        }
        if set(item) not in (required, required | {"parent", "parent_specifier"}):
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
        if "parent" in item:
            parent, separator, version = item["parent"].rpartition("@")
            registry.package_name("npm", parent)
            if (
                not separator
                or registry.version("npm", version) is None
                or not isinstance(item["parent_specifier"], str)
                or not item["parent_specifier"].strip()
            ):
                raise ValueError(
                    "Retained transitive sources require an exact parent version and source declaration"
                )
            if item["parent_specifier"] not in (
                f"github:{item['repository']}#{item['commit']}",
                f"git+https://github.com/{item['repository']}.git",
                f"git+https://github.com/{item['repository']}.git#{item['commit']}",
            ):
                raise ValueError(
                    "Retained parent declaration must name the configured public GitHub repository"
                )
        key = (item["manifest"], item.get("parent"), item["package"])
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
    alias = item["parent"].rpartition("@")[0] if "parent" in item else item["package"]
    matches = [
        table[alias]
        for section in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        )
        if alias in (table := value.get(section, {}))
    ]
    if "parent" in item:
        valid = matches and all(
            Version(item["parent"].rpartition("@")[2]) in NpmSpec(value)
            for value in matches
        )
    else:
        valid = matches and all(value == item["specifier"] for value in matches)
    if not valid:
        raise ValueError(
            "Retained source manifest declaration differs from its immutable policy"
        )


def matched(spec, manifest, alias, value):
    candidates = [
        item
        for item in declarations(spec)
        if "parent" not in item
        and (item["manifest"], item["package"]) == (manifest, alias)
    ]
    if candidates and value != candidates[0]["specifier"]:
        raise ValueError("Retained source declaration changed")
    return bool(candidates)


def override(spec, selector, value):
    candidates = [
        i
        for i in declarations(spec)
        if i.get("parent", "") + ">" + i["package"] == selector
    ]
    if candidates and value != candidates[0]["specifier"]:
        raise ValueError(
            "Retained source override must preserve its exact immutable commit"
        )
    return bool(candidates)


def configured(workspace):
    overrides = {
        **workspace.documents["package.json"][0].get("pnpm", {}).get("overrides", {}),
        **workspace.settings.get("overrides", {}),
    }
    for item in declarations(workspace.spec):
        if "parent" in item:
            selector = item["parent"] + ">" + item["package"]
            if overrides.get(selector) != item["specifier"]:
                raise ValueError(
                    "Retained transitive source requires its immutable parent-scoped override"
                )


def bound_edges(item, lock):
    if "parent" in item:
        parent_name, _, parent_version = item["parent"].rpartition("@")
        importer = str(Path(item["manifest"]).parent)
        incoming = [
            v[parent_name].get("version")
            for section in (
                "dependencies",
                "devDependencies",
                "optionalDependencies",
                "peerDependencies",
            )
            if parent_name
            in (v := lock.get("importers", {}).get(importer, {}).get(section, {}))
        ]
        if not incoming or any(
            value.partition("(")[0] != parent_version for value in incoming
        ):
            raise ValueError("Retained source registry parent changed")
        parents = [
            node
            for context, node in lock.get("snapshots", {}).items()
            if context.partition("(")[0] == item["parent"]
        ]
        edges = [
            node.get("dependencies", {}).get(
                item["package"],
                node.get("optionalDependencies", {}).get(item["package"]),
            )
            for node in parents
        ]
        if not edges or any(value != item["url"] for value in edges):
            raise ValueError(
                "Retained source parent edge differs from its immutable declaration"
            )
    else:
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
        bound_edges(item, lock)
        if key not in lock.get("snapshots", {}):
            raise ValueError("Retained source lacks its locked dependency graph")
        if "parent" not in item and lock["snapshots"][key] != {}:
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
        if "parent" not in item
        and (item["manifest"], item["package"]) == (manifest, alias)
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
        return {}
    import javascript_updates as js

    lock = js.document(
        Path(workspace.lock),
        tc.regular_input(workspace.directory, workspace.lock).decode(),
    )[0]
    registry_entries(workspace.root, workspace.spec, lock)
    contents = {}
    configured(workspace)
    for item in declarations(workspace.spec):
        key = item["package"] + "@" + item["url"]
        package = lock.get("packages", {}).get(key, {})
        if package.get("resolution", {}).get("integrity") != item["integrity"]:
            raise ValueError("Retained source lock must bind its declared archive hash")
        bound_edges(item, lock)
        if "parent" in item:
            parent, _, version = item["parent"].rpartition("@")
            parent_info = registry.data(
                f"https://registry.npmjs.org/{quote(parent, safe='')}/{version}"
            )
            declared = {
                **parent_info.get("dependencies", {}),
                **parent_info.get("optionalDependencies", {}),
            }
            if (
                parent_info.get("name") != parent
                or parent_info.get("version") != version
                or declared.get(item["package"]) != item["parent_specifier"]
            ):
                raise ValueError("Retained source parent registry declaration changed")
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
        if "parent" not in item and any(
            content.get(section)
            for section in ("dependencies", "optionalDependencies", "peerDependencies")
        ):
            raise ValueError(
                "Direct retained source dependency graphs require an explicit registry parent"
            )
        contents[key] = content
    return contents


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
    if content.get("bundledDependencies") or content.get("bundleDependencies"):
        raise ValueError("Retained source bundled dependency graphs are unsupported")
    import javascript_updates as js

    for section in ("dependencies", "optionalDependencies", "peerDependencies"):
        values = content.get(section, {})
        if not isinstance(values, dict) or len(values) > 256:
            raise ValueError(
                "Retained source registry dependency graph exceeds its bound"
            )
        for alias, requirement in values.items():
            if js.parse_requirement(alias, requirement) is None:
                raise ValueError(
                    "Retained source graphs cannot introduce nested local or remote sources"
                )
    if not registry.compatible(
        "npm", content["version"], registry.constraint("npm", policy, package)
    ):
        raise ValueError("Retained source violates package compatibility")
    safe = registry.minimum_safe("npm", policy, package)
    if safe is not None and Version(content["version"]) < safe:
        raise ValueError("Retained source is below its declared security safe floor")
    return content
