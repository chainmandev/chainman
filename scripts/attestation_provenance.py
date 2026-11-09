"""Create and verify exact saved GitHub Actions Sigstore attestations."""

import base64
import builtins
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from attestation_archive import bundle_archive
from attestation_bridge import attest_provenance
from attestation_common import object_value, parse, records, string
from attestation_github import Github, Identity, environment_identity
from attestation_model import (
    Artifact,
    LoadedManifest,
    Policy,
    contained_file,
    load_manifest,
)

PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"


def verify_bundle(
    bundle: object, artifact: Artifact, identity: Identity, policy: Policy
) -> None:
    envelope = object_value(object_value(bundle).get("dsseEnvelope"))
    if envelope.get("payloadType") != "application/vnd.in-toto+json":
        raise ValueError("Unexpected attestation payload type")
    try:
        payload = base64.b64decode(string(envelope.get("payload")), validate=True)
    except ValueError as error:
        raise ValueError("Invalid attestation payload encoding") from error
    statement = object_value(parse(payload))
    subjects = records(statement.get("subject"))
    if (
        statement.get("_type") != "https://in-toto.io/Statement/v1"
        or statement.get("predicateType") != PREDICATE_TYPE
        or len(subjects) != 1
        or subjects[0].get("name") != artifact.subject
        or object_value(subjects[0].get("digest")).get("sha512") != artifact.sha512
        or len(object_value(subjects[0].get("digest"))) != 1
    ):
        raise ValueError(
            "Attestation must identify exactly the selected subject and SHA-512"
        )
    predicate = object_value(statement.get("predicate"))
    definition = object_value(predicate.get("buildDefinition"))
    workflow = object_value(
        object_value(definition.get("externalParameters")).get("workflow")
    )
    dependencies = records(definition.get("resolvedDependencies"))
    internal = object_value(
        object_value(definition.get("internalParameters")).get("github")
    )
    details = object_value(predicate.get("runDetails"))
    if (
        definition.get("buildType") != BUILD_TYPE
        or workflow.get("repository") != f"https://github.com/{policy.repository}"
        or workflow.get("path") != policy.workflow
        or workflow.get("ref") != policy.ref
        or string(internal.get("event_name")) not in ("push", "workflow_dispatch")
        or internal.get("runner_environment") != "github-hosted"
        or len(dependencies) != 1
        or dependencies[0].get("uri")
        != f"git+https://github.com/{policy.repository}@{policy.ref}"
        or object_value(dependencies[0].get("digest")).get("gitCommit")
        != identity.source_commit
        or object_value(details.get("builder")).get("id")
        != f"https://github.com/{policy.repository}/{policy.workflow}@{policy.ref}"
        or object_value(details.get("metadata")).get("invocationId")
        != f"https://github.com/{policy.repository}/actions/runs/{identity.run_id}/attempts/{identity.attempt}"
    ):
        raise ValueError(
            "Attestation source, direct workflow, hosted runner or run attempt mismatch"
        )


def verification_arguments(
    artifact_path: str, bundle_path: str, identity: Identity, policy: Policy
) -> list[str]:
    return [
        "attestation",
        "verify",
        artifact_path,
        "--repo",
        policy.repository,
        "--bundle",
        bundle_path,
        "--digest-alg",
        "sha512",
        "--signer-workflow",
        f"{policy.repository}/{policy.workflow}",
        "--signer-digest",
        identity.source_commit,
        "--source-digest",
        identity.source_commit,
        "--source-ref",
        policy.ref,
        "--deny-self-hosted-runners",
        "--cert-oidc-issuer",
        "https://token.actions.githubusercontent.com",
        "--predicate-type",
        PREDICATE_TYPE,
        "--format",
        "json",
    ]


def verify_certificate(value: object, identity: Identity, policy: Policy) -> None:
    results = records(value)
    if len(results) != 1:
        raise ValueError("Expected exactly one verified saved bundle")
    certificate = object_value(
        object_value(
            object_value(results[0].get("verificationResult")).get("signature")
        ).get("certificate")
    )
    if (
        certificate.get("runInvocationURI")
        != f"https://github.com/{policy.repository}/actions/runs/{identity.run_id}/attempts/{identity.attempt}"
        or certificate.get("sourceRepositoryVisibilityAtSigning") != "public"
        or certificate.get("runnerEnvironment") != "github-hosted"
        or certificate.get("sourceRepositoryDigest") != identity.source_commit
        or certificate.get("sourceRepositoryRef") != policy.ref
        or certificate.get("buildSignerDigest") != identity.source_commit
        or certificate.get("buildSignerURI")
        != f"https://github.com/{policy.repository}/{policy.workflow}@{policy.ref}"
    ):
        raise ValueError(
            "Verified certificate does not identify the requested public hosted direct signing workflow/source/run attempt"
        )


@dataclass(frozen=True)
class VerifiedArtifact:
    artifact: Artifact
    bytes: bytes
    path: Path
    bundle: object
    bundle_bytes: builtins.bytes
    bundle_path: Path


@dataclass(frozen=True)
class VerifiedManifest:
    loaded: LoadedManifest
    identity: Identity
    artifacts: tuple[VerifiedArtifact, ...]
    directory: Path

    def dispose(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def verify_manifest(
    path: Path | str, run_id: str, policy: Policy, github: Github | None = None
) -> VerifiedManifest:
    client = Github(policy) if github is None else github
    loaded = load_manifest(path)
    identity = client.run(run_id)
    if loaded.manifest.source_commit != identity.source_commit:
        raise ValueError("Local manifest differs from qualified source commit")
    names = [
        PurePosixPath(
            item.artifact.attestation or f"{item.artifact.path}.sigstore.json"
        ).name
        for item in loaded.artifacts
    ]
    bundles = bundle_archive(client.archive(identity.artifact_id), names)
    directory = Path(tempfile.mkdtemp(prefix="chainman-attest-"))
    artifacts: list[VerifiedArtifact] = []
    try:
        for selected in loaded.artifacts:
            relative = (
                selected.artifact.attestation
                or f"{selected.artifact.path}.sigstore.json"
            )
            bundle_bytes = bundles[PurePosixPath(relative).name]
            bundle = parse(bundle_bytes)
            bundle_path = directory / PurePosixPath(relative).name
            descriptor = os.open(
                bundle_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bundle_bytes)
            verify_bundle(bundle, selected.artifact, identity, policy)
            verify_certificate(
                client.json(
                    verification_arguments(
                        str(selected.path), str(bundle_path), identity, policy
                    )
                ),
                identity,
                policy,
            )
            if (
                contained_file(loaded.root, selected.artifact.path) != selected.bytes
                or contained_file(directory, bundle_path.name) != bundle_bytes
            ):
                raise ValueError("Artifact or saved bundle changed during verification")
            artifacts.append(
                VerifiedArtifact(
                    selected.artifact,
                    selected.bytes,
                    selected.path,
                    bundle,
                    bundle_bytes,
                    bundle_path,
                )
            )
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return VerifiedManifest(loaded, identity, tuple(artifacts), directory)


def create_attestations(
    path: Path | str, policy: Policy, requested_run_id: str | None = None
) -> None:
    identity = environment_identity(policy)
    loaded = load_manifest(path)
    if requested_run_id is not None and requested_run_id != identity.run_id:
        raise ValueError("Requested run differs from the signing environment")
    if loaded.manifest.source_commit != identity.source_commit:
        raise ValueError("Manifest source commit differs from signing workflow")
    event = os.environ.get("GITHUB_EVENT_PATH")
    if (
        not event
        or object_value(
            object_value(parse(Path(event).read_bytes())).get("repository")
        ).get("private")
        is not False
    ):
        raise ValueError("Signing requires a public repository event")
    if not (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        raise ValueError("Signing requires the workflow token")
    Github(policy).signing_run(identity, os.environ.get("RUNNER_NAME", ""))
    artifacts: list[Artifact] = []
    for selected in loaded.artifacts:
        relative = (
            selected.artifact.attestation or f"{selected.artifact.path}.sigstore.json"
        )
        if (
            PurePosixPath(relative).parent
            != PurePosixPath(selected.artifact.path).parent
        ):
            raise ValueError("Saved attestation must be beside its artifact")
        destination = loaded.root / relative
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        complete = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                bundle = attest_provenance(selected.artifact)
                verify_bundle(bundle, selected.artifact, identity, policy)
                if (
                    contained_file(loaded.root, selected.artifact.path)
                    != selected.bytes
                ):
                    raise ValueError("Artifact changed during attestation signing")
                stream.write((json.dumps(bundle, indent=2) + "\n").encode())
                complete = True
        finally:
            if not complete:
                destination.unlink(missing_ok=True)
        artifacts.append(replace(selected.artifact, attestation=relative))
    for selected in loaded.artifacts:
        if contained_file(loaded.root, selected.artifact.path) != selected.bytes:
            raise ValueError("Artifact changed before saving the signed manifest")
    descriptor = os.open(loaded.path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "wb") as stream:
        value = replace(loaded.manifest, artifacts=tuple(artifacts)).encode()
        stream.write((json.dumps(value, indent=2) + "\n").encode())
