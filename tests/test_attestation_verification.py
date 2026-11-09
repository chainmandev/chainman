"""GitHub qualification and saved Sigstore bundle contracts, without network writes."""

import base64
import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from attestation_archive import bundle_archive
from attestation_common import CommandResult, sha512
from attestation_github import (
    Github,
    Identity,
    QualifiedIdentity,
    environment_identity,
    verify_run,
)
from attestation_model import Artifact, load_manifest, manifest, policy
from attestation_provenance import (
    BUILD_TYPE,
    PREDICATE_TYPE,
    create_attestations,
    verification_arguments,
    verify_bundle,
    verify_certificate,
    verify_manifest,
)

CONFIGURED = policy(
    {
        "provider": "github-actions",
        "repository": "example/package",
        "workflow": ".github/workflows/qualification.yml",
        "ref": "refs/heads/master",
        "requiredJobs": ["Qualification", "Support (*)", "Sign bundles"],
        "artifactName": "qualified-release-{runId}-{runAttempt}",
        "tag": "next",
    }
)
IDENTITY = Identity("a" * 40, "12345", "2")
CONTENT = b"fixture artifact bytes"
ARTIFACT = Artifact(
    "package.tgz", "pkg:npm/%40example/package@1.0.0-rc.0", sha512(CONTENT)
)
REPOSITORY = {"full_name": CONFIGURED.repository, "private": False}
RUN = {
    "id": 12345,
    "run_attempt": 2,
    "status": "completed",
    "conclusion": "success",
    "head_sha": IDENTITY.source_commit,
    "head_branch": "master",
    "event": "push",
    "repository": REPOSITORY,
    "head_repository": REPOSITORY,
    "path": CONFIGURED.workflow,
    "html_url": f"https://github.com/{CONFIGURED.repository}/actions/runs/{IDENTITY.run_id}",
    "run_started_at": "2026-11-08T12:00:00Z",
    "updated_at": "2026-11-08T12:30:00Z",
}
JOBS = [
    {
        "name": f"qualification / {name}",
        "run_id": 12345,
        "run_attempt": 2,
        "head_sha": IDENTITY.source_commit,
        "status": "completed",
        "conclusion": "success",
    }
    for name in ("Qualification", "Support (node)", "Sign bundles")
]
ARTIFACTS = [
    {
        "id": 98765,
        "name": "qualified-release-12345-2",
        "expired": False,
        "created_at": "2026-11-08T12:20:00Z",
        "workflow_run": {
            "id": 12345,
            "head_sha": IDENTITY.source_commit,
            "head_branch": "master",
        },
    }
]
CERTIFICATE = {
    "runInvocationURI": f"https://github.com/{CONFIGURED.repository}/actions/runs/{IDENTITY.run_id}/attempts/{IDENTITY.attempt}",
    "sourceRepositoryVisibilityAtSigning": "public",
    "runnerEnvironment": "github-hosted",
    "sourceRepositoryDigest": IDENTITY.source_commit,
    "sourceRepositoryRef": CONFIGURED.ref,
    "buildSignerDigest": IDENTITY.source_commit,
    "buildSignerURI": f"https://github.com/{CONFIGURED.repository}/{CONFIGURED.workflow}@{CONFIGURED.ref}",
}


def statement():
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": PREDICATE_TYPE,
        "subject": [{"name": ARTIFACT.subject, "digest": {"sha512": ARTIFACT.sha512}}],
        "predicate": {
            "buildDefinition": {
                "buildType": BUILD_TYPE,
                "externalParameters": {
                    "workflow": {
                        "repository": f"https://github.com/{CONFIGURED.repository}",
                        "path": CONFIGURED.workflow,
                        "ref": CONFIGURED.ref,
                    }
                },
                "internalParameters": {
                    "github": {
                        "event_name": "push",
                        "runner_environment": "github-hosted",
                    }
                },
                "resolvedDependencies": [
                    {
                        "uri": f"git+https://github.com/{CONFIGURED.repository}@{CONFIGURED.ref}",
                        "digest": {"gitCommit": IDENTITY.source_commit},
                    }
                ],
            },
            "runDetails": {
                "builder": {
                    "id": f"https://github.com/{CONFIGURED.repository}/{CONFIGURED.workflow}@{CONFIGURED.ref}"
                },
                "metadata": {
                    "invocationId": f"https://github.com/{CONFIGURED.repository}/actions/runs/{IDENTITY.run_id}/attempts/{IDENTITY.attempt}"
                },
            },
        },
    }


def bundle(payload=None):
    return {
        "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
        "dsseEnvelope": {
            "payloadType": "application/vnd.in-toto+json",
            "payload": base64.b64encode(
                json.dumps(statement() if payload is None else payload).encode()
            ).decode(),
        },
    }


def verified_certificate(certificate=None):
    return [
        {
            "verificationResult": {
                "signature": {
                    "certificate": CERTIFICATE if certificate is None else certificate
                }
            }
        }
    ]


def environment():
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": CONFIGURED.repository,
        "GITHUB_REF": CONFIGURED.ref,
        "GITHUB_WORKFLOW_REF": f"{CONFIGURED.repository}/{CONFIGURED.workflow}@{CONFIGURED.ref}",
        "GITHUB_SHA": IDENTITY.source_commit,
        "GITHUB_WORKFLOW_SHA": IDENTITY.source_commit,
        "GITHUB_RUN_ID": IDENTITY.run_id,
        "GITHUB_RUN_ATTEMPT": IDENTITY.attempt,
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_EVENT_NAME": "push",
    }


def zip_bytes(entries):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in entries:
                name, content, *mode = entry
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (mode[0] if mode else stat.S_IFREG) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
    return output.getvalue()


class FixtureGithub(Github):
    def __init__(self, callback=None, archive=None):
        self.calls = []
        self.callback = callback
        self.archive_bytes = archive
        super().__init__(CONFIGURED, self.invoke_fixture)

    def invoke_fixture(
        self,
        program: str,
        args: Sequence[str],
        interactive: bool = False,
        env: Mapping[str, str] | None = None,
    ):
        self.calls.append((program, list(args), interactive, env))
        if self.callback is not None:
            result = self.callback(args)
        elif args[0] == "attestation":
            result = verified_certificate()
        elif any("/jobs" in argument for argument in args):
            result = [{"jobs": JOBS}]
        elif any("/artifacts" in argument for argument in args):
            result = [{"artifacts": ARTIFACTS}]
        else:
            result = RUN
        return CommandResult(0, json.dumps(result), "")

    def archive(self, artifact_id):
        if artifact_id != "98765":
            raise AssertionError("Wrong qualified artifact")
        return (
            zip_bytes(
                [
                    (f"{ARTIFACT.path}.sigstore.json", json.dumps(bundle()).encode()),
                    ("manifest.json", b"untrusted garbage"),
                    (ARTIFACT.path, b"downloaded tarball must not be used"),
                ]
            )
            if self.archive_bytes is None
            else self.archive_bytes
        )


class AttestationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chainman-attest-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "manifest.json"
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sourceCommit": IDENTITY.source_commit,
                    "artifacts": [ARTIFACT.encode()],
                }
            )
        )
        (self.directory / ARTIFACT.path).write_bytes(CONTENT)

    def test_strict_policy_and_manifest_fields_duplicates_and_paths(self):
        valid = json.loads(self.path.read_text())
        self.assertEqual(manifest(valid).encode(), valid)
        for change in (
            {"provider": "other"},
            {"unknown": True},
            {"artifactName": "qualified-release-{runId}"},
            {"requiredJobs": ["Qualification", "Qualification"]},
            {"tag": "latest"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                policy({**CONFIGURED.encode(), **change})
        for path in (
            "../x",
            "/x",
            "a/../x",
            "a//x",
            "a\\x",
            "./x",
            "a\x00x",
            "a\nx",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manifest({**valid, "artifacts": [{**ARTIFACT.encode(), "path": path}]})
        for entries in (
            [ARTIFACT.encode(), ARTIFACT.encode()],
            [
                ARTIFACT.encode(),
                {
                    **ARTIFACT.encode(),
                    "subject": "other",
                    "path": f"{ARTIFACT.path}.sigstore.json",
                },
            ],
            [
                {**ARTIFACT.encode(), "attestation": "shared.json"},
                {
                    **ARTIFACT.encode(),
                    "subject": "other",
                    "path": "other.tgz",
                    "attestation": "shared.json",
                },
            ],
            [
                {
                    **ARTIFACT.encode(),
                    "digest": {"sha512": ARTIFACT.sha512, "sha256": "f" * 64},
                }
            ],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                manifest({**valid, "artifacts": entries})
        with self.assertRaises(ValueError):
            manifest({**valid, "version": True})

    def test_manifest_rejects_changed_bytes_and_symlink_components(self):
        self.assertEqual(len(load_manifest(self.path).artifacts), 1)
        artifact_path = self.directory / ARTIFACT.path
        artifact_path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            load_manifest(self.path)
        artifact_path.unlink()
        artifact_path.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "without symlinks"):
            load_manifest(self.path)
        (self.directory / "real").mkdir()
        (self.directory / "real/package.tgz").write_bytes(CONTENT)
        (self.directory / "link").symlink_to(
            self.directory / "real", target_is_directory=True
        )
        value = json.loads(self.path.read_text())
        value["artifacts"][0]["path"] = "link/package.tgz"
        self.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "without symlinks"):
            load_manifest(self.path)
        self.path.unlink()
        self.path.symlink_to(self.directory / "real/package.tgz")
        with self.assertRaisesRegex(ValueError, "without symlinks"):
            load_manifest(self.path)

    def test_qualification_requires_same_public_source_attempt_jobs_and_artifact(self):
        self.assertEqual(
            verify_run(CONFIGURED, RUN, JOBS, ARTIFACTS, IDENTITY.run_id),
            QualifiedIdentity(
                IDENTITY.source_commit, IDENTITY.run_id, IDENTITY.attempt, "98765"
            ),
        )
        for change in (
            {"run_attempt": 1},
            {"head_sha": "b" * 40},
            {"event": "pull_request"},
            {"repository": {**REPOSITORY, "private": True}},
            {"head_repository": {**REPOSITORY, "full_name": "fork/package"}},
            {"path": ".github/workflows/other.yml"},
            {"status": "in_progress"},
            {"html_url": "https://enterprise.example/actions/runs/12345"},
            {"run_started_at": "invalid"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_run(
                    CONFIGURED, {**RUN, **change}, JOBS, ARTIFACTS, IDENTITY.run_id
                )
        for change in (
            {"conclusion": "skipped"},
            {"run_attempt": 1},
            {"head_sha": "b" * 40},
            {"run_id": 99},
            {"name": "other Qualification"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_run(
                    CONFIGURED,
                    RUN,
                    [{**JOBS[0], **change}, *JOBS[1:]],
                    ARTIFACTS,
                    IDENTITY.run_id,
                )
        for entries in (
            [],
            [*ARTIFACTS, *ARTIFACTS],
            [{**ARTIFACTS[0], "name": "qualified-release-12345-1"}],
            [{**ARTIFACTS[0], "created_at": "2026-11-08T11:59:00Z"}],
            [{**ARTIFACTS[0], "created_at": "2026-11-08T12:31:00Z"}],
            [{**ARTIFACTS[0], "expired": True}],
            [
                {
                    **ARTIFACTS[0],
                    "workflow_run": {
                        "id": 12345,
                        "head_sha": "b" * 40,
                        "head_branch": "master",
                    },
                }
            ],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                verify_run(CONFIGURED, RUN, JOBS, entries, IDENTITY.run_id)

    def test_creation_environment_is_exact_direct_public_hosted_workflow(self):
        self.assertEqual(environment_identity(CONFIGURED, environment()), IDENTITY)
        for change in (
            {"GITHUB_WORKFLOW_SHA": "b" * 40},
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"RUNNER_ENVIRONMENT": "self-hosted"},
            {"GITHUB_REPOSITORY": "fork/package"},
            {"GITHUB_JOB_WORKFLOW_REF": "other/workflow"},
            {"GITHUB_SERVER_URL": "https://private.example"},
            {"GITHUB_RUN_ID": "0"},
            {"GITHUB_RUN_ATTEMPT": "-1"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                environment_identity(CONFIGURED, {**environment(), **change})

    def test_signing_run_only_allows_own_signer_after_successful_qualification(self):
        signing_jobs = [
            {
                **job,
                "status": "in_progress",
                "conclusion": None,
                "runner_name": "hosted-runner",
            }
            if index == 2
            else job
            for index, job in enumerate(JOBS)
        ]

        def response(args):
            return (
                [{"jobs": signing_jobs}]
                if any("/jobs" in arg for arg in args)
                else {**RUN, "status": "in_progress"}
            )

        github = FixtureGithub(response)
        github.signing_run(IDENTITY, "hosted-runner")
        with self.assertRaisesRegex(ValueError, "preceding qualification"):
            github.signing_run(IDENTITY, "other")
        signing_jobs[0] = {**signing_jobs[0], "conclusion": "skipped"}
        with self.assertRaisesRegex(ValueError, "preceding qualification"):
            github.signing_run(IDENTITY, "hosted-runner")

    def test_exact_single_subject_sdk_predicate_and_certificate(self):
        self.assertEqual(BUILD_TYPE, "https://actions.github.io/buildtypes/workflow/v1")
        verify_bundle(bundle(), ARTIFACT, IDENTITY, CONFIGURED)
        with self.assertRaises(ValueError):
            verify_bundle(
                bundle(),
                replace(ARTIFACT, subject="pkg:npm/wrong@1.0.0"),
                IDENTITY,
                CONFIGURED,
            )
        with self.assertRaises(ValueError):
            verify_bundle(
                bundle(), ARTIFACT, replace(IDENTITY, attempt="1"), CONFIGURED
            )
        for field, replacement in (
            ("_type", "other"),
            ("predicateType", "other"),
            ("subject", statement()["subject"] * 2),
            (
                "subject",
                [
                    {
                        "name": ARTIFACT.subject,
                        "digest": {"sha512": ARTIFACT.sha512, "sha256": "b" * 64},
                    }
                ],
            ),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_bundle(
                    bundle({**statement(), field: replacement}),
                    ARTIFACT,
                    IDENTITY,
                    CONFIGURED,
                )
        predicate_mutations = (
            (("buildDefinition", "buildType"), "other"),
            (
                ("buildDefinition", "externalParameters", "workflow", "repository"),
                "https://github.com/fork/package",
            ),
            (
                ("buildDefinition", "externalParameters", "workflow", "path"),
                ".github/workflows/other.yml",
            ),
            (
                ("buildDefinition", "externalParameters", "workflow", "ref"),
                "refs/heads/other",
            ),
            (
                ("buildDefinition", "internalParameters", "github", "event_name"),
                "pull_request",
            ),
            (
                (
                    "buildDefinition",
                    "internalParameters",
                    "github",
                    "runner_environment",
                ),
                "self-hosted",
            ),
            (("buildDefinition", "resolvedDependencies"), []),
            (("runDetails", "builder", "id"), "other"),
            (("runDetails", "metadata", "invocationId"), "other"),
        )
        for fields, replacement in predicate_mutations:
            payload = statement()
            target = payload["predicate"]
            for field in fields[:-1]:
                target = target[field]
            target[fields[-1]] = replacement
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                verify_bundle(bundle(payload), ARTIFACT, IDENTITY, CONFIGURED)
        verify_certificate(verified_certificate(), IDENTITY, CONFIGURED)
        for field in CERTIFICATE:
            with self.subTest(field=field), self.assertRaises(ValueError):
                verify_certificate(
                    verified_certificate({**CERTIFICATE, field: "incorrect"}),
                    IDENTITY,
                    CONFIGURED,
                )
        with self.assertRaises(ValueError):
            verify_certificate(verified_certificate() * 2, IDENTITY, CONFIGURED)
        args = verification_arguments(
            "/local/package.tgz", "/local/package.sigstore.json", IDENTITY, CONFIGURED
        )
        for flag, value in (
            ("--bundle", "/local/package.sigstore.json"),
            ("--source-ref", CONFIGURED.ref),
            ("--signer-digest", IDENTITY.source_commit),
            ("--source-digest", IDENTITY.source_commit),
            ("--digest-alg", "sha512"),
            ("--cert-oidc-issuer", "https://token.actions.githubusercontent.com"),
        ):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--deny-self-hosted-runners", args)

    def test_verification_uses_local_bytes_and_private_exact_saved_bundle(self):
        github = FixtureGithub()
        (self.directory / f"{ARTIFACT.path}.sigstore.json").write_bytes(
            b"untrusted local bundle"
        )
        result = verify_manifest(self.path, IDENTITY.run_id, CONFIGURED, github)
        self.addCleanup(result.dispose)
        selected = result.artifacts[0]
        self.assertEqual(selected.bytes, CONTENT)
        self.assertEqual(selected.bundle_bytes, json.dumps(bundle()).encode())
        self.assertEqual(selected.bundle_path.read_bytes(), selected.bundle_bytes)
        self.assertEqual(selected.bundle_path.name, f"{ARTIFACT.path}.sigstore.json")
        self.assertNotEqual(selected.bundle_path.parent, self.directory)
        self.assertEqual(stat.S_IMODE(result.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(selected.bundle_path.stat().st_mode), 0o400)
        self.assertTrue(
            any(
                any("/attempts/2/jobs" in arg for arg in args)
                for _, args, _, _ in github.calls
            )
        )
        for _, args, _, env in github.calls:
            self.assertEqual(env["GH_HOST"], "github.com")
            if args[0] == "api":
                self.assertEqual(args[1:3], ["--hostname", "github.com"])
            self.assertNotIn("download", args)
        result.dispose()
        self.assertFalse(result.directory.exists())

    def test_verification_rejects_changes_while_verifier_runs_and_cleans_private_files(
        self,
    ):
        for target in ("artifact", "bundle"):
            private_directories = []

            def response(args):
                if args[0] == "attestation":
                    saved = Path(args[args.index("--bundle") + 1])
                    private_directories.append(saved.parent)
                    changed = (
                        self.directory / ARTIFACT.path
                        if target == "artifact"
                        else saved
                    )
                    changed.chmod(0o600)
                    changed.write_bytes(b"changed during verifier")
                    return verified_certificate()
                if any("/jobs" in arg for arg in args):
                    return [{"jobs": JOBS}]
                if any("/artifacts" in arg for arg in args):
                    return [{"artifacts": ARTIFACTS}]
                return RUN

            (self.directory / ARTIFACT.path).write_bytes(CONTENT)
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(ValueError, "changed during verification"),
            ):
                verify_manifest(
                    self.path, IDENTITY.run_id, CONFIGURED, FixtureGithub(response)
                )
            self.assertTrue(private_directories)
            self.assertTrue(all(not path.exists() for path in private_directories))

    def test_archive_rejects_traversal_special_files_duplicates_and_ambiguity(self):
        expected = f"{ARTIFACT.path}.sigstore.json"
        content = b"bundle"
        self.assertEqual(
            bundle_archive(
                zip_bytes([(expected, content), ("manifest.json", b"not JSON")]),
                [expected],
            ),
            {expected: content},
        )
        for entries in (
            [("../escape", content)],
            [("a\\escape", content)],
            [(expected, content, stat.S_IFLNK)],
            [("unused", content, stat.S_IFIFO), (expected, content)],
            [(expected, content), (expected, content)],
            [(f"a/{expected}", content), (f"b/{expected}", content)],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                bundle_archive(zip_bytes(entries), [expected])
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            bundle_archive(zip_bytes([(expected, content)]), [expected, expected])
        with self.assertRaisesRegex(ValueError, "missing expected"):
            bundle_archive(zip_bytes([("manifest.json", b"not JSON")]), [expected])
        with self.assertRaises(ValueError):
            bundle_archive(b"not a ZIP", [expected])

    def test_archive_encryption_entry_count_and_uncompressed_byte_limits(self):
        expected = f"{ARTIFACT.path}.sigstore.json"
        data = bytearray(zip_bytes([(expected, b"bundle")]))
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, 6, 1)
        struct.pack_into("<H", data, central + 8, 1)
        with self.assertRaisesRegex(ValueError, "Encrypted"):
            bundle_archive(bytes(data), [expected])
        data = bytearray(zip_bytes([(expected, b"bundle")]))
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, 22, 16 * 1024 * 1024 + 1)
        struct.pack_into("<I", data, central + 24, 16 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "excessive qualified bundle bytes"):
            bundle_archive(bytes(data), [expected])
        with self.assertRaisesRegex(ValueError, "excessive qualified archive entries"):
            bundle_archive(
                zip_bytes([(f"entry-{index}", b"") for index in range(10001)]), []
            )

    def test_archive_total_selected_bytes_are_bounded(self):
        content = b"x" * (16 * 1024 * 1024)
        names = [f"bundle-{index}.json" for index in range(5)]
        data = zip_bytes([(name, content) for name in names])
        with self.assertRaisesRegex(ValueError, "excessive qualified bundle bytes"):
            bundle_archive(data, names)

    def test_archive_download_pins_host_discards_diagnostics_and_enforces_limit(self):
        executable = self.directory / "gh-fixture"
        capture = self.directory / "download.json"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            f"with open({str(capture)!r}, 'w') as output:\n"
            "    json.dump({'args': sys.argv[1:], 'host': os.environ['GH_HOST']}, output)\n"
            "sys.stderr.write('https://signed.example/archive?token=private\\n' * 10000)\n"
            "sys.stdout.buffer.write(b'archive bytes')\n"
        )
        executable.chmod(0o700)
        github = Github(CONFIGURED, executable=str(executable))
        with patch.dict(os.environ, {"GH_HOST": "enterprise.example"}):
            self.assertEqual(github.archive("98765"), b"archive bytes")
        self.assertEqual(
            json.loads(capture.read_text()),
            {
                "args": [
                    "api",
                    "--hostname",
                    "github.com",
                    "repos/example/package/actions/artifacts/98765/zip",
                ],
                "host": "github.com",
            },
        )
        executable.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "sys.stderr.write('https://signed.example/archive?token=private')\n"
            "raise SystemExit(3)\n"
        )
        with self.assertRaisesRegex(
            ValueError, "^Qualified bundle archive download failed$"
        ):
            github.archive("98765")
        executable.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "for _ in range(65):\n"
            "    sys.stdout.buffer.write(b'x' * (1024 * 1024))\n"
        )
        with self.assertRaisesRegex(ValueError, "archive exceeds size limit"):
            github.archive("98765")

    def test_create_saves_only_sdk_bundle_and_rewrites_manifest(self):
        event = self.directory / "event.json"
        event.write_text(json.dumps({"repository": {"private": False}}))
        env = {
            **environment(),
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_TOKEN": "test-token",
            "RUNNER_NAME": "runner",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("attestation_provenance.Github") as github,
            patch(
                "attestation_provenance.attest_provenance", return_value=bundle()
            ) as sign,
        ):
            create_attestations(self.path, CONFIGURED, IDENTITY.run_id)
            github.return_value.signing_run.assert_called_once_with(IDENTITY, "runner")
            sign.assert_called_once_with(ARTIFACT)
        saved = self.directory / f"{ARTIFACT.path}.sigstore.json"
        self.assertEqual(json.loads(saved.read_text()), bundle())
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(
            load_manifest(self.path).artifacts[0].artifact.attestation, saved.name
        )

    def test_create_rejects_private_event_wrong_run_and_failed_signing_cleans_up(self):
        event = self.directory / "event.json"
        event.write_text(json.dumps({"repository": {"private": True}}))
        env = {
            **environment(),
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_TOKEN": "test-token",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "Requested run differs"):
                create_attestations(self.path, CONFIGURED, "99")
            with self.assertRaisesRegex(ValueError, "public repository event"):
                create_attestations(self.path, CONFIGURED)
        event.write_text(json.dumps({"repository": {"private": False}}))
        with (
            patch.dict(os.environ, env, clear=True),
            patch("attestation_provenance.Github"),
            patch(
                "attestation_provenance.attest_provenance",
                side_effect=ValueError("signing failed"),
            ),
            self.assertRaisesRegex(ValueError, "signing failed"),
        ):
            create_attestations(self.path, CONFIGURED)
        self.assertFalse((self.directory / f"{ARTIFACT.path}.sigstore.json").exists())
        self.assertIsNone(load_manifest(self.path).artifacts[0].artifact.attestation)

    def test_create_rejects_replacement_and_does_not_overwrite_saved_files(self):
        event = self.directory / "event.json"
        event.write_text(json.dumps({"repository": {"private": False}}))
        env = {
            **environment(),
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_TOKEN": "test-token",
        }
        destination = self.directory / f"{ARTIFACT.path}.sigstore.json"

        def sign(_artifact):
            (self.directory / ARTIFACT.path).write_bytes(b"changed while signing")
            return bundle()

        with (
            patch.dict(os.environ, env, clear=True),
            patch("attestation_provenance.Github"),
            patch("attestation_provenance.attest_provenance", side_effect=sign),
            self.assertRaisesRegex(ValueError, "changed during attestation signing"),
        ):
            create_attestations(self.path, CONFIGURED)
        self.assertFalse(destination.exists())
        (self.directory / ARTIFACT.path).write_bytes(CONTENT)
        destination.write_bytes(b"existing saved bundle")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("attestation_provenance.Github"),
            patch("attestation_provenance.attest_provenance") as signer,
            self.assertRaises(FileExistsError),
        ):
            create_attestations(self.path, CONFIGURED)
        signer.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"existing saved bundle")
        value = json.loads(self.path.read_text())
        value["artifacts"][0]["attestation"] = "elsewhere/bundle.json"
        self.path.write_text(json.dumps(value))
        with (
            patch.dict(os.environ, env, clear=True),
            patch("attestation_provenance.Github"),
            self.assertRaisesRegex(ValueError, "beside its artifact"),
        ):
            create_attestations(self.path, CONFIGURED)


if __name__ == "__main__":
    unittest.main()
