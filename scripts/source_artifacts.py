"""Audit explicitly declared artifacts assembled by project preparation hooks."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypedDict

import adapter_data as ad
import manifests
import registry
import source_artifact
import toolchain as tc


class ArtifactIdentity(TypedDict):
    file: str
    pointer: list[str | int]
    url: str
    digest: str
    max_bytes: int


class Snapshot(TypedDict):
    entries: list[ArtifactIdentity]


def identities(root: Path, spec: Mapping[str, object]) -> list[ArtifactIdentity]:
    entries = spec.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Artifact adapters require a declared entry inventory")
    result: list[ArtifactIdentity] = []
    seen = set()
    for raw in entries:
        entry = ad.table(raw, "Artifact entry")
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("pointer"), list)
            or not entry.get("file")
        ):
            raise ValueError("Artifact entries require file and object pointer")
        path = tc.contained(root, ad.text(entry.get("file", ""), "Artifact file"))
        pointer = manifests.pin_pointer(entry["pointer"])
        key = (str(path.relative_to(root)), json.dumps(pointer))
        if key in seen:
            raise ValueError("Artifact inventory contains a duplicate source")
        seen.add(key)
        value = manifests.lookup(manifests.document(path)[0], pointer)
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("url"), str)
            or not value.get("url")
        ):
            raise ValueError("Artifact entry lacks a literal object URL")
        digest = ad.text(value.get("digest", value.get("hash", "")), "Artifact digest")
        if isinstance(digest, str) and digest.startswith("sha256-"):
            try:
                digest = "sha256:" + base64.b64decode(digest[7:], validate=True).hex()
            except ValueError:
                raise ValueError("Artifact SRI hash is malformed") from None
        registry.digest(digest)
        maximum = entry.get("max_bytes", source_artifact.MAX_BYTES)
        if (
            type(maximum) is not int
            or not 1 <= maximum <= source_artifact.MAX_DECLARED_BYTES
        ):
            raise ValueError("Declared artifact size limit is invalid")
        result.append(
            {
                "file": key[0],
                "pointer": pointer,
                "url": ad.text(value["url"], "Artifact URL"),
                "digest": digest,
                "max_bytes": maximum,
            }
        )
    return result


def snapshot(root: Path, spec: Mapping[str, object]) -> Snapshot:
    # A retained legacy URL may lack current public evidence; snapshot its identity
    # so an explicit refresh can fail without losing the user's existing pin.
    return {"entries": identities(root, spec)}


def resolve(
    root: Path, spec: Mapping[str, object], policy: ad.Table, now: datetime
) -> Snapshot:
    selected = snapshot(root, spec)
    audit(root, spec, {}, policy, now)
    return selected


def audit(
    root: Path,
    spec: Mapping[str, object],
    before: Mapping[str, object],
    policy: ad.Table,
    now: datetime,
) -> None:
    entries = identities(root, spec)
    if (
        before.get("resolution")
        and entries != ad.table(before["resolution"], "Artifact resolution")["entries"]
    ):
        raise ValueError("A project hook changed a selected artifact identity")
    for entry in entries:
        source_artifact.audit(
            entry["url"], entry["digest"], policy, now, max_bytes=entry["max_bytes"]
        )
