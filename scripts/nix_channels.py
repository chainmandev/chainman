"""Official Nix channel publications, independent of Git ancestry timestamps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import re
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import adapter_data as ad
import registry

PAGE_LIMIT = 32
RELEASE_LIMIT = 20_000
XML_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
RELEASES = "https://releases.nixos.org/"
LISTING = "https://nix-releases.s3.eu-west-1.amazonaws.com/"


@dataclass(frozen=True)
class Channel:
    prefix: str
    package: str


@dataclass(frozen=True)
class Publication:
    key: str
    published: datetime
    abbreviated_revision: str

    @property
    def url(self) -> str:
        return RELEASES + self.key + "/"


def channel(item: Mapping[str, object]) -> Channel | None:
    selection = item.get("selection", "git")
    if selection == "git":
        return None
    if selection != "published-channel":
        raise ValueError("Nix selection must be git or published-channel")
    if item.get("repository") != "NixOS/nixpkgs":
        raise ValueError("Published channels require the NixOS/nixpkgs repository")
    branch = ad.text(item.get("branch"), "Official Nix channel branch")
    if branch in {"nixos-unstable", "nixos-unstable-small"}:
        return Channel("nixos/" + branch.removeprefix("nixos-") + "/", "nixos")
    if branch == "nixpkgs-unstable":
        return Channel("nixpkgs/", "nixpkgs")
    stable = re.fullmatch(r"nixos-([0-9]{2}\.(?:05|11))(-small)?", branch)
    if stable:
        return Channel("nixos/" + stable[1] + (stable[2] or "") + "/", "nixos")
    darwin = re.fullmatch(r"nixpkgs-([0-9]{2}\.(?:05|11))-darwin", branch)
    if darwin:
        return Channel("nixpkgs/" + darwin[1] + "-darwin/", "nixpkgs-darwin")
    raise ValueError("Unsupported official Nix channel branch")


def publications(selected: Channel) -> list[Publication]:
    """Read the complete bounded official index, including older release series."""
    result: list[Publication] = []
    keys: set[str] = set()
    tokens: set[str] = set()
    token = ""
    pattern = re.compile(
        re.escape(selected.prefix + selected.package)
        + r"-[0-9][A-Za-z0-9.+-]*\.([a-f0-9]{7,40})"
    )
    for _ in range(PAGE_LIMIT):
        query: dict[str, str | int] = {
            "list-type": 2,
            "delimiter": "/",
            "prefix": selected.prefix,
            "max-keys": 1000,
        }
        if token:
            query["continuation-token"] = token
        body, _headers = registry.fetch(
            LISTING + "?" + urlencode(query), "application/xml", anonymous=True
        )
        if len(body) > 1024 * 1024 or b"<!DOCTYPE" in body.upper():
            raise ValueError("Invalid official Nix channel publication index")
        try:
            page = ET.fromstring(body)
        except ET.ParseError as exc:
            raise ValueError(
                "Malformed official Nix channel publication index"
            ) from exc
        if (
            page.tag != XML_NAMESPACE + "ListBucketResult"
            or page.findtext(XML_NAMESPACE + "Name") != "nix-releases"
            or page.findtext(XML_NAMESPACE + "Prefix") != selected.prefix
        ):
            raise ValueError("Official Nix channel index has a different scope")
        for entry in page.findall(XML_NAMESPACE + "Contents"):
            key = ad.text(entry.findtext(XML_NAMESPACE + "Key"), "Nix release key")
            match = pattern.fullmatch(key)
            if not match or key in keys:
                raise ValueError("Invalid or duplicate official Nix channel release")
            keys.add(key)
            result.append(
                Publication(
                    key,
                    registry.timestamp(
                        ad.text(
                            entry.findtext(XML_NAMESPACE + "LastModified"),
                            "Nix channel publication date",
                        )
                    ),
                    match[1],
                )
            )
            if len(result) > RELEASE_LIMIT:
                raise ValueError("Official Nix channel publication limit exceeded")
        truncated = page.findtext(XML_NAMESPACE + "IsTruncated")
        if truncated == "false":
            return sorted(result, key=lambda p: (p.published, p.key), reverse=True)
        token = ad.nonempty(
            page.findtext(XML_NAMESPACE + "NextContinuationToken"),
            "Nix channel continuation token",
        )
        if truncated != "true" or token in tokens or len(token) > 4096:
            raise ValueError("Invalid official Nix channel pagination")
        tokens.add(token)
    raise ValueError("Official Nix channel pagination limit exceeded")


def revision(publication: Publication) -> str:
    body, headers = registry.fetch(
        publication.url + "git-revision", "text/plain", anonymous=True
    )
    try:
        commit = body.decode("ascii").strip()
        modified = parsedate_to_datetime(
            next(v for k, v in headers.items() if k.lower() == "last-modified")
        )
    except (UnicodeError, ValueError, TypeError, StopIteration) as exc:
        raise ValueError(
            "Nix channel revision lacks valid publication evidence"
        ) from exc
    if (
        not re.fullmatch(r"[a-f0-9]{40}", commit)
        or not commit.startswith(publication.abbreviated_revision)
        or modified.tzinfo is None
        or modified > publication.published
    ):
        raise ValueError("Nix channel revision differs from its published release")
    return commit


def approved_publication(selected: Channel, commit: str) -> Publication:
    for publication in publications(selected):
        if (
            commit.startswith(publication.abbreviated_revision)
            and revision(publication) == commit
        ):
            return publication
    raise ValueError("Approved Nix revision has no official channel publication")
