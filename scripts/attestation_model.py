"""Validated policy, manifest and local artifact snapshots."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from adapter_data import array
from attestation_common import keys, object_value, parse, sha512, string


@dataclass(frozen=True)
class Policy:
    provider: str
    repository: str
    workflow: str
    ref: str
    required_jobs: tuple[str, ...]
    artifact_name: str
    tag: str | None = None

    def encode(self) -> dict[str, object]:
        result: dict[str, object] = {
            "provider": self.provider,
            "repository": self.repository,
            "workflow": self.workflow,
            "ref": self.ref,
            "requiredJobs": list(self.required_jobs),
            "artifactName": self.artifact_name,
        }
        if self.tag is not None:
            result["tag"] = self.tag
        return result


def policy(value: object) -> Policy:
    data = object_value(value)
    keys(
        data,
        [
            "provider",
            "repository",
            "workflow",
            "ref",
            "requiredJobs",
            "artifactName",
            "tag",
        ],
    )
    if data.get("provider") != "github-actions":
        raise ValueError(
            "Unsupported attestation provider; initially only github-actions is supported"
        )
    if "tag" in data and data["tag"] != "next":
        raise ValueError("Initially npm publication supports only the next tag")
    repository = string(data.get("repository"))
    workflow = string(data.get("workflow"))
    ref = string(data.get("ref"))
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", workflow)
        or not re.fullmatch(r"refs/heads/[A-Za-z0-9_./-]+", ref)
        or ".." in ref
        or ref.endswith("/")
    ):
        raise ValueError("Invalid repository, direct workflow or branch ref policy")
    jobs = tuple(
        string(job)
        for job in array(data.get("requiredJobs"), "Required qualification jobs")
    )
    if (
        not jobs
        or len(jobs) != len(set(jobs))
        or any(not job.strip() or any(c in job for c in "\0\r\n") for job in jobs)
    ):
        raise ValueError(
            "Required qualification jobs must contain distinct nonempty job names"
        )
    artifact_name = string(data.get("artifactName"))
    if (
        "{runId}" not in artifact_name
        or "{runAttempt}" not in artifact_name
        or not re.fullmatch(r"[A-Za-z0-9_.{}-]+", artifact_name)
        or re.search(
            r"[{}]", artifact_name.replace("{runId}", "").replace("{runAttempt}", "")
        )
    ):
        raise ValueError("Artifact name must contain runId and runAttempt placeholders")
    return Policy(
        "github-actions",
        repository,
        workflow,
        ref,
        jobs,
        artifact_name,
        "next" if "tag" in data else None,
    )


@dataclass(frozen=True)
class Artifact:
    path: str
    subject: str
    sha512: str
    attestation: str | None = None

    def encode(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "subject": self.subject,
            "digest": {"sha512": self.sha512},
        }
        if self.attestation is not None:
            result["attestation"] = self.attestation
        return result


@dataclass(frozen=True)
class Manifest:
    version: int
    source_commit: str
    artifacts: tuple[Artifact, ...]

    def encode(self) -> dict[str, object]:
        return {
            "version": self.version,
            "sourceCommit": self.source_commit,
            "artifacts": [item.encode() for item in self.artifacts],
        }


def relative_path(value: object) -> str:
    path = string(value)
    if (
        Path(path).is_absolute()
        or re.search(r"[\\\x00-\x1f\x7f]", path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError(
            "Expected a contained manifest-relative path without traversal"
        )
    return path


def manifest(value: object) -> Manifest:
    data = object_value(value)
    keys(data, ["version", "sourceCommit", "artifacts"])
    source_commit = string(data.get("sourceCommit"))
    if (
        type(data.get("version")) is not int
        or data["version"] != 1
        or not re.fullmatch(r"[a-f0-9]{40}", source_commit)
    ):
        raise ValueError("Invalid manifest version or source commit")
    entries = array(data.get("artifacts"), "Manifest artifacts")
    if not entries:
        raise ValueError("Manifest must select artifacts")
    paths: set[str] = set()
    subjects: set[str] = set()
    artifacts: list[Artifact] = []
    for entry in entries:
        item = object_value(entry)
        keys(item, ["path", "subject", "digest", "attestation"])
        path = relative_path(item.get("path"))
        subject = string(item.get("subject"))
        digest = object_value(item.get("digest"))
        keys(digest, ["sha512"])
        digest_hex = string(digest.get("sha512"))
        if not re.fullmatch(r"[a-f0-9]{128}", digest_hex):
            raise ValueError("Expected canonical SHA-512 artifact digest")
        if path in paths or subject in subjects:
            raise ValueError("Duplicate manifest artifact path or subject")
        paths.add(path)
        subjects.add(subject)
        attestation = (
            relative_path(item["attestation"]) if "attestation" in item else None
        )
        artifacts.append(Artifact(path, subject, digest_hex, attestation))
    for artifact in artifacts:
        bundle = artifact.attestation or f"{artifact.path}.sigstore.json"
        if bundle in paths:
            raise ValueError("Duplicate or overlapping artifact/bundle path")
        paths.add(bundle)
    return Manifest(1, source_commit, tuple(artifacts))


def contained_file(root: Path, relative: str) -> bytes:
    components = relative_path(relative).split("/")
    path = root
    for index, component in enumerate(components):
        path /= component
        mode = path.lstat().st_mode
        expected = (
            stat.S_ISREG(mode) if index == len(components) - 1 else stat.S_ISDIR(mode)
        )
        if not expected:
            raise ValueError(
                "Manifest paths must contain regular files and directories without symlinks"
            )
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Expected a regular artifact file")
        return stream.read()


@dataclass(frozen=True)
class LoadedArtifact:
    artifact: Artifact
    bytes: bytes
    path: Path


@dataclass(frozen=True)
class LoadedManifest:
    manifest: Manifest
    root: Path
    path: Path
    artifacts: tuple[LoadedArtifact, ...]


def load_manifest(path: Path | str) -> LoadedManifest:
    absolute = Path(path).absolute()
    root = absolute.parent.resolve(strict=True)
    data = manifest(parse(contained_file(root, absolute.name)))
    artifacts: list[LoadedArtifact] = []
    for artifact in data.artifacts:
        content = contained_file(root, artifact.path)
        if sha512(content) != artifact.sha512:
            raise ValueError(f"Artifact digest mismatch: {artifact.path}")
        artifacts.append(LoadedArtifact(artifact, content, root / artifact.path))
    return LoadedManifest(data, root, root / absolute.name, tuple(artifacts))
