"""Opt-in integration with the packaged SDK adapter and exact native npm CLI."""

import base64
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from attestation_bridge import inspect_package, npm_executable
from attestation_common import command, sha512
from attestation_github import Identity
from attestation_model import Artifact, LoadedArtifact, LoadedManifest, Manifest, policy
from attestation_provenance import PREDICATE_TYPE, VerifiedArtifact, VerifiedManifest
from attestation_publication import npm_registry, publish, snapshot_packages

CONFIGURED = policy(
    {
        "provider": "github-actions",
        "repository": "Example/Package",
        "workflow": ".github/workflows/qualification.yml",
        "ref": "refs/heads/master",
        "requiredJobs": ["Qualification", "Sign bundles"],
        "artifactName": "qualified-release-{runId}-{runAttempt}",
        "tag": "next",
    }
)
IDENTITY = Identity("a" * 40, "12345", "2")
VERSION = "1.0.0-rc.0"


def archive(directory, name, extra=None):
    data = {
        "name": name,
        "version": VERSION,
        "repository": "git+https://github.com/Example/Package.git",
        **(extra or {}),
    }
    destination = directory / (name.replace("@", "").replace("/", "-") + ".tgz")
    with tarfile.open(destination, "w:gz") as output:
        content = json.dumps(data).encode()
        entry = tarfile.TarInfo("package/package.json")
        entry.size = len(content)
        output.addfile(entry, io.BytesIO(content))
    return destination


def verified(path, metadata, bundle_bytes=None):
    content = path.read_bytes()
    artifact = Artifact(path.name, metadata.subject, sha512(content))
    if bundle_bytes is None:
        statement = {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": PREDICATE_TYPE,
            "subject": [
                {"name": artifact.subject, "digest": {"sha512": artifact.sha512}}
            ],
            "predicate": {},
        }
        bundle_bytes = json.dumps(
            {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "dsseEnvelope": {
                    "payloadType": "application/vnd.in-toto+json",
                    "payload": base64.b64encode(
                        json.dumps(statement).encode()
                    ).decode(),
                },
            }
        ).encode()
    bundle = json.loads(bundle_bytes)
    loaded = LoadedManifest(
        Manifest(1, IDENTITY.source_commit, (artifact,)),
        path.parent,
        path.parent / "manifest.json",
        (LoadedArtifact(artifact, content, path),),
    )
    return VerifiedManifest(
        loaded,
        IDENTITY,
        (
            VerifiedArtifact(
                artifact,
                content,
                path,
                bundle,
                bundle_bytes,
                Path(str(path) + ".sigstore.json"),
            ),
        ),
        path.parent / "unused-verification-directory",
    )


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_ATTEST_RUNTIME") == "1",
    "requires the pinned optional attest runtime",
)
class AttestationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="chainman-attest-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.npm = npm_executable()
        self.assertTrue(Path(os.environ["CHAINMAN_ATTEST_ADAPTER"]).is_absolute())

    def snapshot(self, fixture, selected_policy=CONFIGURED):
        destination = (
            self.directory / f"snapshot-{len(list(self.directory.glob('snapshot-*')))}"
        )
        destination.mkdir(mode=0o700)
        return snapshot_packages(fixture, destination, selected_policy)

    def test_packaged_python_entry_is_isolated_and_retains_manual_publication_gate(
        self,
    ):
        configured = self.directory / "policy.json"
        configured.write_text(json.dumps(CONFIGURED.encode()))
        # A consumer's current directory cannot replace isolated runtime imports.
        (self.directory / "attestation_publication.py").write_text(
            "raise RuntimeError('shadow import')\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(ROOT / "scripts/isolated.py"),
                "attestation_entry.py",
                "npm-publish",
                "unused.json",
                "--run",
                "12345",
                "--policy",
                str(configured),
            ],
            cwd=self.directory,
            env={**os.environ, "CI": "true"},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("manual only", result.stderr)
        self.assertNotIn("shadow import", result.stderr)

    def test_native_normalization_and_complete_preflight_precede_registry_access(self):
        accepted = [
            "https://github.com/Example/Package",
            "https://GITHUB.COM/Example/Package.git",
            "git+https://github.com/Example/Package.git",
            "git@github.com:Example/Package.git",
            "ssh://git@github.com/Example/Package.git",
            "git+ssh://git@github.com/Example/Package.git",
            "github:Example/Package",
            "Example/Package",
            {
                "type": "git",
                "url": "https://github.com/Example/Package.git",
                "directory": "packages/fixture",
            },
        ]
        for index, repository in enumerate(accepted):
            with self.subTest(repository=repository):
                path = archive(
                    self.directory,
                    f"@example/repository-{index}",
                    {"repository": repository},
                )
                metadata = inspect_package(path)
                self.assertEqual(metadata.repository, "github.com/Example/Package")
                self.assertEqual(
                    metadata.subject, f"pkg:npm/%40example/repository-{index}@{VERSION}"
                )
                self.assertEqual(
                    self.snapshot(verified(path, metadata))[0].name,
                    f"@example/repository-{index}",
                )
        calls = []

        class Registry:
            def lookup(self, pkg, wait=False):
                calls.append("lookup")

            def publish(self, pkg):
                calls.append("upload")

            def verify(self, pkg):
                calls.append("verify")

        good = archive(self.directory, "@example/good")
        good_fixture = verified(good, inspect_package(good))
        # npm's full reader gives legacy repositories precedence over repository.
        conflict = archive(
            self.directory,
            "@example/conflict",
            {
                "repository": "github:Example/Package",
                "repositories": ["github:Example/Other"],
            },
        )
        metadata = inspect_package(conflict)
        self.assertEqual(metadata.repository, "github.com/Example/Other")
        self.assertEqual(
            metadata.manifest["repository"]["url"],
            "git+https://github.com/Example/Other.git",
        )
        reverse = archive(
            self.directory,
            "@example/reverse",
            {
                "repository": "github:Example/Other",
                "repositories": ["github:Example/Package"],
            },
        )
        self.assertEqual(
            inspect_package(reverse).repository, "github.com/Example/Package"
        )
        provenance = archive(
            self.directory,
            "@example/provenance",
            {
                "publishConfig": {"provenance": True},
            },
        )
        for path, message in [(conflict, "repository"), (provenance, "provenance")]:
            later = verified(path, inspect_package(path))
            combined = replace(
                good_fixture, artifacts=good_fixture.artifacts + later.artifacts
            )
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, message):
                publish(self.snapshot(combined), Registry())
        self.assertEqual(
            calls, [], "later preflight failure must precede every lookup/upload"
        )

    def test_native_npm_uploads_exact_saved_bytes_and_reconciles_readback(self):
        inputs = [
            os.environ.get(name)
            for name in (
                "CHAINMAN_ATTEST_SMOKE_TARBALL",
                "CHAINMAN_ATTEST_SMOKE_BUNDLE",
                "CHAINMAN_ATTEST_SMOKE_REPOSITORY",
            )
        ]
        if any(inputs) and not all(inputs):
            self.fail(
                "Actual smoke tarball, saved bundle and signing repository are all required"
            )
        actual_tarball, actual_bundle, actual_repository = inputs
        if actual_tarball:
            path = Path(actual_tarball)
        else:
            source = self.directory / "package"
            source.mkdir()
            (source / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@example/fixture",
                        "version": VERSION,
                        "repository": "git+https://github.com/Example/Package.git",
                        "scripts": {
                            "prepublishOnly": "exit 91",
                            "prepare": "exit 92",
                            "publish": "exit 93",
                        },
                        "publishConfig": {
                            "registry": "https://untrusted.invalid/",
                            "tag": "latest",
                        },
                    }
                )
            )
            (source / "index.js").write_text("export default 1;\n")
            result = command(
                self.npm,
                [
                    "pack",
                    str(source),
                    "--ignore-scripts",
                    "--pack-destination",
                    str(self.directory),
                    "--json",
                ],
            )
            self.assertEqual(result.status, 0, result.stderr)
            packed = json.loads(result.stdout)
            record = (
                packed[0] if isinstance(packed, list) else packed["@example/fixture"]
            )
            path = self.directory / record["filename"]
        metadata = inspect_package(path)
        fixture = verified(
            path, metadata, Path(actual_bundle).read_bytes() if actual_bundle else None
        )
        selected_policy = replace(
            CONFIGURED, repository=actual_repository or CONFIGURED.repository
        )
        packages = self.snapshot(fixture, selected_policy)
        pkg = packages[0]
        name, version = pkg.name, pkg.version
        scope = name.split("/")[0] if name.startswith("@") else None
        state = {"uploads": 0, "requests": 0, "uploaded": None, "wrong": False}
        registry_url = ""

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, value, status=200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_PUT(self):
                state["requests"] += 1
                state["uploaded"] = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                state["uploads"] += 1
                self.reply({"ok": True}, 201)

            def do_GET(self):
                state["requests"] += 1
                uploaded = state["uploaded"]
                if uploaded is None:
                    self.reply({"error": "not_found"}, 404)
                    return
                selected = unquote(self.path)
                attachments = uploaded["_attachments"]
                if "/-/npm/v1/attestations/" in selected:
                    provenance = next(
                        value
                        for key, value in attachments.items()
                        if key.endswith(".sigstore")
                    )
                    self.reply(
                        {
                            "attestations": [
                                {
                                    "predicateType": PREDICATE_TYPE,
                                    "bundle": {}
                                    if state["wrong"]
                                    else json.loads(provenance["data"]),
                                }
                            ]
                        }
                    )
                elif "/-/" in selected:
                    attachment = next(
                        value
                        for key, value in attachments.items()
                        if key.endswith(".tgz")
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(base64.b64decode(attachment["data"]))
                else:
                    published = uploaded["versions"][version]
                    self.reply(
                        {
                            **uploaded,
                            "dist-tags": {"next": version},
                            "versions": {
                                version: {
                                    **published,
                                    "dist": {
                                        **published["dist"],
                                        "tarball": registry_url
                                        + "fixture/-/fixture.tgz",
                                        "attestations": {
                                            "url": registry_url
                                            + "-/npm/v1/attestations/fixture",
                                            "provenance": {
                                                "predicateType": PREDICATE_TYPE
                                            },
                                        },
                                    },
                                }
                            },
                        }
                    )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        registry_url = f"http://127.0.0.1:{server.server_address[1]}/"
        config = self.directory / "npmrc"
        config.write_text(
            "registry=https://untrusted.invalid/\n"
            + (f"{scope}:registry=https://untrusted.invalid/\n" if scope else "")
            + f"//127.0.0.1:{server.server_address[1]}/:_authToken=synthetic-loopback-token\n"
        )
        environment = {
            **os.environ,
            "NPM_CONFIG_USERCONFIG": str(config),
            "NPM_CONFIG_CACHE": str(self.directory / "cache"),
            "NPM_CONFIG_FETCH_RETRIES": "0",
        }
        commands = []
        marker = self.directory / "trust-calls.jsonl"
        preload = self.directory / "fixture-trust.cjs"
        npm_root = (
            Path(self.npm).resolve().parent.parent
            / "lib/chainman-attest/node_modules/npm"
        )
        # Offline mode isolates the prior signature-verification boundary. Every
        # metadata/subject/upload/readback operation still uses actual pinned npm.
        # Real bundle mode uses the installed wrapper and actual Sigstore verifier.
        if not actual_bundle:
            exec_line = next(
                line
                for line in Path(self.npm).read_text().splitlines()
                if line.startswith("exec ")
            )
            native_node = shlex.split(exec_line)[1]
            preload.write_text(
                "const fs=require('node:fs');const r=require('node:module').createRequire("
                + json.dumps(str(npm_root / "package.json"))
                + ");Object.defineProperty(r('sigstore'),'verify',{value:async b=>{fs.appendFileSync("
                + json.dumps(str(marker))
                + ",JSON.stringify(b)+'\\n')}});\n"
            )

        def native(program, args, interactive=False, env=None):
            commands.append(list(args))
            if not actual_bundle and args[0] == "publish":
                result = command(
                    native_node,
                    [
                        "--require",
                        str(preload),
                        str(npm_root / "bin/npm-cli.js"),
                        *args,
                    ],
                    False,
                    {**environment, "NODE_OPTIONS": "--use-openssl-ca"},
                )
            else:
                result = command(program, args, False, environment)
            if args[0] == "publish":
                self.assertEqual(result.status, 0, result.stderr)
            return result

        registry = npm_registry(
            registry=registry_url, attempts=1, delay_ms=0, command=native
        )
        self.assertIsNone(
            registry.lookup(pkg), "native JSON E404 permits absent version"
        )
        publish(packages, registry)
        self.assertEqual(state["uploads"], 1)
        uploaded = state["uploaded"]
        self.assertEqual(uploaded["dist-tags"], {"next": version})
        attachments = uploaded["_attachments"]
        tarball = next(
            value for key, value in attachments.items() if key.endswith(".tgz")
        )
        provenance = next(
            value for key, value in attachments.items() if key.endswith(".sigstore")
        )
        self.assertEqual(base64.b64decode(tarball["data"]), path.read_bytes())
        self.assertEqual(json.loads(provenance["data"]), pkg.bundle)
        if not actual_bundle:
            self.assertEqual(len(marker.read_text().strip().splitlines()), 1)
        native_view = native(
            self.npm,
            [
                "view",
                f"{name}@{version}",
                "dist",
                "--json",
                "--registry",
                registry_url,
                *([f"--{scope}:registry={registry_url}"] if scope else []),
                "--loglevel=silent",
            ],
        )
        self.assertEqual(native_view.status, 0, native_view.stderr)
        self.assertIsInstance(
            json.loads(native_view.stdout),
            list,
            "npm12 exact version view returns an array",
        )
        publish(packages, registry)
        self.assertEqual(
            state["uploads"], 1, "matching publication resumes without upload"
        )
        state["wrong"] = True
        with self.assertRaisesRegex(
            ValueError, "differs from the exact verified bundle"
        ):
            publish(packages, registry)
        self.assertEqual(state["uploads"], 1)
        self.assertGreater(state["requests"], 5)
        self.assertTrue(
            any(
                "--ignore-scripts" in args
                and "--provenance-file" in args
                and (not scope or f"--{scope}:registry={registry_url}" in args)
                for args in commands
            )
        )
        bad = archive(self.directory, "@example/bad-tag", {"tag": "latest"})
        with self.assertRaisesRegex(ValueError, "next publication policy"):
            self.snapshot(verified(bad, inspect_package(bad)))
        self.assertEqual(state["uploads"], 1)


if __name__ == "__main__":
    unittest.main()
