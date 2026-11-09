"""Offline publication policy, exact snapshots, and registry reconciliation."""

import base64
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from attestation_bridge import PackageMetadata
from attestation_common import CommandResult, sha512
from attestation_model import Policy
import attestation_publication as publication
from attestation_publication import (
    Package,
    dependency_order,
    npm_publish,
    npm_registry,
    publish,
    snapshot_packages,
)
from attestation_provenance import PREDICATE_TYPE


POLICY = Policy(
    provider="github-actions",
    repository="Example/Package",
    workflow=".github/workflows/qualification.yml",
    ref="refs/heads/main",
    required_jobs=("Qualification", "Sign bundles"),
    artifact_name="release-{runId}-{runAttempt}",
    tag="next",
)
LEAF = Package(
    name="leaf",
    version="1.0.0",
    path=Path("/leaf.tgz"),
    bundle_path=Path("/leaf.sigstore.json"),
    bytes=b"leaf",
    bundle_bytes=b"{}",
    bundle={},
    dependencies=(),
)
PARENT = replace(LEAF, name="parent", bytes=b"parent", dependencies=("leaf",))
E404 = CommandResult(1, '{"error":{"code":"E404"}}', "")


def result(value):
    return CommandResult(0, json.dumps(value), "")


def verified(*packages):
    return SimpleNamespace(
        artifacts=tuple(
            SimpleNamespace(
                artifact=SimpleNamespace(
                    subject=f"pkg:npm/{name.replace('@', '%40')}@1.0.0"
                ),
                bytes=name.encode(),
                bundle_bytes=b'{"saved":true}',
                bundle={"saved": True},
            )
            for name in packages
        )
    )


def metadata(name="leaf", extra=None, repository="github.com/Example/Package"):
    return PackageMetadata(
        manifest={"name": name, "version": "1.0.0", **(extra or {})},
        subject=f"pkg:npm/{name.replace('@', '%40')}@1.0.0",
        repository=repository,
    )


class ReconciliationTests(unittest.TestCase):
    def test_stable_dependency_order_ignores_external_dependencies(self):
        independent = replace(LEAF, name="independent", dependencies=("external",))
        self.assertEqual(
            [pkg.name for pkg in dependency_order([PARENT, independent, LEAF])],
            ["leaf", "parent", "independent"],
        )
        self.assertEqual(dependency_order([]), [])

    def test_duplicate_and_cyclic_packages_fail_before_registry_access(self):
        for packages, message in (
            ([LEAF, LEAF], "Duplicate"),
            ([replace(LEAF, dependencies=("parent",)), PARENT], "cycle"),
            ([replace(LEAF, dependencies=("leaf",))], "cycle"),
        ):
            registry = Mock()
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                publish(packages, registry)
            self.assertEqual(registry.mock_calls, [])

    def test_all_registry_versions_and_bundles_preflight_before_any_upload(self):
        for collision in ("bytes", "provenance", "lookup"):
            calls = []

            def lookup(pkg, wait=False):
                calls.append(f"lookup:{pkg.name}")
                if pkg.name != "parent":
                    return None
                if collision == "lookup":
                    raise ValueError("lookup failure")
                return "wrong" if collision == "bytes" else sha512(pkg.bytes)

            registry = SimpleNamespace(
                lookup=lookup,
                publish=lambda pkg: calls.append(f"upload:{pkg.name}"),
                verify=Mock(side_effect=ValueError("provenance mismatch")),
            )
            with self.subTest(collision=collision), self.assertRaises(ValueError):
                publish([PARENT, LEAF], registry)
            self.assertFalse(any(call.startswith("upload:") for call in calls))

    def test_success_reconciles_all_packages_then_uploads_in_dependency_order(self):
        calls = []
        uploaded = set()

        def lookup(pkg, wait=False):
            calls.append(f"lookup:{pkg.name}:{wait}")
            return sha512(pkg.bytes) if pkg.name in uploaded else None

        def upload(pkg):
            calls.append(f"upload:{pkg.name}")
            uploaded.add(pkg.name)

        registry = SimpleNamespace(
            lookup=lookup,
            publish=upload,
            verify=lambda pkg: calls.append(f"verify:{pkg.name}"),
        )
        publish([PARENT, LEAF], registry)
        self.assertEqual(
            calls,
            [
                "lookup:leaf:False",
                "lookup:parent:False",
                "upload:leaf",
                "lookup:leaf:True",
                "verify:leaf",
                "upload:parent",
                "lookup:parent:True",
                "verify:parent",
            ],
        )

    def test_lost_upload_acknowledgement_and_resume_do_not_upload_again(self):
        uploaded = False

        def upload(pkg):
            nonlocal uploaded
            uploaded = True
            raise OSError("lost acknowledgement")

        registry = SimpleNamespace(
            lookup=Mock(
                side_effect=lambda pkg, wait=False: (
                    sha512(pkg.bytes) if uploaded else None
                )
            ),
            publish=Mock(side_effect=upload),
            verify=Mock(),
        )
        publish([LEAF], registry)
        publish([LEAF], registry)
        self.assertEqual(registry.publish.call_count, 1)
        self.assertEqual(registry.verify.call_count, 2)
        self.assertEqual(registry.lookup.call_args_list[1].args, (LEAF, True))

    def test_failed_upload_preserves_original_error_when_readback_fails_or_is_absent(
        self,
    ):
        for readback in (None, OSError("readback failure")):
            failure = OSError("original upload failure")
            registry = SimpleNamespace(
                lookup=Mock(side_effect=[None, readback]),
                publish=Mock(side_effect=failure),
                verify=Mock(),
            )
            with self.subTest(readback=readback), self.assertRaises(OSError) as caught:
                publish([LEAF], registry)
            self.assertIs(caught.exception, failure)
            registry.verify.assert_not_called()

    def test_post_upload_collision_or_provenance_failure_stops_remaining_uploads(self):
        for digest in (None, "wrong", sha512(LEAF.bytes)):
            registry = SimpleNamespace(
                lookup=Mock(side_effect=[None, None, digest]),
                publish=Mock(),
                verify=Mock(side_effect=ValueError("wrong provenance")),
            )
            with (
                self.subTest(digest=digest),
                self.assertRaisesRegex(ValueError, "digest mismatch|provenance"),
            ):
                publish([PARENT, LEAF], registry)
            self.assertEqual(registry.publish.call_count, 1)
            self.assertEqual(registry.publish.call_args.args, (LEAF,))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.views = []
        self.downloads = []
        self.fetch_value = LEAF.bytes
        self.sleeps = []

    def invoke(self, program, args, interactive=False, env=None):
        self.calls.append((program, list(args), interactive))
        if args[0] == "config":
            return CommandResult(0, "https://registry.npmjs.org/\n", "")
        if args[0] == "view":
            value = self.views.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return CommandResult(0, "", "")

    def fetch(self, url):
        self.downloads.append(url)
        if isinstance(self.fetch_value, Exception):
            raise self.fetch_value
        return self.fetch_value

    def registry(self, **options):
        return npm_registry(
            command=self.invoke,
            fetch=self.fetch,
            npm_program="/pinned/npm",
            attempts=1,
            sleep=self.sleeps.append,
            **options,
        )

    def dist(self, **extra):
        return {
            "tarball": "https://registry.npmjs.org/leaf/-/leaf.tgz",
            "integrity": "sha512-"
            + base64.b64encode(bytes.fromhex(sha512(LEAF.bytes))).decode(),
            **extra,
        }

    def test_public_registry_is_required_except_for_explicit_loopback_tests(self):
        for url in (
            "https://registry.example/",
            "http://registry.npmjs.org/",
            "https://registry.npmjs.org/path",
            "https://registry.npmjs.org/?other=1",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(ValueError, "public npm"),
            ):
                npm_registry(registry=url)
        for url in (
            "https://registry.npmjs.org",
            "http://127.0.0.1:1234/",
            "http://localhost:1234/",
            "http://[::1]:1234/",
        ):
            self.assertEqual(npm_registry(registry=url).registry, url.rstrip("/") + "/")

    def test_retry_parameters_are_positive_safe_integers(self):
        for options in (
            {"attempts": 0},
            {"attempts": -1},
            {"attempts": True},
            {"attempts": 1.5},
            {"attempts": 2**53},
            {"delay_ms": -1},
            {"delay_ms": False},
            {"delay_ms": 0.5},
            {"delay_ms": 2**53},
        ):
            with (
                self.subTest(options=options),
                self.assertRaisesRegex(ValueError, "retry options"),
            ):
                npm_registry(**options)

    def test_registry_constructor_does_not_resolve_optional_npm(self):
        with patch.object(
            publication,
            "npm_executable",
            side_effect=AssertionError("Node must remain optional"),
        ):
            npm_registry()
            npm_registry(command=self.invoke)

    def test_only_structured_e404_in_every_nonempty_stream_means_absent(self):
        valid = (
            E404,
            CommandResult(1, "", E404.stdout),
            CommandResult(1, E404.stdout, E404.stdout),
        )
        for value in valid:
            self.views = [value]
            self.assertIsNone(self.registry().lookup(LEAF))
        invalid = (
            "E404 synthetic-token",
            '{"error":{"code":"E401","message":"E404 synthetic-token"}}',
            "null",
            "[]",
            "{}",
            "",
        )
        for output in invalid:
            for value in (
                CommandResult(1, output, ""),
                CommandResult(1, E404.stdout, output or "plain failure"),
            ):
                self.views = [value]
                with (
                    self.subTest(output=output),
                    self.assertRaisesRegex(
                        ValueError, "Registry lookup failed"
                    ) as error,
                ):
                    self.registry().lookup(LEAF)
                self.assertNotIn("synthetic-token", str(error.exception))

    def test_native_npm_single_result_array_and_object_download_exact_bytes(self):
        for value in (self.dist(), [self.dist()]):
            self.views = [result(value)]
            self.assertEqual(self.registry().lookup(LEAF), sha512(LEAF.bytes))
        self.assertEqual(len(self.downloads), 2)
        for value in ([], [self.dist(), self.dist()]):
            self.views = [result(value)]
            with self.assertRaisesRegex(ValueError, "exactly one"):
                self.registry().lookup(LEAF)

    def test_successful_null_metadata_does_not_mean_version_absent(self):
        for value in (None, [None]):
            self.views = [result(value)]
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.registry().lookup(LEAF)
        self.assertEqual(self.downloads, [])

    def test_download_integrity_and_origin_are_checked_before_acceptance(self):
        self.views = [result(self.dist(integrity="wrong"))]
        with self.assertRaisesRegex(ValueError, "metadata disagrees"):
            self.registry().lookup(LEAF)
        self.downloads.clear()
        for url in (
            "https://evil.invalid/file",
            "http://registry.npmjs.org/file",
            "https://registry.npmjs.org:444/file",
            "https://registry.npmjs.org:0/file",
            "https://user:secret@registry.npmjs.org/file",
            "https://user@registry.npmjs.org/file",
            "/relative",
        ):
            self.views = [result(self.dist(tarball=url))]
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(ValueError, "unexpected origin"),
            ):
                self.registry().lookup(LEAF)
        self.assertEqual(self.downloads, [])

    def test_retries_wait_for_visibility_and_use_increasing_delays(self):
        self.views = [E404, E404, result(self.dist())]
        registry = npm_registry(
            command=self.invoke,
            fetch=self.fetch,
            npm_program="/pinned/npm",
            attempts=3,
            delay_ms=250,
            sleep=self.sleeps.append,
        )
        self.assertEqual(registry.lookup(LEAF, True), sha512(LEAF.bytes))
        self.assertEqual(self.sleeps, [0.25, 0.5])
        self.assertEqual(sum(args[0] == "config" for _, args, _ in self.calls), 1)
        self.views = [E404]
        self.assertIsNone(registry.lookup(LEAF))
        self.assertEqual(self.sleeps, [0.25, 0.5])

    def test_retries_preserve_the_last_failure(self):
        self.views = [OSError("first"), OSError("last")]
        registry = npm_registry(
            command=self.invoke,
            npm_program="/pinned/npm",
            attempts=2,
            delay_ms=0,
            sleep=self.sleeps.append,
        )
        with self.assertRaisesRegex(OSError, "last"):
            registry.lookup(LEAF)
        self.assertEqual(self.sleeps, [0])

    def test_configuration_failure_prevents_view_or_upload_and_hides_diagnostics(self):
        for reply in (
            CommandResult(1, "synthetic-token", "secret"),
            CommandResult(0, "https://wrong.invalid/", ""),
        ):
            invoke = Mock(return_value=reply)
            registry = npm_registry(
                command=invoke, npm_program="/pinned/npm", attempts=1
            )
            with self.assertRaisesRegex(
                ValueError, "configuration check failed"
            ) as error:
                registry.lookup(LEAF)
            self.assertEqual(invoke.call_count, 1)
            self.assertNotIn("synthetic-token", str(error.exception))

    def test_flags_pin_scoped_registry_saved_bundle_next_and_interactive_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pkg = replace(
                LEAF,
                name="@example/leaf",
                path=root / "leaf.tgz",
                bundle_path=root / "saved.json",
            )
            pkg.path.write_bytes(pkg.bytes)
            pkg.bundle_path.write_bytes(pkg.bundle_bytes)
            self.views = [E404]
            registry = self.registry()
            registry.lookup(pkg)
            registry.publish(pkg)
        self.assertEqual(
            [args[0] for _, args, _ in self.calls], ["config", "view", "publish"]
        )
        for program, args, interactive in self.calls:
            self.assertEqual(program, "/pinned/npm")
            self.assertIn("--@example:registry=https://registry.npmjs.org/", args)
            self.assertIn("--scope=", args)
            self.assertIn("--ignore-scripts", args)
            self.assertEqual(
                args[args.index("--registry") + 1], "https://registry.npmjs.org/"
            )
            self.assertEqual(args[args.index("--tag") + 1], "next")
            self.assertEqual(args[args.index("--access") + 1], "public")
            self.assertEqual(
                args[args.index("--provenance-file") + 1], str(pkg.bundle_path)
            )
            self.assertNotIn("--provenance=false", args)
            self.assertEqual(interactive, args[0] == "publish")
        self.assertIn("--loglevel=silent", self.calls[1][1])

    def test_frozen_archive_or_bundle_change_prevents_upload(self):
        for changed in ("archive", "bundle"):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                pkg = replace(
                    LEAF, path=root / "leaf.tgz", bundle_path=root / "saved.json"
                )
                pkg.path.write_bytes(b"changed" if changed == "archive" else pkg.bytes)
                pkg.bundle_path.write_bytes(
                    b"changed" if changed == "bundle" else pkg.bundle_bytes
                )
                self.calls.clear()
                with self.assertRaisesRegex(
                    ValueError, "Frozen publication files changed"
                ):
                    self.registry().publish(pkg)
                self.assertEqual([args[0] for _, args, _ in self.calls], ["config"])

    def test_failed_native_upload_has_bounded_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pkg = replace(LEAF, path=root / "leaf.tgz", bundle_path=root / "saved.json")
            pkg.path.write_bytes(pkg.bytes)
            pkg.bundle_path.write_bytes(pkg.bundle_bytes)
            invoke = Mock(
                side_effect=[
                    CommandResult(0, "https://registry.npmjs.org/", ""),
                    CommandResult(23, "synthetic-token", "secret"),
                ]
            )
            registry = npm_registry(command=invoke, npm_program="/pinned/npm")
            with self.assertRaisesRegex(
                ValueError, r"Package upload failed \(23\): leaf"
            ) as error:
                registry.publish(pkg)
            self.assertNotIn("synthetic-token", str(error.exception))

    def verification_views(self, *, predicate=PREDICATE_TYPE, tag="1.0.0"):
        return [
            result(
                {
                    "attestations": {
                        "url": "https://registry.npmjs.org/-/npm/v1/attestations/leaf",
                        "provenance": {"predicateType": predicate},
                    }
                }
            ),
            result({"next": tag}),
        ]

    def test_verify_requires_exact_saved_bundle_predicate_and_next_tag(self):
        pkg = replace(LEAF, bundle={"a": [1, {"b": True}], "z": "saved"})
        self.views = self.verification_views()
        self.fetch_value = json.dumps(
            {
                "attestations": [
                    {"predicateType": "other", "bundle": {}},
                    {
                        "predicateType": PREDICATE_TYPE,
                        "bundle": {"z": "saved", "a": [1.0, {"b": True}]},
                    },
                ]
            }
        ).encode()
        self.registry().verify(pkg)
        self.assertEqual([args[2] for _, args, _ in self.calls], ["dist", "dist-tags"])
        for bundle in (
            {},
            {"a": [1, {"b": 1}], "z": "saved"},
            {"a": [True, {"b": True}], "z": "saved"},
        ):
            self.views = self.verification_views()
            self.fetch_value = json.dumps(
                {"attestations": [{"predicateType": PREDICATE_TYPE, "bundle": bundle}]}
            ).encode()
            with (
                self.subTest(bundle=bundle),
                self.assertRaisesRegex(ValueError, "exact verified bundle"),
            ):
                self.registry().verify(pkg)
        self.views = self.verification_views(predicate="other")
        with self.assertRaisesRegex(ValueError, "provenance metadata missing"):
            self.registry().verify(pkg)
        self.views = self.verification_views(tag="2.0.0")
        self.fetch_value = json.dumps(
            {"attestations": [{"predicateType": PREDICATE_TYPE, "bundle": pkg.bundle}]}
        ).encode()
        with self.assertRaisesRegex(ValueError, "next tag does not select"):
            self.registry().verify(pkg)

    def test_verify_retries_registry_readback_until_exact_bundle_is_visible(self):
        self.views = (
            self.verification_views(predicate="pending")[:1] + self.verification_views()
        )
        self.fetch_value = json.dumps(
            {"attestations": [{"predicateType": PREDICATE_TYPE, "bundle": {}}]}
        ).encode()
        registry = npm_registry(
            command=self.invoke,
            fetch=self.fetch,
            npm_program="/pinned/npm",
            attempts=2,
            delay_ms=50,
            sleep=self.sleeps.append,
        )
        registry.verify(LEAF)
        self.assertEqual(self.sleeps, [0.05])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.program = shutil.which("curl")
        self.assertIsNotNone(self.program, "The pinned core shell must supply curl")
        environment = patch.dict(os.environ, {"CHAINMAN_ATTEST_CURL": self.program})
        environment.start()
        self.addCleanup(environment.stop)

    def test_native_download_rejects_redirects_and_http_errors(self):
        requests = []
        content = b"saved exact bytes\x00\xff\xfe200"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                requests.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/ok")
                elif self.path == "/error":
                    self.send_response(403)
                else:
                    self.send_response(200)
                self.end_headers()
                self.wfile.write(content)

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01))
            thread.start()
            try:
                origin = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual(publication._download(origin + "/ok"), content)
                for path, status in (("/redirect", 302), ("/error", 403)):
                    with (
                        self.subTest(path=path),
                        self.assertRaisesRegex(
                            ValueError, f"Registry download failed \\({status}\\)"
                        ),
                    ):
                        publication._download(origin + path)
                self.assertEqual(requests, ["/ok", "/redirect", "/error"])
            finally:
                server.shutdown()
                thread.join()

    def test_native_total_deadline_covers_trickling_headers_and_body(self):
        for phase in ("headers", "body"):
            writes = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass

                def do_GET(self):
                    try:
                        if phase == "headers":
                            self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Trickle: ")
                        else:
                            self.send_response(200)
                            self.send_header("Content-Length", "20")
                            self.end_headers()
                        for _ in range(20):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            writes.append(time.monotonic())
                            time.sleep(0.03)
                        if phase == "headers":
                            self.wfile.write(b"\r\nContent-Length: 4\r\n\r\nbody")
                    except OSError:
                        pass

            with (
                self.subTest(phase=phase),
                HTTPServer(("127.0.0.1", 0), Handler) as server,
                patch.object(publication, "_DOWNLOAD_TIMEOUT", 0.2),
            ):
                thread = Thread(target=lambda: server.serve_forever(poll_interval=0.01))
                thread.start()
                try:
                    started = time.monotonic()
                    with self.assertRaisesRegex(
                        TimeoutError, "Registry download timed out"
                    ):
                        publication._download(f"http://127.0.0.1:{server.server_port}/")
                    self.assertLess(time.monotonic() - started, 0.8)
                    self.assertGreater(len(writes), 1)
                    self.assertLess(len(writes), 20)
                finally:
                    server.shutdown()
                    thread.join()

    def test_private_body_and_remaining_deadline_are_shared_with_curl(self):
        paths = []
        url = "https://registry.npmjs.org/package[1].tgz?value={x}"
        content = b"binary\x00\xff200"

        def run(argv, **kwargs):
            body = Path(argv[argv.index("--output") + 1])
            self.assertEqual(body.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(body.stat().st_mode & 0o777, 0o600)
            self.assertEqual(argv[:2], [self.program, "--disable"])
            self.assertIn("--silent", argv)
            self.assertIn("--globoff", argv)
            self.assertEqual(argv[argv.index("--proto") + 1], "=http,https")
            self.assertEqual(argv[argv.index("--write-out") + 1], "%{http_code}")
            self.assertEqual(argv[argv.index("--url") + 1], url)
            self.assertFalse(
                any(
                    arg.startswith("--retry")
                    or arg
                    in {"--location", "--location-trusted", "-L", "--config", "-K"}
                    for arg in argv[1:]
                )
            )
            self.assertEqual(kwargs["timeout"], 24)
            self.assertEqual(
                float(argv[argv.index("--max-time") + 1]), kwargs["timeout"]
            )
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertFalse(kwargs.get("text", False))
            body.write_bytes(content)
            paths.append(body)
            return subprocess.CompletedProcess(argv, 0, stdout=b"200")

        with (
            patch.object(
                publication.time, "monotonic", side_effect=[100, 106, 107, 108]
            ),
            patch.object(publication.tc, "managed_run", side_effect=run),
        ):
            self.assertEqual(publication._download(url), content)
        self.assertFalse(paths[0].parent.exists())

    def test_setup_cannot_restart_an_expired_deadline(self):
        paths = []
        original = publication.tempfile.TemporaryDirectory

        def temporary(**kwargs):
            directory = original(**kwargs)
            paths.append(Path(directory.name))
            return directory

        with (
            patch.object(
                publication.tempfile, "TemporaryDirectory", side_effect=temporary
            ),
            patch.object(publication.time, "monotonic", side_effect=[100, 131]),
            patch.object(publication.tc, "managed_run") as run,
            self.assertRaisesRegex(TimeoutError, "Registry download timed out"),
        ):
            publication._download("https://registry.npmjs.org/package.tgz")
        run.assert_not_called()
        self.assertFalse(paths[0].exists())

    def test_timeout_or_failed_process_cannot_supply_http_success(self):
        for failure in ("subprocess", 28, 7):
            paths = []

            def run(argv, **kwargs):
                body = Path(argv[argv.index("--output") + 1])
                body.write_bytes(b"untrusted partial body")
                paths.append(body)
                if failure == "subprocess":
                    raise subprocess.TimeoutExpired(
                        argv, kwargs["timeout"], output=b"200"
                    )
                return subprocess.CompletedProcess(argv, failure, stdout=b"200")

            exception = ValueError if failure == 7 else TimeoutError
            message = "curl exit 7" if failure == 7 else "Registry download timed out"
            with (
                self.subTest(failure=failure),
                patch.object(publication.tc, "managed_run", side_effect=run),
                self.assertRaisesRegex(exception, message),
            ):
                publication._download("https://registry.npmjs.org/package.tgz")
            self.assertFalse(paths[0].parent.exists())

    def test_only_exact_ascii_success_status_is_accepted(self):
        for status in (b"20", b"200\n", b" 200", b"200200", b"\xff00", b"000", b"403"):
            with (
                self.subTest(status=status),
                patch.object(
                    publication.tc,
                    "managed_run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=status),
                ),
                self.assertRaisesRegex(ValueError, "Registry download failed"),
            ):
                publication._download("https://registry.npmjs.org/package.tgz")

    def test_success_returned_after_deadline_is_rejected(self):
        for instants in ([100, 101, 131], [100, 101, 102, 131]):
            with (
                self.subTest(instants=instants),
                patch.object(publication.time, "monotonic", side_effect=instants),
                patch.object(
                    publication.tc,
                    "managed_run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=b"200"),
                ),
                self.assertRaisesRegex(TimeoutError, "Registry download timed out"),
            ):
                publication._download("https://registry.npmjs.org/package.tgz")


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="publication snapshot ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def snapshot(self, fixture=None, inspected=None):
        return snapshot_packages(
            fixture or verified("leaf"),
            self.root,
            POLICY,
            lambda path: inspected or metadata(),
        )

    def test_snapshot_uses_verified_bytes_exclusive_readonly_files_and_native_metadata(
        self,
    ):
        pkg = self.snapshot(
            inspected=metadata(
                extra={
                    "dependencies": {"external": "1", "other": "*"},
                    "publishConfig": {
                        "tag": "latest",
                        "registry": "https://untrusted.invalid/",
                    },
                }
            )
        )[0]
        self.assertEqual(pkg.path.read_bytes(), b"leaf")
        self.assertEqual(pkg.bundle_path.read_bytes(), b'{"saved":true}')
        self.assertEqual(pkg.path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(pkg.bundle_path.stat().st_mode & 0o777, 0o400)
        self.assertEqual(pkg.dependencies, ("external", "other"))
        with self.assertRaises(FileExistsError):
            self.snapshot()

    def test_repository_compares_normalized_native_identity_and_preserves_case(self):
        for repository in (
            None,
            "github.com/Other/Package",
            "github.com/example/Package",
            "github.com/Example/package",
        ):
            directory = self.root / str(len(list(self.root.iterdir())))
            directory.mkdir()
            with (
                self.subTest(repository=repository),
                self.assertRaisesRegex(
                    ValueError, r"leaf@1\.0\.0.*https://github.com/Example/Package"
                ),
            ):
                snapshot_packages(
                    verified("leaf"),
                    directory,
                    POLICY,
                    lambda path: metadata(repository=repository),
                )

    def test_all_package_preflight_rejects_later_native_repository_override_or_provenance_before_lookup(
        self,
    ):
        for conflict in ("repository", "provenance"):
            directory = self.root / conflict
            directory.mkdir()
            fixture = verified("leaf", "parent")

            def inspector(path):
                if path.name == "0.tgz":
                    return metadata()
                if conflict == "repository":
                    return metadata(
                        "parent",
                        extra={
                            "repository": "github:Example/Package",
                            "repositories": [{"url": "github:Other/Package"}],
                        },
                        repository="github.com/Other/Package",
                    )
                return metadata("parent", extra={"publishConfig": {"provenance": True}})

            registry = Mock()
            with (
                self.subTest(conflict=conflict),
                self.assertRaisesRegex(
                    ValueError, "repository must match|publishConfig.provenance"
                ),
            ):
                publish(
                    snapshot_packages(fixture, directory, POLICY, inspector), registry
                )
            self.assertEqual(registry.mock_calls, [])

    def test_saved_provenance_conflict_is_rejected_for_every_package(self):
        with self.assertRaisesRegex(
            ValueError, "publishConfig.provenance.*saved provenance"
        ):
            self.snapshot(
                inspected=metadata(extra={"publishConfig": {"provenance": True}})
            )
        for index, config in enumerate(
            (
                None,
                {},
                {"provenance": False},
                {"provenance": None},
                {"provenance": "true"},
                {"provenance": "false"},
                {"provenance": 1},
                {"provenance": 0},
            )
        ):
            directory = self.root / str(index)
            directory.mkdir()
            self.assertEqual(
                len(
                    snapshot_packages(
                        verified("leaf"),
                        directory,
                        POLICY,
                        lambda path: metadata(extra={"publishConfig": config}),
                    )
                ),
                1,
            )

    def test_package_metadata_policy_rejects_private_extensions_tag_and_subject(self):
        invalid = (
            metadata(extra={"private": True}),
            metadata(extra={"private": {}}),
            metadata(extra={"private": []}),
            metadata(extra={"packageExtensions": None}),
            metadata(extra={"tag": "latest"}),
            metadata(extra={"tag": None}),
            metadata("other"),
        )
        for index, inspected in enumerate(invalid):
            directory = self.root / str(index)
            directory.mkdir()
            with (
                self.subTest(inspected=inspected),
                self.assertRaisesRegex(
                    ValueError, "manifest PURL or next publication policy"
                ),
            ):
                snapshot_packages(
                    verified("leaf"), directory, POLICY, lambda path: inspected
                )

    def test_snapshots_reject_invalid_metadata_and_dependency_graph_before_upload(self):
        for index, inspected in enumerate(
            (
                metadata(extra={"name": ""}),
                metadata(extra={"version": 1}),
                metadata(extra={"dependencies": []}),
                metadata(extra={"publishConfig": []}),
            )
        ):
            directory = self.root / str(index)
            directory.mkdir()
            with self.subTest(inspected=inspected), self.assertRaises(ValueError):
                snapshot_packages(
                    verified("leaf"), directory, POLICY, lambda path: inspected
                )
        directory = self.root / "cycle"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "cycle"):
            snapshot_packages(
                verified("leaf"),
                directory,
                POLICY,
                lambda path: metadata(extra={"dependencies": {"leaf": "*"}}),
            )
        directory = self.root / "duplicate"
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            snapshot_packages(
                verified("leaf", "leaf"), directory, POLICY, lambda path: metadata()
            )


class ManualPublicationTests(unittest.TestCase):
    def test_ci_and_missing_explicit_next_policy_fail_before_verification(self):
        with patch.object(publication, "verify_manifest") as verify:
            for variable in ("CI", "GITHUB_ACTIONS"):
                with (
                    self.subTest(variable=variable),
                    patch.dict(
                        os.environ, {"CI": "", "GITHUB_ACTIONS": "", variable: "false"}
                    ),
                    self.assertRaisesRegex(ValueError, "manual only"),
                ):
                    npm_publish("unused", "12", POLICY)
            for tag in (None, "latest"):
                with (
                    patch.dict(os.environ, {"CI": "", "GITHUB_ACTIONS": ""}),
                    self.assertRaisesRegex(ValueError, "explicit next"),
                ):
                    npm_publish("unused", "12", replace(POLICY, tag=tag))
            verify.assert_not_called()

    def test_verification_snapshot_and_registry_are_ordered_and_private_files_always_cleaned(
        self,
    ):
        for failure in (None, "snapshot", "registry", "publish"):
            observed = []
            fixture = SimpleNamespace(dispose=Mock())

            def snapshot(selected, directory, policy):
                self.assertIs(selected, fixture)
                self.assertEqual(policy, POLICY)
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                observed.append(directory)
                (directory / "frozen").write_bytes(b"snapshot")
                if failure == "snapshot":
                    raise ValueError("snapshot failure")
                return [LEAF]

            registry = Mock()
            with (
                patch.dict(os.environ, {"CI": "", "GITHUB_ACTIONS": ""}),
                patch.object(
                    publication, "verify_manifest", return_value=fixture
                ) as verify,
                patch.object(
                    publication, "snapshot_packages", side_effect=snapshot
                ) as snapshot_call,
                patch.object(
                    publication,
                    "npm_registry",
                    side_effect=ValueError("registry failure")
                    if failure == "registry"
                    else None,
                    return_value=registry,
                ) as registry_call,
                patch.object(
                    publication,
                    "publish",
                    side_effect=ValueError("publish failure")
                    if failure == "publish"
                    else None,
                ) as publish_call,
            ):
                if failure is None:
                    npm_publish("local.json", "12", POLICY)
                else:
                    with self.assertRaisesRegex(ValueError, failure + " failure"):
                        npm_publish("local.json", "12", POLICY)
                verify.assert_called_once_with("local.json", "12", POLICY)
                snapshot_call.assert_called_once()
                if failure == "snapshot":
                    registry_call.assert_not_called()
                    publish_call.assert_not_called()
                elif failure == "registry":
                    publish_call.assert_not_called()
                else:
                    publish_call.assert_called_once_with([LEAF], registry)
            fixture.dispose.assert_called_once()
            self.assertTrue(observed)
            self.assertFalse(observed[0].exists())


if __name__ == "__main__":
    unittest.main()
