"""Declared source updates with immutable identities and independently repeatable audits."""

from __future__ import annotations

import json
import re
import stat
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import quote, urlencode

import chainman
import registry
import toolchain as tc
import yaml
import adapter_data as ad
import manifests

ACTION_REF_LIMIT = 100_000


class SourceSelection(TypedDict):
    revision: str
    reason: str
    published: NotRequired[str]
    version: NotRequired[str]
    tag: NotRequired[str]


class SourceRevision(TypedDict):
    revision: str
    published: str


class ActionRecord(TypedDict):
    file: str
    action: str
    ordinal: int
    repository: str
    revision: str
    tracking: ad.Table


class ActionPlan(ActionRecord):
    selected: SourceSelection


class ActionsSnapshot(TypedDict):
    adapter: str
    files: list[str]
    records: list[ActionRecord]


class ImageRecord(TypedDict):
    repository: str
    tag: str
    digest: str
    versionSource: str


class NixSnapshot(TypedDict):
    adapter: str
    locks: dict[str, ad.Table]


def image_record(value: object) -> ImageRecord:
    entry = ad.table(value, "OCI image")
    if oci_tag(entry.get("tag")) is None:
        raise ValueError("OCI inventory requires explicit stable release tags")
    return {
        "repository": ad.text(entry["repository"], "OCI repository"),
        "tag": ad.text(entry["tag"], "OCI tag"),
        "digest": registry.digest(ad.text(entry.get("digest"), "OCI digest")),
        "versionSource": ad.text(
            entry.get("versionSource", "dockerHub"), "OCI version source"
        ),
    }


def pointer(spec: Mapping[str, object]) -> list[str | int]:
    result: list[str | int] = []
    for part in ad.array(spec.get("pointer", ["images"]), "OCI inventory pointer"):
        if not isinstance(part, (str, int)):
            raise ValueError("OCI pointer components must be names or array indexes")
        result.append(part)
    return result


def nix_nodes(lock: Mapping[str, object]) -> dict[str, ad.Table]:
    return {
        name: ad.table(value, "Nix lock node")
        for name, value in ad.table(lock["nodes"], "Nix lock nodes").items()
    }


def action_records(value: object) -> list[ActionRecord]:
    result: list[ActionRecord] = []
    for raw in ad.array(value, "Actions snapshot records"):
        record = ad.table(raw, "Actions snapshot record")
        ordinal = record["ordinal"]
        if type(ordinal) is not int or ordinal < 0:
            raise ValueError("Actions record requires a nonnegative occurrence ordinal")
        result.append(
            {
                "file": ad.text(record["file"], "Action file"),
                "action": ad.text(record["action"], "Action name"),
                "ordinal": ordinal,
                "repository": ad.text(record["repository"], "Action repository"),
                "revision": ad.text(record["revision"], "Action revision"),
                "tracking": ad.table(record["tracking"], "Action tracking policy"),
            }
        )
    return result


def object_pairs(pairs: list[tuple[str, object]]) -> ad.Table:
    result: ad.Table = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in dependency input")
        result[key] = value
    return result


def read_json(root: Path, name: str) -> object:
    return json.loads(tc.regular_input(root, name), object_pairs_hook=object_pairs)


def repository_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise ValueError("Expected an explicit public GitHub owner/repository")
    return value


def revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Expected an immutable GitHub commit identity")
    return value


def commit_time(repository: str, commit: str) -> datetime:
    repository_name(repository)
    revision(commit)
    item = ad.table(
        registry.data(f"https://api.github.com/repos/{repository}/commits/{commit}"),
        "GitHub commit",
    )
    if item.get("sha") != commit:
        raise ValueError("GitHub returned a different commit identity")
    details = ad.table(item["commit"], "GitHub commit details")
    committer = ad.table(details["committer"], "GitHub committer")
    return registry.timestamp(committer["date"])


def cutoff(policy: Mapping[str, object], now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Update time must include a timezone")
    return now - timedelta(days=registry.minimum_age(policy))


def nix_candidate(
    repository: str, branch: str, policy: Mapping[str, object], now: datetime
) -> SourceRevision:
    """Branch age is commit age; it is never presented as release publication age."""
    repository_name(repository)
    if not isinstance(branch, str) or not branch or any(c.isspace() for c in branch):
        raise ValueError("An explicit source branch is required")
    limit = cutoff(policy, now)
    query = urlencode({"sha": branch, "until": limit.isoformat(), "per_page": 1})
    entries = registry.data(
        f"https://api.github.com/repos/{repository}/commits?{query}"
    )
    if not isinstance(entries, list) or not entries:
        raise ValueError("No mature source revision with complete evidence")
    selected = revision(
        ad.text(ad.table(entries[0], "GitHub commit").get("sha"), "GitHub commit SHA")
    )
    published = commit_time(repository, selected)
    if published > limit:
        raise ValueError("Selected branch revision is younger than its age policy")
    return {"revision": selected, "published": published.isoformat()}


def action_version(tag: str) -> tuple[int, int, int] | None:
    if not isinstance(tag, str) or not re.fullmatch(r"v?\d+(?:\.\d+){0,2}", tag):
        return None
    parts = tuple(int(part) for part in tag.removeprefix("v").split("."))
    padded = parts + (0,) * (3 - len(parts))
    return padded[0], padded[1], padded[2]


def action_rank(tag: str) -> tuple[int, int, int]:
    rank = action_version(tag)
    if rank is None:
        raise ValueError("Actions ranking requires a release version")
    return rank


def action_refs(repository: str) -> dict[str, str]:
    """Read the complete v0 Git advertisement, including peeled tag identities."""
    repository_name(repository)
    media = "application/x-git-upload-pack-advertisement"
    body, headers = registry.fetch(
        f"https://github.com/{repository}.git/info/refs?service=git-upload-pack",
        media,
    )
    headers = {key.lower(): value for key, value in headers.items()}
    if (
        len(body) > registry.MAX_RESPONSE_BYTES
        or headers.get("content-type", "").split(";", 1)[0] != media
    ):
        raise ValueError("GitHub ref advertisement exceeds its bound or media type")
    position = 0

    def packet() -> bytes | None:
        nonlocal position
        size = body[position : position + 4]
        if not re.fullmatch(rb"[0-9a-f]{4}", size):
            raise ValueError("Incomplete GitHub ref advertisement framing")
        count = int(size, 16)
        if count == 0:
            position += 4
            return None
        if count < 4 or count > 65520 or position + count > len(body):
            raise ValueError("Invalid GitHub ref advertisement packet bound")
        value = body[position + 4 : position + count]
        position += count
        return value

    if packet() != b"# service=git-upload-pack\n" or packet() is not None:
        raise ValueError("Unsupported GitHub ref advertisement protocol")
    refs: dict[str, str] = {}
    previous: str | None = None
    while (value := packet()) is not None:
        if len(refs) >= ACTION_REF_LIMIT:
            raise ValueError("GitHub ref inventory exceeds its bound")
        value = value.removesuffix(b"\n")
        if previous is None:
            value, separator, capabilities = value.partition(b"\0")
            if (
                not separator
                or not re.fullmatch(rb"[\x21-\x7e]+(?: [\x21-\x7e]+)*", capabilities)
                or any(
                    item.startswith(b"object-format=") and item != b"object-format=sha1"
                    for item in capabilities.split()
                )
            ):
                raise ValueError("Unsupported GitHub ref identity capabilities")
        match = re.fullmatch(rb"([a-f0-9]{40}) (HEAD|refs/[^\x00-\x20\x7f]+)", value)
        if not match or match[1] == b"0" * 40:
            raise ValueError("Malformed GitHub advertised ref identity")
        commit, ref = (item.decode("utf-8") for item in match.groups())
        if ref in refs:
            raise ValueError("Duplicate GitHub advertised ref identity")
        if ref.endswith("^{}") and (
            previous is None
            or previous != ref[:-3]
            or not previous.startswith("refs/tags/")
            or previous.endswith("^{}")
        ):
            raise ValueError("GitHub peeled tag lacks its exact adjacent ref")
        refs[ref] = commit
        previous = ref
    if position != len(body) or previous is None:
        raise ValueError("Incomplete or trailing GitHub ref advertisement")
    result = {}
    for ref, commit in refs.items():
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if not re.fullmatch(r"v?\d+(?:\.\d+){0,2}", tag):
            continue
        if len(tag) > 128:
            raise ValueError("GitHub release tag exceeds its bounded identity")
        result[tag] = refs.get(ref + "^{}", commit)
    return result


def action_release(
    item: object, expected: str | None = None
) -> registry.Release | None:
    if not isinstance(item, dict):
        raise ValueError("Malformed GitHub release metadata")  # noqa: TRY004
    tag = item.get("tag_name")
    if (
        not isinstance(tag, str)
        or len(tag) > 128
        or (expected is not None and tag != expected)
        or type(item.get("draft")) is not bool
        or type(item.get("prerelease")) is not bool
    ):
        raise ValueError("Malformed or mismatched GitHub release identity")
    rank = action_version(tag)
    if item["draft"] or item["prerelease"] or rank is None:
        return None
    return registry.Release(
        ".".join(map(str, rank)), registry.timestamp(item.get("published_at")), tag
    )


def action_release_batch(repository: str) -> dict[str, registry.Release | None]:
    # A batch saves requests for young releases. It is never the candidate inventory:
    # GitHub caps this API at 1,000 results, regardless of pagination parameters.
    entries = registry.data(
        f"https://api.github.com/repos/{repository}/releases?per_page=100&page=1"
    )
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Malformed GitHub release metadata batch")
    result: dict[str, registry.Release | None] = {}
    for item in entries:
        release = action_release(item)
        tag = ad.text(ad.table(item, "GitHub release")["tag_name"], "Release tag")
        if tag in result:
            raise ValueError("Duplicate GitHub release metadata identity")
        result[tag] = release
    return result


def action_release_by_tag(repository: str, tag: str) -> registry.Release | None:
    try:
        item = registry.data(
            f"https://api.github.com/repos/{repository}/releases/tags/{quote(tag, safe='')}"
        )
    except registry.RegistryHTTPError as exc:
        if exc.status != 404:
            raise
        # An advertised Git tag without a published GitHub release is not a release.
        return None
    return action_release(item, tag)


def select_action(
    repository: str,
    current: str,
    tracking: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
    *,
    advance_major: bool = True,
) -> SourceSelection:
    repository_name(repository)
    revision(current)
    if tracking["kind"] == "pin":
        return {"revision": current, "reason": "explicit immutable pin"}
    if tracking["kind"] == "channel":
        selected = nix_candidate(
            repository,
            ad.text(tracking["channel"], "Action tracking channel"),
            policy,
            now,
        )
        if commit_time(repository, current) >= registry.timestamp(
            selected["published"]
        ):
            return {"revision": current, "reason": "retained newer current revision"}
        return {
            "revision": selected["revision"],
            "published": selected["published"],
            "reason": "mature channel revision",
        }
    if tracking["kind"] != "release":
        raise ValueError("Unknown Actions tracking policy")
    major = tracking.get("major")
    if major is not None and type(major) is not int:
        raise ValueError("Actions tracking major must be an integer")
    bound = registry.constraint("github", policy, repository)
    safe = registry.minimum_safe("github", policy, repository)
    limit = cutoff(policy, now)
    exception_versions = {
        ad.text(item["version"], "Exception version")
        for raw in ad.array(policy.get("exceptions", []), "Version exceptions")
        for item in [ad.table(raw, "Version exception")]
        if item.get("package") == f"github:{repository}"
    }
    refs = action_refs(repository)
    groups: dict[tuple[int, int, int], list[str]] = {}
    for tag in refs:
        rank = action_rank(tag)
        version = ".".join(map(str, rank))
        if (
            (not advance_major and major is not None and rank[0] != major)
            or (safe is not None and registry.stable_version("github", version) < safe)
            or not registry.compatible("github", version, bound)
        ):
            continue
        groups.setdefault(rank, []).append(tag)
    batch = action_release_batch(repository)
    if any(item is not None and tag not in refs for tag, item in batch.items()):
        raise ValueError("Published Actions release lacks its advertised immutable tag")

    def publication(tag: str) -> registry.Release | None:
        if tag not in batch:
            batch[tag] = action_release_by_tag(repository, tag)
        return batch[tag]

    releases = []
    for rank in sorted(groups, reverse=True):
        group = []
        for tag in sorted(groups[rank]):
            candidate = publication(tag)
            if candidate is not None:
                group.append(candidate)
        if not group:
            continue
        # Validate every alias before excluding a version published during this
        # transaction. Neither an older alias nor a security exception can admit it.
        if max(item.published for item in group) > now:
            continue
        if (
            max(item.published for item in group) > limit
            and group[0].version not in exception_versions
        ):
            continue
        # Bind every alias of this version before maturity or exception retirement.
        # A young publication cannot mature by consulting its older commit.
        published = max(
            max(item.published, commit_time(repository, refs[item.identity]))
            for item in group
        )
        group = [
            registry.Release(
                item.version,
                published,
                item.identity,
            )
            for item in group
        ]
        if any(item.published > now for item in group):
            raise ValueError("Actions selected commit has future age evidence")
        releases.extend(group)
        if registry.maturity("github", group, policy, repository, now):
            # Lower versions cannot outrank this mature group or affect retirement.
            break
    chosen = max(
        registry.eligible("github", releases, policy, repository, now),
        key=lambda item: action_rank(item.version),
    )
    commit = refs[chosen.identity]
    tag = chosen.identity
    rank = action_rank(chosen.version)
    old_version = tracking.get("version")
    old_rank: tuple[int, ...] | None = None
    if old_version is not None:
        components = ad.array(old_version, "Actions tracking version")
        integers: list[int] = []
        for component in components:
            if type(component) is not int:
                raise ValueError("Actions tracking version components must be integers")
            integers.append(component)
        old_rank = tuple(integers)
    retain = (
        (major is not None and rank[0] < major)
        or (old_rank is not None and rank < tuple(old_rank))
        or (
            (major is None or rank[0] == major)
            and commit != current
            and commit_time(repository, current) >= commit_time(repository, commit)
        )
    )
    major_hold = not advance_major and major is not None
    if retain and (bound or safe is not None or major_hold):
        # Baseline age may be retained, but dates and annotations cannot waive an
        # operative version limit. Use the highest published identity of this SHA;
        # a lower alias must not hide a current major/floor violation.
        current_groups: dict[tuple[int, int, int], list[str]] = {}
        for name, ref_commit in refs.items():
            if ref_commit == current:
                current_groups.setdefault(action_rank(name), []).append(name)
        current_version = None
        for current_rank in sorted(current_groups, reverse=True):
            observations = [publication(name) for name in current_groups[current_rank]]
            values = [item for item in observations if item is not None]
            if values:
                if (
                    max(
                        commit_time(repository, current),
                        *(item.published for item in values),
                    )
                    > now
                ):
                    raise ValueError("Actions current release has future age evidence")
                current_version = values[0].version
                break
        if current_version is None:
            raise ValueError("Actions current immutable version evidence is missing")
        retain = (
            (safe is None or registry.stable_version("github", current_version) >= safe)
            and registry.compatible("github", current_version, bound)
            and (not major_hold or action_rank(current_version)[0] == major)
        )
    if retain:
        return {"revision": current, "reason": "retained newer current release"}
    return {
        "revision": commit,
        "tag": tag,
        "version": chosen.version,
        "published": chosen.published.isoformat(),
        "reason": "eligible stable release",
    }


ACTION = re.compile(
    r"(?m)^(?P<prefix>[ \t]*(?:-[ \t]+)?uses:[ \t]+)"
    r"(?P<quote>['\"]?)(?P<action>[^\s#'\"]+)@(?P<revision>[^\s#'\"]+)"
    r"(?P=quote)(?P<suffix>[^\r\n]*)$"
)


def action_tracking(suffix: str) -> ad.Table:
    if "deps-update:" in suffix:
        text = suffix.split("deps-update:", 1)[1].strip()
        tokens = text.split()
        if "pin" in tokens:
            if any(t.startswith(("channel=", "release-major=")) for t in tokens):
                raise ValueError("Conflicting Actions pin and tracking declarations")
            return {"kind": "pin"}
        major = re.search(r"(?:^|\s)release-major=v(\d+)(?:$|\s)", text)
        channel = re.search(r"(?:^|\s)channel=([^\s]+)", text)
        if major and channel:
            raise ValueError("Conflicting Actions release and channel declarations")
        if major:
            return {"kind": "release", "major": int(major[1])}
        if channel:
            return {"kind": "channel", "channel": channel[1]}
        if re.fullmatch(r"tool=[^\s]+", text):
            return {"kind": "release"}
        raise ValueError(
            "Unsupported Actions annotation; declare release, channel, or pin"
        )
    version = re.fullmatch(r"[ \t]*#[ \t]*(v?\d+(?:\.\d+){0,2})[ \t]*", suffix)
    if version:
        return {
            "kind": "release",
            "major": action_rank(version[1])[0],
            "version": list(action_rank(version[1])),
        }
    return {"kind": "pin"}


def action_files(root: Path, spec: Mapping[str, object]) -> list[str]:
    declared = spec.get("files")
    if not isinstance(declared, list) or not declared:
        raise ValueError("Actions requires declared workflow files or contained globs")
    result = set()
    for pattern in declared:
        if not isinstance(pattern, str):
            raise ValueError("Workflow paths must be strings")  # noqa: TRY004 - decoded external data
        tc.contained(root, pattern)
        matches = (
            sorted(root.glob(pattern))
            if any(c in pattern for c in "*?[")
            else [root / pattern]
        )
        for path in matches:
            name = path.relative_to(root).as_posix()
            tc.regular_input(root, name)
            result.add(name)
    if not result:
        raise ValueError("Declared workflow patterns have no inputs")
    return sorted(result)


def actions_snapshot(root: Path, spec: Mapping[str, object]) -> ActionsSnapshot:
    records: list[ActionRecord] = []
    files = action_files(root, spec)
    for name in files:
        source = tc.regular_input(root, name).decode()

        def external_uses(value: object) -> Iterator[str]:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "uses":
                        if not isinstance(item, str):
                            raise ValueError(
                                "Actions uses values must be literal strings"
                            )
                        if not item.startswith(("./", "docker://")):
                            yield item
                    else:
                        yield from external_uses(item)
            elif isinstance(value, list):
                for item in value:
                    yield from external_uses(item)

        parsed = list(external_uses(yaml.safe_load(source)))
        matched = [
            m["action"] + "@" + m["revision"]
            for m in ACTION.finditer(source)
            if not m["action"].startswith(("./", "docker://"))
        ]
        if sorted(parsed) != sorted(matched):
            raise ValueError(
                "External Actions uses entries must be standalone literal lines"
            )
        ordinals: dict[str, int] = {}
        for match in ACTION.finditer(source):
            action = match["action"]
            if action.startswith(("./", "docker://")):
                continue
            parts = action.split("/")
            if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
                raise ValueError("Unsupported Actions repository or subpath")
            repository = repository_name("/".join(parts[:2]))
            ordinal = ordinals.get(action, 0)
            ordinals[action] = ordinal + 1
            records.append(
                {
                    "file": name,
                    "action": action,
                    "ordinal": ordinal,
                    "repository": repository,
                    "revision": revision(match["revision"]),
                    "tracking": action_tracking(match["suffix"]),
                }
            )
    return {"adapter": "actions", "files": files, "records": records}


def action_plan(
    before: Mapping[str, object],
    spec: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> list[ActionPlan]:
    advance_major = spec.get("advance_major", True)
    if type(advance_major) is not bool:
        raise ValueError("advance_major must be boolean")
    return [
        {
            **record,
            "selected": select_action(
                record["repository"],
                record["revision"],
                record["tracking"],
                policy,
                now,
                advance_major=advance_major
                and spec.get("mode", "aggressive") != "compatible",
            ),
        }
        for record in action_records(before["records"])
    ]


def write_planned(root: Path, plans: list[tuple[str, bytes, bytes]]) -> list[str]:
    for name, old, _ in plans:
        if tc.regular_input(root, name) != old:
            raise ValueError(
                "Dependency input changed concurrently; changes are preserved"
            )
    changed = []
    for name, old, new in plans:
        if old != new:
            path = tc.contained(root, name)
            tc.atomic_bytes(path, new, stat.S_IMODE(path.stat().st_mode))
            changed.append(name)
    return changed


def resolve_actions(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object], now: datetime
) -> ad.Table:
    before = actions_snapshot(root, spec)
    decisions = action_plan(before, spec, policy, now)
    planned = []
    for name in before["files"]:
        old = tc.regular_input(root, name)
        by_key = {
            (d["action"], d["ordinal"]): d for d in decisions if d["file"] == name
        }
        counts: dict[str, int] = {}

        def replace(
            match: re.Match[str],
            counts: dict[str, int] = counts,
            by_key: Mapping[tuple[str, int], ActionPlan] = by_key,
        ) -> str:
            action = match["action"]
            if action.startswith(("./", "docker://")):
                return match[0]
            ordinal = counts.get(action, 0)
            counts[action] = ordinal + 1
            selected = by_key[action, ordinal]["selected"]
            suffix = match["suffix"]
            if "version" in selected:
                major = action_rank(selected["version"])[0]
                if "deps-update:" in suffix:
                    suffix = re.sub(
                        r"release-major=v\d+", f"release-major=v{major}", suffix
                    )
                else:
                    suffix = re.sub(
                        r"(?<=#)\s*v?\d+(?:\.\d+){0,2}\s*$",
                        " v" + selected["version"],
                        suffix,
                    )
            return (
                match["prefix"]
                + match["quote"]
                + action
                + "@"
                + selected["revision"]
                + match["quote"]
                + suffix
            )

        planned.append((name, old, ACTION.sub(replace, old.decode()).encode()))
    return {"changed": write_planned(root, planned), "selected": decisions}


def oci_tag(tag: object) -> tuple[str, int, str] | None:
    if not isinstance(tag, str):
        return None
    match = re.fullmatch(r"v?(\d+(?:\.\d+){0,2})(-[A-Za-z0-9][A-Za-z0-9._-]*)?", tag)
    if not match or re.search(
        r"(?:^|[._-])(?:alpha|beta|rc|pre|preview|nightly|dev|canary|snapshot)\d*(?:[._-]|$)",
        match[2] or "",
    ):
        return None
    parts = match[1].split(".")
    return (".".join(parts + ["0"] * (3 - len(parts))), len(parts), match[2] or "")


def oci_candidates(repository: str, source: str) -> list[registry.Release]:
    result = []
    if source == "dockerHub":
        return registry.docker_releases(
            repository, lambda tag: oci_tag(tag) is not None
        )
    if source == "gcr":
        if (
            not isinstance(repository, str)
            or not re.fullmatch(r"(?:[a-z]+\.)?gcr\.io/[A-Za-z0-9_./-]+", repository)
            or any(p in ("", ".", "..") for p in repository.split("/"))
        ):
            raise ValueError("GCR requires an explicit public registry/repository")
        host, name = repository.split("/", 1)
        body = ad.table(
            registry.data(f"https://{host}/v2/{name}/tags/list"), "GCR inventory"
        )
        if not isinstance(body.get("manifest"), dict):
            raise ValueError("GCR lacks manifest-bound publication evidence")
        seen = set()
        for identity, raw_entry in ad.table(body["manifest"], "GCR manifests").items():
            entry = ad.table(raw_entry, "GCR manifest")
            identity = registry.digest(identity)
            raw = entry.get("timeUploadedMs")
            if (
                not isinstance(raw, (str, int))
                or isinstance(raw, bool)
                or not re.fullmatch(r"\d+", str(raw))
            ):
                raise ValueError("GCR manifest lacks its upload time")
            at = datetime.fromtimestamp(int(raw) / 1000, timezone.utc)
            for tag in ad.strings(entry.get("tag", []), "GCR manifest tags"):
                if oci_tag(tag) is None:
                    continue
                if tag in seen:
                    raise ValueError("GCR tag has conflicting immutable identities")
                seen.add(tag)
                result.append(registry.Release(tag, at, identity))
        return result
    raise ValueError("Unsupported OCI age-evidence source")


def oci_inventory(root: Path, spec: Mapping[str, object]) -> dict[str, ImageRecord]:
    value = manifests.lookup(
        read_json(root, ad.text(spec["file"], "OCI inventory file")), pointer(spec)
    )
    if not isinstance(value, dict) or not value:
        raise ValueError("OCI inventory must be a nonempty named image mapping")
    inventory = ad.table(value, "OCI inventory")
    names = ad.strings(spec.get("names", list(inventory)), "OCI selected names")
    if (
        not isinstance(names, list)
        or not names
        or len(set(names)) != len(names)
        or set(names) - inventory.keys()
    ):
        raise ValueError("OCI names must select distinct declared inventory entries")
    return {name: image_record(inventory[name]) for name in names}


def select_oci(
    current: Mapping[str, object],
    spec: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> ad.Table:
    parsed = oci_tag(current["tag"])
    assert parsed is not None  # Inventory has already parsed this tag.
    repository = ad.text(current["repository"], "OCI repository")
    candidates = oci_candidates(
        repository, ad.text(current["versionSource"], "OCI version source")
    )
    eligible = []
    for item in candidates:
        tag = oci_tag(item.version)
        assert tag is not None  # Candidate discovery includes only parsed tags.
        if tag[1:] != parsed[1:]:
            continue
        if (
            spec.get("mode", "aggressive") == "compatible"
            and action_rank(tag[0])[: min(parsed[1], 2)]
            != action_rank(parsed[0])[: min(parsed[1], 2)]
        ):
            continue
        eligible.append(
            registry.Release(tag[0], item.published, item.version + "@" + item.identity)
        )
    if spec.get("mode", "aggressive") not in ("aggressive", "compatible"):
        raise ValueError("OCI mode must be aggressive or compatible")
    chosen = registry.select("docker", eligible, policy, repository, now)
    result: ad.Table = {
        **current,
        "reason": "retained newer immutable image",
    }
    if registry.stable_version("docker", chosen.version) < registry.stable_version(
        "docker", parsed[0]
    ):
        return result
    selected_tag, digest = chosen.identity.rsplit("@", 1)
    result["tag"] = selected_tag
    result["digest"] = registry.digest(digest)
    result["published"] = chosen.published.isoformat()
    result["reason"] = "eligible manifest-bound image"
    return result


def resolve_oci(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object], now: datetime
) -> ad.Table:
    before = oci_inventory(root, spec)
    decisions = {
        name: select_oci(value, spec, policy, now) for name, value in before.items()
    }
    file = ad.text(spec["file"], "OCI inventory file")
    old = tc.regular_input(root, file)
    document: object = json.loads(old, object_pairs_hook=object_pairs)
    inventory_pointer = pointer(spec)
    for name, selected in decisions.items():
        manifests.assign(document, [*inventory_pointer, name, "tag"], selected["tag"])
        manifests.assign(
            document, [*inventory_pointer, name, "digest"], selected["digest"]
        )
    new = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    if all(
        selected["tag"] == before[name]["tag"]
        and selected["digest"] == before[name]["digest"]
        for name, selected in decisions.items()
    ):
        new = old
    return {
        "changed": write_planned(root, [(file, old, new)]),
        "selected": decisions,
    }


def nix_specs(root: Path, spec: Mapping[str, object]) -> list[ad.Table]:
    entries = spec.get("inputs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Nix updates require explicitly declared inputs")
    seen: set[tuple[str, str]] = set()
    result: list[ad.Table] = []
    for raw in entries:
        item = ad.table(raw, "Nix input")
        directory = ad.text(item.get("directory", "."), "Nix input directory")
        tc.regular_input(root, str(Path(directory) / "flake.nix"))
        repository_name(ad.text(item.get("repository"), "Nix repository"))
        name = item.get("input")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", name
        ):
            raise ValueError("Nix requires an exact input name or nested input path")
        key = (directory, name)
        if key in seen:
            raise ValueError("Duplicate Nix update target")
        seen.add(key)
        result.append(item)
    return result


def nix_node(lock: Mapping[str, object], name: str) -> str:
    nodes = nix_nodes(lock)

    def follow(path: Sequence[str], active: set[tuple[str, ...]]) -> str:
        key = ad.text(lock["root"], "Nix root node")
        for part in path:
            ref = ad.table(nodes[key]["inputs"], "Nix node inputs")[part]
            if isinstance(ref, list):
                token = tuple(ad.strings(ref, "Nix follows path"))
                if token in active:
                    raise ValueError("Nix follows cycle")
                key = follow(token, active | {token})
            elif isinstance(ref, str):
                key = ref
            else:
                raise ValueError("Malformed Nix input reference")  # noqa: TRY004 - decoded external data
        return key

    return follow(name.split("/"), set())


def nix_snapshot(root: Path, spec: Mapping[str, object]) -> NixSnapshot:
    inputs = nix_specs(root, spec)
    paths = dict.fromkeys(
        str(
            Path(ad.text(item.get("directory", "."), "Nix input directory"))
            / "flake.lock"
        )
        for item in inputs
    )
    locks = {path: ad.table(read_json(root, path), "Nix lock") for path in paths}
    return {"adapter": "nix", "locks": locks}


def nix_plan(
    before: Mapping[str, object],
    spec: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> list[ad.Table]:
    plans: list[ad.Table] = []
    locks = ad.table(before["locks"], "Nix snapshot locks")
    for raw in ad.array(spec["inputs"], "Nix inputs"):
        item = ad.table(raw, "Nix input")
        path = str(
            Path(ad.text(item.get("directory", "."), "Nix input directory"))
            / "flake.lock"
        )
        lock = ad.table(locks[path], "Nix lock")
        node = nix_node(lock, ad.text(item["input"], "Nix input name"))
        current = ad.table(nix_nodes(lock)[node]["locked"], "Nix locked source")
        repository = ad.text(item["repository"], "Nix repository")
        if (
            current.get("type") != "github"
            or ad.text(current.get("owner", ""), "Nix owner")
            + "/"
            + ad.text(current.get("repo", ""), "Nix repository")
            != repository
        ):
            raise ValueError("Nix input differs from its declared source repository")
        selected = nix_candidate(
            repository, ad.text(item["branch"], "Nix branch"), policy, now
        )
        current_revision = ad.text(current.get("rev"), "Nix current revision")
        at = commit_time(repository, current_revision)
        if at >= registry.timestamp(selected["published"]):
            selected = {"revision": current_revision, "published": at.isoformat()}
        if any(
            p["file"] == path
            and p["node"] == node
            and p["revision"] != selected["revision"]
            for p in plans
        ):
            raise ValueError("Nix aliases select conflicting revisions for one source")
        plans.append({**item, "file": path, "node": node, **selected})
    return plans


def resolve_nix(
    root: Path, spec: Mapping[str, object], policy: Mapping[str, object], now: datetime
) -> ad.Table:
    before = nix_snapshot(root, spec)
    plans = nix_plan(before, spec, policy, now)
    changed = []
    # One invocation per flake keeps coordinated overrides in the same lock operation.
    for file in before["locks"]:
        selected = [p for p in plans if p["file"] == file]
        nodes = nix_nodes(before["locks"][file])
        if all(
            ad.table(
                nodes[ad.text(p["node"], "Nix planned node")]["locked"],
                "Nix locked source",
            )["rev"]
            == p["revision"]
            for p in selected
        ):
            continue
        argv = [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "flake",
            "lock",
            "path:.",
        ]
        for item in selected:
            argv.extend(
                [
                    "--override-input",
                    ad.text(item["input"], "Nix input name"),
                    f"github:{item['repository']}/{item['revision']}",
                ]
            )
        chainman.execute(
            root,
            ad.text(spec.get("profile", "core"), "Nix profile"),
            argv,
            env=tc.environment(root),
            cwd=tc.contained(root, str(Path(file).parent)),
        )
        changed.append(file)
    audit_nix(root, spec, before, policy, now)
    return {"changed": changed, "selected": plans}


def nix_tree(
    root: Path, spec: Mapping[str, object], repository: str, commit: str
) -> dict[str, str]:
    repository_name(repository)
    revision(commit)
    owner, repo = repository.split("/")
    # All inserted values have a restricted literal alphabet; no Nix string
    # interpolation or project expression is accepted by this evidence query.
    expression = (
        'let tree = builtins.fetchTree { type = "github"; owner = "'
        + owner
        + '"; repo = "'
        + repo
        + '"; rev = "'
        + commit
        + '"; }; in { inherit (tree) narHash; }'
    )
    result = chainman.execute(
        root,
        ad.text(spec.get("profile", "core"), "Nix profile"),
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "eval",
            "--impure",
            "--json",
            "--expr",
            expression,
        ],
        env=tc.environment(root),
        text=True,
        stdout=subprocess.PIPE,
    )
    evidence = json.loads(result.stdout)
    if (
        not isinstance(evidence, dict)
        or set(evidence) != {"narHash"}
        or not isinstance(evidence["narHash"], str)
        or not re.fullmatch(r"sha256-[A-Za-z0-9+/]{43}=", evidence["narHash"])
    ):
        raise ValueError("Nix source evidence lacks one SHA-256 content hash")
    return {"narHash": evidence["narHash"]}


def audit_nix(
    root: Path,
    spec: Mapping[str, object],
    before: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> None:
    plans = nix_plan(before, spec, policy, now)
    current = nix_snapshot(root, spec)
    for file, raw_old in ad.table(before["locks"], "Nix snapshot locks").items():
        old = ad.table(raw_old, "Original Nix lock")
        new = current["locks"][file]
        expected = {
            ad.text(p["node"], "Nix planned node"): p
            for p in plans
            if p["file"] == file
        }
        old_nodes, new_nodes = nix_nodes(old), nix_nodes(new)
        if new.get("root") != old.get("root") or new_nodes.keys() != old_nodes.keys():
            raise ValueError("Nix changed undeclared lock structure")
        for key, node in old_nodes.items():
            if key not in expected:
                if node != new_nodes[key]:
                    raise ValueError("Nix changed an undeclared input")
                continue
            plan = expected[key]
            selected_node = new_nodes[key]
            selected = ad.table(selected_node["locked"], "Nix selected source")
            owner, repository = ad.text(plan["repository"], "Nix repository").split("/")
            canonical_original = {
                "type": "github",
                "owner": owner,
                "repo": repository,
                "rev": plan["revision"],
            }
            if selected_node.get("original") not in (
                node.get("original"),
                canonical_original,
            ):
                raise ValueError(
                    "Nix changed the input's declared source beyond its exact selected revision"
                )
            if {
                k: value
                for k, value in selected_node.items()
                if k not in ("locked", "original")
            } != {
                k: value for k, value in node.items() if k not in ("locked", "original")
            }:
                raise ValueError(
                    "Nix changed the selected input's dependency structure"
                )
            allowed_locked = {"rev", "lastModified", "narHash"}
            if {k: v for k, v in selected.items() if k not in allowed_locked} != {
                k: v
                for k, v in ad.table(node["locked"], "Nix original source").items()
                if k not in allowed_locked
            }:
                raise ValueError("Nix changed undeclared source attributes")
            if (
                nix_node(new, ad.text(plan["input"], "Nix input name")) != key
                or selected.get("rev") != plan["revision"]
                or selected.get("type") != "github"
                or ad.text(selected.get("owner", ""), "Nix owner")
                + "/"
                + ad.text(selected.get("repo", ""), "Nix repository")
                != plan["repository"]
            ):
                raise ValueError(
                    "Nix final input differs from its selected immutable source"
                )
            if selected != node["locked"]:
                if selected.get("lastModified") != int(
                    registry.timestamp(plan["published"]).timestamp()
                ) or not re.fullmatch(
                    r"sha256-[A-Za-z0-9+/]{43}=",
                    ad.text(selected.get("narHash", ""), "Nix content hash"),
                ):
                    raise ValueError(
                        "Nix input lacks consistent timestamp/hash evidence"
                    )
                tree = nix_tree(
                    root,
                    spec,
                    ad.text(plan["repository"], "Nix repository"),
                    ad.text(plan["revision"], "Nix revision"),
                )
                if tree.get("narHash") != selected["narHash"]:
                    raise ValueError(
                        "Nix input content hash differs from its immutable source"
                    )


def snapshot(root: Path, spec: Mapping[str, object]) -> Mapping[str, object]:
    adapter = spec.get("adapter")
    if adapter == "actions":
        return actions_snapshot(root, spec)
    if adapter == "oci":
        return {"adapter": "oci", "images": oci_inventory(root, spec)}
    if adapter == "nix":
        return nix_snapshot(root, spec)
    if adapter == "go":
        import source_go

        return source_go.snapshot(root, spec)
    if adapter == "toolchain":
        import source_toolchain

        return source_toolchain.snapshot(root, spec)
    raise ValueError("Unsupported source update adapter")


def resolve(
    root: Path,
    spec: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
    *,
    before: ad.Table | None = None,
) -> Mapping[str, object]:
    cutoff(policy, now)
    adapter = spec.get("adapter")
    if adapter == "actions":
        return resolve_actions(root, spec, policy, now)
    if adapter == "oci":
        return resolve_oci(root, spec, policy, now)
    if adapter == "nix":
        return resolve_nix(root, spec, policy, now)
    if adapter == "go":
        import source_go

        return source_go.resolve(root, spec, policy, now)
    if adapter == "toolchain":
        import source_toolchain

        return source_toolchain.resolve(root, spec, dict(policy), now, before=before)
    raise ValueError("Unsupported source update adapter")


def audit(
    root: Path,
    spec: Mapping[str, object],
    before: Mapping[str, object],
    policy: Mapping[str, object],
    now: datetime,
) -> None:
    if before.get("adapter") != spec.get("adapter"):
        raise ValueError("Source audit snapshot belongs to a different adapter")
    cutoff(policy, now)
    if spec["adapter"] == "actions":
        actual = actions_snapshot(root, spec)
        expected = action_plan(before, spec, policy, now)
        if actual["files"] != before["files"] or len(actual["records"]) != len(
            expected
        ):
            raise ValueError("Actions target inventory changed during reconciliation")
        for current, planned in zip(actual["records"], expected, strict=True):
            current_fields: Mapping[str, object] = current
            planned_fields: Mapping[str, object] = planned
            if (
                any(
                    current_fields[k] != planned_fields[k]
                    for k in ("file", "action", "ordinal", "repository")
                )
                or current["revision"] != planned["selected"]["revision"]
            ):
                raise ValueError(
                    "Actions final identity differs from its verified selection"
                )
            old_tracking, new_tracking = planned["tracking"], current["tracking"]
            expected_tracking = dict(old_tracking)
            if "version" in planned["selected"]:
                version = list(action_rank(planned["selected"]["version"]))
                if "major" in expected_tracking:
                    expected_tracking["major"] = version[0]
                if "version" in expected_tracking:
                    expected_tracking["version"] = version
            if expected_tracking != new_tracking:
                raise ValueError(
                    "Actions tracking policy changed during reconciliation"
                )
    elif spec["adapter"] == "oci":
        actual_images = oci_inventory(root, spec)
        expected_images = {
            name: select_oci(image_record(value), spec, policy, now)
            for name, value in ad.table(before["images"], "OCI snapshot images").items()
        }
        if actual_images.keys() != expected_images.keys() or any(
            {k: v for k, v in value.items() if k in actual_images[name]}
            != actual_images[name]
            for name, value in expected_images.items()
        ):
            raise ValueError("OCI final identity differs from its verified selection")
    elif spec["adapter"] == "nix":
        audit_nix(root, spec, before, policy, now)
    elif spec["adapter"] == "go":
        import source_go

        source_go.audit(root, spec, before, policy, now)
    elif spec["adapter"] == "toolchain":
        import source_toolchain

        source_toolchain.audit(root, spec, before, policy, now)
    else:
        raise ValueError("Unsupported source update adapter")
