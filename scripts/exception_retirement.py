"""Remove temporary age exceptions only after auditing their complete scope.

The original policy remains the authority for this transaction. No security
floor is copied elsewhere: future vulnerability detection belongs to the gate.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import stat
import sys
import tomllib
from typing import cast

import tomlkit

import adapter_data as ad
import dependency_api as api
from dependency_identity import Identity, inventory
import lock_adapters
import registry
import toolchain as tc
import configuration_files


@dataclass(frozen=True)
class Document:
    path: str
    body: bytes
    prefix: tuple[str, ...]


def has_exceptions(settings: Mapping[str, object]) -> bool:
    return bool(settings.get("exceptions")) or any(
        ad.table(ad.table(spec, "Adapter").get("policy", {}), "Adapter policy").get(
            "exceptions"
        )
        for spec in ad.table(settings.get("adapters", {}), "Adapters").values()
    )


def documents(root: Path) -> list[Document]:
    """Read original bytes, including shadowed lists, before running any hooks."""
    filename = (
        "chainman.toml" if (root / "chainman.toml").is_file() else "toolchain.toml"
    )
    source = configuration_files.read(root, filename)
    policy = ad.table(source.data.get("updates", {}), "Updates")
    result = [
        Document(path, body, ("updates",)) for path, body in source.documents.items()
    ]
    extra = policy.get("policy_file")
    if filename == "toolchain.toml":
        extra = "dependencies.toml"
    if extra:
        path = ad.text(extra, "Policy file")
        result.append(Document(path, tc.regular_input(root, path), ()))
    return result


def lists(document: Document) -> dict[tuple[str, ...], list[object]]:
    value = tomllib.loads(document.body.decode())
    for key in document.prefix:
        value = ad.table(value.get(key, {}), "Policy")
    result = {}
    if "exceptions" in value:
        result[document.prefix + ("exceptions",)] = ad.array(
            value["exceptions"], "Exceptions"
        )
    for name, raw in ad.table(value.get("adapters", {}), "Adapters").items():
        policy = ad.table(ad.table(raw, "Adapter").get("policy", {}), "Adapter policy")
        if "exceptions" in policy:
            result[document.prefix + ("adapters", name, "policy", "exceptions")] = (
                ad.array(policy["exceptions"], "Exceptions")
            )
    return result


def output_paths(root: Path) -> list[str]:
    return [doc.path for doc in documents(root) if lists(doc)]


def permitted(
    original: Document, current: bytes, allowed: Mapping[tuple[str, ...], set[int]]
) -> bool:
    """Only an ordered subset of approved, whole entries may disappear."""
    old = tomllib.loads(original.body.decode())
    new = tomllib.loads(current.decode())
    for path, entries in lists(original).items():
        parent = new
        for key in path[:-1]:
            nested = parent.get(key)
            if not isinstance(nested, dict):
                return False
            parent = nested
        remaining = list(ad.array(parent.get(path[-1], []), "Exceptions"))
        for index, entry in enumerate(entries):
            if remaining and remaining[0] == entry:
                remaining.pop(0)
            elif index not in allowed.get(path, set()):
                return False
        if remaining:
            return False
        parent[path[-1]] = entries
    return old == new


def check_policy_changes(
    original: Path, candidate: Path, *, allow_retirement: bool = True
) -> None:
    """Inspection admits only exception deletion; runtime audits prove eligibility."""
    for document in documents(original):
        if not permitted(
            document,
            tc.regular_input(candidate, document.path),
            {
                path: set(range(len(entries))) if allow_retirement else set()
                for path, entries in lists(document).items()
            },
        ):
            raise ValueError(
                "Update must not change its workflow or dependency policy except to retire security exceptions"
            )


def affects(spec: Mapping[str, object], provider: str, package: str) -> bool:
    kind = spec.get("adapter")
    if kind == "toolchain":
        return any(
            tool.get("provider") == provider
            and registry.package_name(provider, ad.text(tool.get("name"), "SDK name"))
            == package
            for raw in ad.array(spec.get("tools", []), "SDK tools")
            for tool in [ad.table(raw, "SDK tool")]
        )
    return {
        "javascript": "npm",
        "rust": "crates",
        "python": "pypi",
        "flutter": "pub",
        "swift": "swift",
        "gradle": "maven",
        "go": "go",
        "actions": "github",
        "oci": "docker",
    }.get(ad.text(kind, "Adapter kind")) == provider


def mature_artifacts(
    root: Path, identities: set[Identity], policy: ad.Table, now: datetime
) -> bool:
    if not identities:
        return True
    provider, package = next(iter(identities))[:2]
    if provider in {"go", "swift", "maven"}:
        releases = lock_adapters.evidence(root, provider, package, identities)
    elif provider == "npm":
        releases = registry.releases(
            provider, package, include_prerelease=True, include_deprecated=True
        )
    else:
        releases = registry.releases(provider, package)
    cutoff = now - timedelta(days=registry.minimum_age(policy))
    safe = registry.minimum_safe(provider, policy, package)
    mature = True
    for item in identities:
        rank = registry.lock_version(provider, item.version)
        if rank is None or (safe is not None and rank < safe):
            raise ValueError(
                "Exception retirement found an artifact below its security safe floor"
            )
        artifacts = [
            a
            for release in releases
            if release.version == item.version
            for a in release.artifacts
            if a.digest == item.digest and (not item.url or a.url == item.url)
        ]
        if not artifacts:
            raise ValueError(
                "Exception retirement lacks matching immutable artifact evidence"
            )
        published = max(a.published for a in artifacts)
        if published > now:
            raise ValueError("Exception retirement found future artifact evidence")
        mature = mature and published <= cutoff
    return mature


def mature_scope(
    root: Path,
    spec: ad.Table,
    before: ad.Table,
    policy: ad.Table,
    provider: str,
    package: str,
    now: datetime,
) -> bool:
    kind = spec["adapter"]
    if kind in {"javascript", "rust", "python", "flutter", "swift", "gradle", "go"}:
        observed = ad.table(
            api.implementation(spec).snapshot(root, spec), "Audited inventory"
        )
        raw = ad.array(observed["identities"], "Audited identities")
        actual = inventory(
            [["go", *ad.strings(item, "Go identity")] for item in raw]
            if kind == "go"
            else raw
        )
        return mature_artifacts(
            root, {i for i in actual if i[:2] == (provider, package)}, policy, now
        )
    # Source adapters have already bound their selected identity during audit.
    evidence: list[tuple[str, datetime]] = []
    if kind == "toolchain":
        import source_toolchain

        observed_tools = source_toolchain.snapshot(root, spec)["tools"]
        evidence = [
            (item["release"], registry.timestamp(item["published"]))
            for item in observed_tools
            if item["provider"] == provider
            and registry.package_name(provider, item["name"]) == package
        ]
    elif kind == "actions":
        import source_updates

        for item in source_updates.action_plan(before, spec, policy, now):
            if registry.package_name(provider, item["repository"]) != package:
                continue
            selected = item["selected"]
            if "version" not in selected or "published" not in selected:
                return False  # A retained pin without release evidence is not proof.
            evidence.append(
                (
                    ad.text(selected["version"], "Action version"),
                    registry.timestamp(selected["published"]),
                )
            )
    elif kind == "oci":
        import source_updates

        for image in source_updates.oci_inventory(root, spec).values():
            if registry.package_name(provider, image["repository"]) != package:
                continue
            releases = source_updates.oci_candidates(
                image["repository"], image["versionSource"]
            )
            matching = [
                r
                for r in releases
                if r.version == image["tag"] and r.identity == image["digest"]
            ]
            parsed = source_updates.oci_tag(image["tag"])
            if not matching or parsed is None:
                raise ValueError(
                    "Exception retirement lacks matching immutable image evidence"
                )
            evidence.append((parsed[0], max(r.published for r in matching)))
    else:
        return False
    safe = registry.minimum_safe(provider, policy, package)
    for version, published in evidence:
        if safe is not None and registry.stable_version(provider, version) < safe:
            raise ValueError(
                "Exception retirement found a source below its security safe floor"
            )
        if published > now:
            raise ValueError("Exception retirement found future source evidence")
    return all(
        published <= now - timedelta(days=registry.minimum_age(policy))
        for _, published in evidence
    )


def retire(
    root: Path,
    originals: list[Document],
    settings: Mapping[str, object],
    audited: Mapping[str, tuple[ad.Table, ad.Table]],
    baselines: Mapping[str, ad.Table],
    now: datetime,
) -> list[str]:
    """Called after all selected audits, before the candidate is frozen and verified."""
    configured = ad.table(settings.get("adapters", {}), "Adapters")
    proofs: dict[tuple[str, str, str], bool] = {}
    plans: list[tuple[Document, bytes, bytes]] = []
    retired: list[str] = []
    for document in originals:
        allowed: dict[tuple[str, ...], set[int]] = {}
        for path, entries in lists(document).items():
            owner = path[-3] if len(path) >= 4 and path[-4] == "adapters" else None
            for index, raw in enumerate(entries):
                entry = ad.table(raw, "Exception")
                exception = ad.AgeException.decode(entry)
                provider, sep, name = ad.text(
                    entry.get("package"), "Exception package"
                ).partition(":")
                if not sep or not name:
                    raise ValueError("Exceptions require a provider:package identity")
                package = registry.package_name(provider, name)
                # Validate the entry even when its dependency has disappeared.
                registry.minimum_safe(provider, {"exceptions": [entry]}, package)
                scope = [
                    name
                    for name, spec in configured.items()
                    if (owner is None or owner == name)
                    and affects(ad.table(spec, "Adapter"), provider, package)
                ]
                if not scope or any(name not in audited for name in scope):
                    continue
                ready = True
                for name in scope:
                    spec, policy = audited[name]
                    if entry not in ad.array(
                        policy.get("exceptions", []), "Exceptions"
                    ):
                        ready = (
                            False  # A shadowed declaration has no audited authority.
                        )
                        break
                    proof_key = (name, provider, package)
                    if proof_key not in proofs:
                        proofs[proof_key] = mature_scope(
                            root, spec, baselines[name], policy, provider, package, now
                        )
                    ready = ready and proofs[proof_key]
                if ready:
                    allowed.setdefault(path, set()).add(index)
                    retired.append(
                        ad.text(entry["package"], "Exception package")
                        + "@"
                        + exception.version
                    )
        current = tc.regular_input(root, document.path)
        if not permitted(document, current, allowed):
            raise ValueError(
                "Candidate policy changed beyond proven security-exception retirement"
            )
        rendered = tomlkit.parse(current.decode())
        original_lists = lists(document)
        current_lists = lists(Document(document.path, current, document.prefix))
        for path, indices in allowed.items():
            # tomlkit retains unrelated formatting/comments. Keep an empty list to
            # avoid resurrecting an exception inherited from a lower-priority file.
            # permitted() already validated the nested table and array shapes.
            parent: MutableMapping[str, object] = rendered
            for key in path[:-1]:
                parent = cast(MutableMapping[str, object], parent[key])
            approved = [original_lists[path][i] for i in indices]
            items = cast(MutableSequence[object], parent.get(path[-1], tomlkit.array()))
            remaining = current_lists.get(path, [])
            for index in reversed(range(len(remaining))):
                if remaining[index] in approved:
                    del items[index]
            if not items:
                parent[path[-1]] = tomlkit.array()
        body = tomlkit.dumps(rendered).encode()
        if not allowed:
            body = current
        plans.append((document, current, body))
    # Validate every policy document before writing any of them.
    for document, old, body in plans:
        if tc.regular_input(root, document.path) != old:
            raise ValueError(
                "Dependency policy changed concurrently; edits are preserved"
            )
        if old != body:
            mode = stat.S_IMODE(tc.contained(root, document.path).stat().st_mode)
            tc.atomic_bytes(tc.contained(root, document.path), body, mode)
    if retired:
        print(
            "Retired security age exceptions: " + ", ".join(sorted(set(retired))),
            file=sys.stderr,
        )
    return retired
