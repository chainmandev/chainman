"""Strict security-exception declarations shared by selection and inspection."""

from collections.abc import Mapping
from pathlib import PurePosixPath
import re
from urllib.parse import urlsplit

import adapter_data as ad
import registry

KINDS = ("exceptions", "nix_exceptions", "artifact_exceptions")
METADATA = {"reason", "advisory", "expires"}
POLICY_FIELDS = set(
    "minimum_age_days policy_file constraints exceptions nix_exceptions artifact_exceptions adapters steps targets target_groups profile outputs resolver eligibility verify verify_task verify_tasks reconcile_tasks reconcile_outputs pins nix docker javascript".split()
)


def entries(policy: Mapping[str, object], kind: str) -> list[ad.Table]:
    return [ad.table(value, kind) for value in ad.array(policy.get(kind, []), kind)]


def metadata(entry: Mapping[str, object], allowed: set[str]) -> None:
    unknown = set(entry) - allowed - METADATA
    if unknown:
        raise ValueError(
            "Unknown security exception fields: " + ", ".join(sorted(unknown))
        )
    for key in METADATA:
        if not ad.nonempty(entry.get(key), "Exception " + key).strip():
            raise ValueError("Exception metadata must not be blank")
    advisory = urlsplit(ad.text(entry["advisory"], "Advisory"))
    if (
        advisory.scheme != "https"
        or not advisory.hostname
        or advisory.username
        or advisory.password
    ):
        raise ValueError("Exception advisory must be a credential-free HTTPS URL")
    registry.timestamp(entry["expires"])


def nix_identity(entry: Mapping[str, object]) -> tuple[str, str, str]:
    directory = ad.text(entry.get("directory", "."), "Nix exception directory")
    path = PurePosixPath(directory)
    if (
        not directory
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in directory
        or str(path) != directory
    ):
        raise ValueError("Nix exception directory must be a canonical relative path")
    name = ad.text(entry.get("input"), "Nix exception input")
    repository = ad.text(entry.get("repository"), "Nix exception repository")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", name):
        raise ValueError("Nix exceptions require an exact input path")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
        p in {".", ".."} for p in repository.split("/")
    ):
        raise ValueError("Nix exceptions require an explicit GitHub repository")
    return directory, name, repository


def validate(policy: Mapping[str, object]) -> None:
    """Validate declarations even when their adapter/package is not selected."""
    unknown = set(policy) - POLICY_FIELDS
    if unknown:
        raise ValueError(
            "Unknown dependency policy fields: " + ", ".join(sorted(unknown))
        )
    registry.minimum_age(policy)
    seen: set[tuple[str, ...]] = set()
    for entry in entries(policy, "exceptions"):
        metadata(entry, {"package", "version", "minimum_safe", "applies_to"})
        exception = ad.AgeException.decode(entry)
        provider, sep, name = ad.text(
            entry.get("package"), "Exception package"
        ).partition(":")
        if (
            not sep
            or not name
            or provider
            not in {
                "npm",
                "crates",
                "pypi",
                "github",
                "docker",
                "go",
                "pub",
                "swift",
                "maven",
            }
        ):
            raise ValueError("Exceptions require a supported provider:package identity")
        package = registry.package_name(provider, name)
        registry.minimum_safe(provider, {"exceptions": [entry]}, package)
        key: tuple[str, ...] = (
            "package",
            provider,
            package,
            exception.version,
            exception.applies_to,
        )
        if key in seen:
            raise ValueError("Duplicate package security exception")
        seen.add(key)
    for entry in entries(policy, "nix_exceptions"):
        metadata(entry, {"directory", "input", "repository", "revision"})
        directory, name, repository = nix_identity(entry)
        if not re.fullmatch(
            r"[a-f0-9]{40}", ad.text(entry.get("revision"), "Nix exception revision")
        ):
            raise ValueError("Nix exceptions require an exact immutable revision")
        key = ("nix", directory, name)
        if key in seen:
            raise ValueError("Duplicate or conflicting Nix security exception")
        seen.add(key)
    for entry in entries(policy, "artifact_exceptions"):
        metadata(entry, {"url", "digest"})
        url = ad.text(entry.get("url"), "Artifact exception URL")
        registry.artifact_url(url)
        if not re.fullmatch(
            r"sha256:[a-f0-9]{64}",
            ad.text(entry.get("digest"), "Artifact exception digest"),
        ):
            raise ValueError("Artifact exceptions require an exact SHA-256 digest")
        key = ("artifact", url, ad.text(entry["digest"], "Artifact digest"))
        if key in seen:
            raise ValueError("Duplicate artifact security exception")
        seen.add(key)
    for raw in ad.table(policy.get("adapters", {}), "Adapters").values():
        spec = ad.table(raw, "Adapter")
        validate(ad.table(spec.get("policy", {}), "Adapter policy"))
