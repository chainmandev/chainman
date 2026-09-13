"""Dated GitHub releases whose public tags wrap a stable numeric version."""

import re
from collections.abc import Mapping
from datetime import datetime
from typing import TypedDict

import adapter_data as ad
import registry
import source_updates

VERSION_FORMS = {
    r"\d+\.\d+\.\d+",
    r"[0-9]+\.[0-9]+\.[0-9]+",
    r"[0-9]+(?:\.[0-9]+){1,2}",
    r"\d+\.\d+(?:\.\d+)?",
}


class Selection(TypedDict):
    release: registry.Release
    tag: str


def pattern(value: str) -> re.Pattern[str]:
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("GitHub tag pattern must be a bounded string")
    match = re.fullmatch(
        r"\^([A-Za-z0-9._/-]*)\(\?P<version>(.*)\)([A-Za-z0-9._/-]*)\$", value
    )
    if not match or match[2] not in VERSION_FORMS:
        raise ValueError(
            "Tag pattern requires anchored literal affixes and a supported numeric version group"
        )
    return re.compile(
        re.escape(match[1]) + "(?P<version>" + match[2] + ")" + re.escape(match[3])
    )


def releases(repository: str, tag_pattern: str) -> list[registry.Release]:
    source_updates.repository_name(repository)
    expression = pattern(tag_pattern)
    result = []
    for page in range(1, 101):
        values = registry.data(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        )
        if not isinstance(values, list):
            raise ValueError("Malformed GitHub release inventory")  # noqa: TRY004
        for raw in values:
            value = ad.table(raw, "GitHub release")
            tag = value.get("tag_name")
            if not isinstance(tag, str) or len(tag) > 128:
                raise ValueError("GitHub release tag exceeds its bounded identity")
            match = expression.fullmatch(tag)
            if value.get("draft") or value.get("prerelease") or not match:
                continue
            version = match["version"]
            if version.count(".") == 1:
                version += ".0"
            if registry.version("github", version) is None:
                raise ValueError("GitHub tag does not identify one stable version")
            result.append(
                registry.Release(
                    version, registry.timestamp(value.get("published_at")), tag
                )
            )
        if len(values) < 100:
            return result
    raise ValueError("GitHub release inventory exceeds its pagination bound")


def bind(repository: str, release: registry.Release) -> registry.Release:
    commit = registry.github_commit(repository, release.identity)
    published = max(release.published, source_updates.commit_time(repository, commit))
    return registry.Release(release.version, published, commit)


def select(
    repository: str,
    tag_pattern: str,
    policy: Mapping[str, object],
    now: datetime,
    *,
    values: list[registry.Release] | None = None,
) -> Selection:
    values = releases(repository, tag_pattern) if values is None else values
    eligible = sorted(
        registry.eligible("github", values, policy, repository, now),
        key=lambda release: registry.stable_version("github", release.version),
        reverse=True,
    )
    for candidate in eligible:
        chosen = bind(repository, candidate)
        if chosen.published > now:
            raise ValueError("GitHub tag points to future-dated contents")
        rebound = [chosen if value is candidate else value for value in values]
        if chosen in registry.eligible("github", rebound, policy, repository, now):
            return {"release": chosen, "tag": candidate.identity}
    raise ValueError("No eligible GitHub release with mature immutable contents")


def metadata(repository: str, tag_pattern: str, value: str) -> Selection:
    values = releases(repository, tag_pattern)
    exact = [release for release in values if release.identity == value]
    matches = exact or [release for release in values if release.version == value]
    if len(matches) != 1:
        raise ValueError("Exact GitHub tag metadata is missing or ambiguous")
    return {"release": bind(repository, matches[0]), "tag": matches[0].identity}
