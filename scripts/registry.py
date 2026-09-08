"""Version selection from dated registry artifacts, with explicit age evidence."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
import json
import hashlib
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version as PythonVersion
from packaging.utils import canonicalize_name
from semantic_version import NpmSpec, Version as Semver

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Artifact:
    url: str
    digest: str
    published: datetime


@dataclass(frozen=True)
class Release:
    version: str
    published: datetime
    identity: str = ""
    python: str = ""
    artifacts: tuple[Artifact, ...] = ()


class RegistryHTTPError(ValueError):
    def __init__(self, status: int, host: str):
        self.status = status
        super().__init__(f"Registry HTTP {status} from {host}")


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Missing registry publication age")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Registry timestamp lacks a timezone")
    return result.astimezone(timezone.utc)


def artifact_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Missing artifact URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError(
            "Artifact URL must be credential-free HTTPS without a fragment"
        )
    return value


def digest(value: str, *, npm: bool = False) -> str:
    """Canonicalize supported immutable hash evidence; never accept a missing hash."""
    if not isinstance(value, str):
        raise ValueError("Missing artifact digest")
    if npm:
        match = re.fullmatch(
            r"(sha1|sha256|sha384|sha512)-([A-Za-z0-9+/]+={0,2})", value
        )
        if match:
            algorithm, encoded = match.groups()
            try:
                raw = base64.b64decode(encoded, validate=True)
            except binascii.Error:
                raw = b""
            if (
                len(raw)
                == {"sha1": 20, "sha256": 32, "sha384": 48, "sha512": 64}[algorithm]
            ):
                return f"{algorithm}:{raw.hex()}"
    elif re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        return value
    raise ValueError("Unsupported or malformed artifact digest")


def package_name(provider: str, name: str) -> str:
    return canonicalize_name(name) if provider == "pypi" else name


def constraint(provider: str, policy: dict, name: str) -> str:
    rules = [
        rule
        for key, rule in policy.get("constraints", {}).items()
        if key.partition(":")[0] == provider
        and package_name(provider, key.partition(":")[2])
        == package_name(provider, name)
    ]
    if len(rules) > 1:
        raise ValueError("Duplicate normalized compatibility constraints")
    rule = rules[0] if rules else {}
    if rule and not str(rule.get("reason", "")).strip():
        raise ValueError("Compatibility constraints require a reason")
    return rule.get("range", "")


@lru_cache(maxsize=2048)
def fetch(
    url: str, accept: str = "application/json", method: str = "GET"
) -> tuple[bytes, dict]:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        raise ValueError("Registry requests require credential-free HTTPS URLs")
    for attempt in range(3):
        try:
            request = Request(
                url,
                headers={"User-Agent": "nix-just-toolchain", "Accept": accept},
                method=method,
            )
            with urlopen(request, timeout=30) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Registry response exceeds 64 MiB from {parsed.hostname}"
                    )
                return body, dict(response.headers.items())
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RegistryHTTPError(exc.code, parsed.hostname) from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise ValueError(f"Registry unavailable: {parsed.hostname}") from None
        time.sleep(2**attempt)
    raise AssertionError("unreachable")


def data(url: str):
    return json.loads(fetch(url)[0])


def version(provider: str, text: str):
    try:
        if provider == "maven":
            # Maven Central also publishes stable two- and four-component versions.
            return (
                PythonVersion(text)
                if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", text)
                else None
            )
        if provider == "pypi":
            result = PythonVersion(text)
            if result.is_prerelease or result.is_devrelease:
                return None
            return result
        # Deliberately do not coerce prerelease suffixes or incomplete tags.
        result = Semver(text.removeprefix("v"))
        return None if result.prerelease else result
    except (ValueError, InvalidVersion):
        return None


def compatible(provider: str, value: str, constraint: str) -> bool:
    if not constraint:
        return True
    if provider in ("pypi", "maven"):
        return PythonVersion(value) in SpecifierSet(constraint)
    return Semver(value.removeprefix("v")) in NpmSpec(constraint)


def lock_version(provider: str, value: str):
    """Parse existing npm prereleases without making them selectable releases."""
    if provider != "npm":
        return version(provider, value)
    try:
        return Semver(value)
    except (ValueError, TypeError):
        return None


def minimum_age(policy: dict) -> int:
    days = policy.get("minimum_age_days", 30)
    if type(days) is not int or days < 0:
        raise ValueError("minimum_age_days must be a nonnegative integer")
    return days


def minimum_safe(provider: str, policy: dict, name: str):
    floors = []
    for exception in policy.get("exceptions", []):
        scope, _, package = exception.get("package", "").partition(":")
        if scope != provider or package_name(provider, package) != package_name(
            provider, name
        ):
            continue
        if not all(
            exception.get(key)
            for key in ("version", "minimum_safe", "reason", "advisory", "expires")
        ):
            raise ValueError(
                "Security exceptions require exact version, safe floor, reason, advisory, and expiry"
            )
        floor = version(provider, exception["minimum_safe"])
        admitted = version(provider, exception["version"])
        timestamp(exception["expires"])
        if floor is None or admitted is None or admitted < floor:
            raise ValueError("Invalid exception safe floor")
        floors.append(floor)
    return max(floors) if floors else None


def maturity(
    provider: str, releases: list[Release], policy: dict, name: str, now: datetime
) -> list[Release]:
    bound = constraint(provider, policy, name)
    safe = minimum_safe(provider, policy, name)
    cutoff = now - timedelta(days=minimum_age(policy))
    candidates = []
    dates = {}
    for release in releases:
        dates[release.version] = max(
            dates.get(release.version, release.published), release.published
        )
    for release in releases:
        rank = version(provider, release.version)
        if (
            rank is None
            or (safe is not None and rank < safe)
            or release.python == "unsupported"
            or not compatible(provider, release.version, bound)
        ):
            continue
        if dates[release.version] > now:
            raise ValueError(f"Future registry timestamp for {name}")
        if dates[release.version] <= cutoff:
            candidates.append(release)
    return candidates


def active_exceptions(
    provider: str, releases: list[Release], policy: dict, name: str, now: datetime
) -> list[Release]:
    mature = maturity(provider, releases, policy, name, now)
    bound = constraint(provider, policy, name)
    required_safe = minimum_safe(provider, policy, name)
    candidates = []
    # An exception admits one exact version only while a mature safe version is absent.
    for exception in policy.get("exceptions", []):
        scope, _, package = exception.get("package", "").partition(":")
        if scope != provider or package_name(provider, package) != package_name(
            provider, name
        ):
            continue
        if not all(
            exception.get(k)
            for k in ("version", "minimum_safe", "reason", "advisory", "expires")
        ):
            raise ValueError(
                "Security exceptions require exact version, safe floor, reason, advisory, and expiry"
            )
        expiry = timestamp(exception["expires"])
        safe = version(provider, exception["minimum_safe"])
        admitted = version(provider, exception["version"])
        if safe is None or admitted is None or admitted < safe:
            raise ValueError("Invalid exception safe floor")
        if any(version(provider, release.version) >= safe for release in mature):
            continue
        if expiry <= now:
            raise ValueError(f"Expired security exception for {name}")
        for release in releases:
            if (
                release.version == exception["version"]
                and version(provider, release.version) >= required_safe
                and release.python != "unsupported"
                and compatible(provider, release.version, bound)
            ):
                candidates.append(release)
    return candidates


def eligible(
    provider: str, releases: list[Release], policy: dict, name: str, now: datetime
) -> list[Release]:
    candidates = maturity(provider, releases, policy, name, now) + active_exceptions(
        provider, releases, policy, name, now
    )
    if not candidates:
        raise ValueError(
            f"No eligible stable release with verified age for {provider}:{name}"
        )
    return candidates


def select(
    provider: str, releases: list[Release], policy: dict, name: str, now: datetime
) -> Release:
    return max(
        eligible(provider, releases, policy, name, now),
        key=lambda item: version(provider, item.version),
    )


def github_releases(repository: str) -> list[Release]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected a GitHub owner/repository")
    releases = []
    for page in range(1, 101):
        entries = data(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}"
        )
        if not isinstance(entries, list):
            raise ValueError("Malformed GitHub release response")
        for item in entries:
            if (
                not item["draft"]
                and not item["prerelease"]
                and version("github", item["tag_name"]) is not None
            ):
                releases.append(
                    Release(item["tag_name"], timestamp(item["published_at"]))
                )
        if len(entries) < 100:
            return releases
    raise ValueError(
        "GitHub release pagination ceiling reached; selection is incomplete"
    )


def github_commit(repository: str, tag: str) -> str:
    item = data(
        f"https://api.github.com/repos/{repository}/git/ref/tags/{quote(tag, safe='')}"
    )["object"]
    for _ in range(10):
        if item["type"] == "commit" and re.fullmatch(r"[a-f0-9]{40}", item["sha"]):
            return item["sha"]
        if item["type"] != "tag":
            break
        item = data(
            f"https://api.github.com/repos/{repository}/git/tags/{item['sha']}"
        )["object"]
    raise ValueError("Release tag does not resolve to a bounded commit identity")


def go_path(package: str) -> str:
    if (
        not isinstance(package, str)
        or not re.fullmatch(r"[A-Za-z0-9._~/-]+", package)
        or "." not in package.split("/")[0]
        or any(part in ("", ".", "..") for part in package.split("/"))
    ):
        raise ValueError("Unsupported public Go module path")
    return "".join("!" + c.lower() if c.isupper() else c for c in package)


def go_version(value: str) -> bool:
    # Audit canonical pseudoversions too; selection still excludes prereleases.
    if not isinstance(value, str) or not value.startswith("v"):
        return False
    try:
        parsed = Semver(value[1:])
        return not parsed.build or parsed.build == ("incompatible",)
    except ValueError:
        return False


def go_info(package: str, value: str) -> Release:
    if not go_version(value):
        raise ValueError("Invalid Go module version")
    item = data(
        f"https://proxy.golang.org/{go_path(package)}/@v/{quote(value, safe='')}.info"
    )
    if item.get("Version") != value:
        raise ValueError("Go proxy version identity mismatch")
    # Go's Time is version-creation time, not registry publication time. This
    # public proxy metadata is not authenticated by the checksum database.
    return Release(value, timestamp(item.get("Time")))


def go_digest(value: str) -> str:
    try:
        raw = base64.b64decode(value.removeprefix("h1:"), validate=True)
    except (AttributeError, binascii.Error):
        raw = b""
    if not isinstance(value, str) or not value.startswith("h1:") or len(raw) != 32:
        raise ValueError("Missing or malformed Go h1 checksum")
    return "h1:" + base64.b64encode(raw).decode()


def go_artifacts(package: str, value: str) -> tuple[Artifact, ...]:
    release = go_info(package, value)
    escaped = go_path(package)
    body = fetch(
        f"https://sum.golang.org/lookup/{escaped}@{quote(value, safe='')}", "text/plain"
    )[0].decode()
    result = {}
    # The record body precedes the signed tree. Do not interpret the signature
    # as a locally verified transparency proof; Go performs native verification.
    lines = body.split("\n\n", 1)[0].splitlines()
    if not lines or not lines[0].isdigit():
        raise ValueError("Malformed Go checksum database record")
    for line in lines[1:]:
        parts = line.split()
        if (
            len(parts) != 3
            or parts[0] != package
            or parts[1] not in (value, value + "/go.mod")
        ):
            raise ValueError("Go checksum database identity mismatch")
        suffix = ".mod" if parts[1].endswith("/go.mod") else ".zip"
        if suffix in result:
            raise ValueError("Duplicate Go checksum database identity")
        result[suffix] = Artifact(
            f"https://proxy.golang.org/{escaped}/@v/{quote(value, safe='')}{suffix}",
            go_digest(parts[2]),
            release.published,
        )
    if set(result) != {".mod", ".zip"}:
        raise ValueError("Go checksum database lacks module or go.mod checksum")
    return tuple(result.values())


def maven_prefix(package: str, repository: str = "central") -> str:
    if not isinstance(package, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*:[A-Za-z0-9_.-]+", package
    ):
        raise ValueError("Unsupported Maven coordinates")
    group, name = package.split(":")
    if name in (".", ".."):
        raise ValueError("Unsupported Maven artifact name")
    bases = {
        "central": "https://repo.maven.apache.org/maven2",
        "google": "https://dl.google.com/dl/android/maven2",
        "plugins": "https://plugins.gradle.org/m2",
    }
    if repository not in bases or (
        repository == "google" and not group.startswith(("androidx.", "com.android."))
    ):
        raise ValueError("Unsupported Maven repository or group scope")
    if repository == "plugins" and not name.endswith(
        ("-gradle-plugin", ".gradle.plugin")
    ):
        raise ValueError("Unsupported Gradle Plugin Portal coordinate scope")
    return f"{bases[repository]}/{group.replace('.', '/')}/{name}"


def maven_content_digest(url: str, modified: str) -> str:
    # Hash actual bytes when the repository has no corresponding SHA-256 sidecar.
    hashed, total = hashlib.sha256(), 0
    with urlopen(
        Request(url, headers={"User-Agent": "nix-just-toolchain"}), timeout=30
    ) as response:
        if response.headers.get("Last-Modified") != modified:
            raise ValueError(
                "Maven artifact changed between metadata and content requests"
            )
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 512 * 1024 * 1024:
                raise ValueError("Maven artifact exceeds 512 MiB audit bound")
            hashed.update(chunk)
    return hashed.hexdigest()


def maven_artifact(
    package: str, value: str, filename: str, repository: str = "central"
) -> Artifact:
    if version("maven", value) is None or not re.fullmatch(
        r"[A-Za-z0-9_.+-]+", filename
    ):
        raise ValueError("Unsupported Maven artifact identity")
    name = package.split(":")[1]
    if not filename.startswith(f"{name}-{value}.") and not filename.startswith(
        f"{name}-{value}-"
    ):
        raise ValueError("Maven artifact filename disagrees with component coordinates")
    url = f"{maven_prefix(package, repository)}/{value}/{filename}"
    _, headers = fetch(url, "application/octet-stream", "HEAD")
    modified = next(
        (v for k, v in headers.items() if k.lower() == "last-modified"), None
    )
    if not modified:
        raise ValueError("Maven artifact lacks publication-age metadata")
    at = parsedate_to_datetime(modified)
    if at.tzinfo is None:
        raise ValueError("Maven publication-age metadata lacks a timezone")
    if repository == "plugins":
        # Portal checksum URLs can redirect to Central even when the Portal
        # artifact has different bytes. Its own artifact bytes are authoritative.
        checksum = maven_content_digest(url, modified)
    else:
        try:
            checksum = fetch(url + ".sha256", "text/plain")[0].decode().strip()
        except RegistryHTTPError as exc:
            if exc.status != 404:
                raise
            checksum = maven_content_digest(url, modified)
    return Artifact(url, digest("sha256:" + checksum), at.astimezone(timezone.utc))


def maven_releases(package: str, repository: str = "central") -> list[Release]:
    prefix = maven_prefix(package, repository)
    xml = ET.fromstring(fetch(f"{prefix}/maven-metadata.xml", "application/xml")[0])
    result = []
    for item in xml.findall("./versioning/versions/version"):
        name = item.text or ""
        if version("maven", name) is None:
            continue
        artifact = maven_artifact(
            package, name, f"{package.split(':')[1]}-{name}.pom", repository
        )
        result.append(Release(name, artifact.published, artifacts=(artifact,)))
    return result


def docker_releases(repository: str, accepts_tag) -> list[Release]:
    """Discover dated tags; callers must validate the selected manifest identity."""
    if isinstance(repository, str):
        repository = repository.removeprefix("docker.io/")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository
    ):
        raise ValueError("Docker Hub requires an explicit namespace/repository")
    prefix = f"https://hub.docker.com/v2/repositories/{repository}/tags"
    url = prefix + "?page_size=100"
    result, visited = [], set()
    for _ in range(100):
        if url in visited or not url.startswith((prefix + "?", prefix + "/?")):
            raise ValueError("Unexpected Docker Hub pagination target")
        visited.add(url)
        body = data(url)
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise ValueError("Malformed Docker Hub tag inventory")
        for entry in body["results"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError("Malformed Docker Hub tag entry")
            if not accepts_tag(entry["name"]):
                continue
            # Legacy inventory can lack a digest. Keep the candidate visible to
            # ranking so missing evidence cannot silently select an older tag.
            identity = entry.get("digest")
            result.append(
                Release(
                    entry["name"],
                    timestamp(entry.get("last_updated")),
                    "" if identity is None else digest(identity),
                )
            )
        url = body.get("next")
        if url is None:
            return result
        if not isinstance(url, str) or not url:
            raise ValueError("Unexpected Docker Hub pagination target")
    raise ValueError("Docker Hub pagination exceeded its bound")


def releases(
    provider: str, package: str, *, include_prerelease: bool = False
) -> list[Release]:
    if provider == "go":
        values = (
            fetch(f"https://proxy.golang.org/{go_path(package)}/@v/list", "text/plain")[
                0
            ]
            .decode()
            .splitlines()
        )
        return [
            go_info(package, value)
            for value in values
            if version("go", value) is not None and go_version(value)
        ]
    if provider == "swift":
        import source_updates

        return [
            Release(
                r.version.removeprefix("v"),
                max(
                    r.published,
                    source_updates.commit_time(
                        package, github_commit(package, r.version)
                    ),
                ),
                r.version,
            )
            for r in github_releases(package)
        ]
    if provider == "npm":
        body = data(f"https://registry.npmjs.org/{quote(package, safe='')}")
        result = []
        for value, info in body["versions"].items():
            parsed = (
                lock_version(provider, value)
                if include_prerelease
                else version(provider, value)
            )
            if parsed is None or info.get("deprecated"):
                continue
            dist = info.get("dist", {})
            integrity = dist.get("integrity")
            # Older npm releases expose the registry's SHA-1 tarball checksum.
            if not integrity and re.fullmatch(r"[a-f0-9]{40}", dist.get("shasum", "")):
                integrity = (
                    "sha1-" + base64.b64encode(bytes.fromhex(dist["shasum"])).decode()
                )
            artifact = Artifact(
                artifact_url(dist.get("tarball")),
                digest(integrity, npm=True),
                timestamp(body.get("time", {}).get(value)),
            )
            result.append(Release(value, artifact.published, artifacts=(artifact,)))
        return result
    if provider == "crates":
        body = data(f"https://crates.io/api/v1/crates/{quote(package, safe='')}")
        result = []
        for value in body["versions"]:
            if value["yanked"] or version(provider, value["num"]) is None:
                continue
            artifact = Artifact(
                f"https://crates.io/api/v1/crates/{quote(package, safe='')}/{value['num']}/download",
                digest("sha256:" + value.get("checksum", "")),
                timestamp(value.get("created_at")),
            )
            result.append(
                Release(value["num"], artifact.published, artifacts=(artifact,))
            )
        return result
    if provider == "pypi":
        import platform

        body = data(f"https://pypi.org/pypi/{quote(package, safe='')}/json")
        result = []
        for name, artifacts in body["releases"].items():
            available = [a for a in artifacts if not a.get("yanked")]
            if version(provider, name) is not None and available:
                actual = tuple(
                    Artifact(
                        artifact_url(a.get("url")),
                        digest("sha256:" + a.get("digests", {}).get("sha256", "")),
                        timestamp(a.get("upload_time_iso_8601")),
                    )
                    for a in available
                )
                # Keep evidence for all locked platforms/Pythons. A release with
                # no host-compatible file is retained for audit, but not selection.
                supported = any(
                    not a.get("requires_python")
                    or PythonVersion(platform.python_version())
                    in SpecifierSet(a["requires_python"])
                    for a in available
                )
                result.append(
                    Release(
                        name,
                        max(a.published for a in actual),
                        python="" if supported else "unsupported",
                        artifacts=actual,
                    )
                )
        return result
    if provider == "pub":
        body = data(f"https://pub.dev/api/packages/{quote(package, safe='')}")
        result = []
        for value in body["versions"]:
            if value.get("retracted") or version(provider, value["version"]) is None:
                continue
            artifact = Artifact(
                artifact_url(value.get("archive_url")),
                digest("sha256:" + value.get("archive_sha256", "")),
                timestamp(value.get("published")),
            )
            result.append(
                Release(value["version"], artifact.published, artifacts=(artifact,))
            )
        return result
    if provider == "github":
        return github_releases(package)
    if provider == "docker":
        return docker_releases(package, lambda tag: version(provider, tag) is not None)
    if provider == "maven":
        return maven_releases(package)
    raise ValueError(f"Unsupported registry provider: {provider}")
