"""The SDK process keeps runner stdout separate from private structured replies."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import attestation_bridge as bridge
from attestation_common import parse
from attestation_model import Artifact


class AttestationBridgeTests(unittest.TestCase):
    def test_json_boundary_rejects_non_json_numeric_constants(self):
        for value in ("NaN", "Infinity", "-Infinity", '{"field": NaN}'):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "Invalid JSON constant"),
            ):
                parse(value)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="attestation bridge ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.program = self.root / "adapter"
        self.program.write_text("fixture\n")
        self.environment = patch.dict(
            os.environ, {"CHAINMAN_ATTEST_ADAPTER": str(self.program)}
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_signing_preserves_stdout_and_private_response_cleanup(self):
        paths = []
        artifact = Artifact("package.tgz", "pkg:npm/example@1.0.0", "a" * 128)

        def run(argv, **kwargs):
            self.assertEqual(argv[:2], [str(self.program), "--response-file"])
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            self.assertNotIn("capture_output", kwargs)
            request = json.loads(kwargs["input"])
            self.assertEqual(
                request["subject"],
                {"name": artifact.subject, "digest": {"sha512": artifact.sha512}},
            )
            self.assertNotIn("token", request)
            path = Path(argv[-1])
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            path.write_text(
                json.dumps(
                    {
                        "protocol": 1,
                        "operation": "attest-provenance",
                        "bundle": {"dsseEnvelope": {}},
                    }
                )
            )
            path.chmod(0o600)
            paths.append(path)
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(bridge.tc, "managed_run", side_effect=run):
            self.assertEqual(bridge.attest_provenance(artifact), {"dsseEnvelope": {}})
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].parent.exists())

    def test_failed_process_cannot_supply_a_success_response(self):
        paths = []

        def run(argv, **kwargs):
            path = Path(argv[-1])
            path.write_text(
                '{"protocol":1,"operation":"npm-inspect","manifest":{},"subject":"package","repository":null}'
            )
            path.chmod(0o600)
            paths.append(path)
            return subprocess.CompletedProcess(argv, 7)

        with (
            patch.object(bridge.tc, "managed_run", side_effect=run),
            self.assertRaisesRegex(ValueError, "failed.*7"),
        ):
            bridge.inspect_package(self.root / "package.tgz")
        self.assertFalse(paths[0].parent.exists())

    def test_malformed_or_unprotected_response_is_rejected(self):
        for response, mode in (
            ("not json", 0o600),
            ("[]", 0o600),
            ('{"protocol":true,"operation":"npm-inspect"}', 0o600),
            ('{"protocol":1,"operation":"attest-provenance"}', 0o600),
            ('{"protocol":1,"operation":"npm-inspect"}', 0o644),
            (
                '{"protocol":1,"operation":"npm-inspect","manifest":{},"subject":"p","repository":null,"extra":true}',
                0o600,
            ),
        ):
            paths = []

            def run(argv, **kwargs):
                path = Path(argv[-1])
                path.write_text(response)
                path.chmod(mode)
                paths.append(path)
                return subprocess.CompletedProcess(argv, 0)

            with (
                self.subTest(response=response, mode=mode),
                patch.object(bridge.tc, "managed_run", side_effect=run),
                self.assertRaises(ValueError),
            ):
                bridge.inspect_package(self.root / "package.tgz")
            self.assertFalse(paths[0].parent.exists())

    def test_symlink_and_oversized_response_are_rejected(self):
        target = self.root / "target"
        target.write_text("{}")
        target.chmod(0o600)
        for kind in ("symlink", "oversized"):

            def run(argv, **kwargs):
                path = Path(argv[-1])
                if kind == "symlink":
                    path.symlink_to(target)
                else:
                    with path.open("wb") as stream:
                        stream.truncate(bridge.MAX_RESPONSE + 1)
                    path.chmod(0o600)
                return subprocess.CompletedProcess(argv, 0)

            with (
                self.subTest(kind=kind),
                patch.object(bridge.tc, "managed_run", side_effect=run),
                self.assertRaises((OSError, ValueError)),
            ):
                bridge.inspect_package(self.root / "package.tgz")
        self.assertEqual(target.read_text(), "{}")

    def test_no_path_fallback_for_pinned_tools(self):
        for variable in (
            "CHAINMAN_ATTEST_ADAPTER",
            "CHAINMAN_ATTEST_NPM",
            "CHAINMAN_ATTEST_CURL",
        ):
            for value in ("", "npm", str(self.root / "missing")):
                with (
                    self.subTest(variable=variable, value=value),
                    patch.dict(os.environ, {variable: value}),
                    self.assertRaisesRegex(ValueError, "pinned attest profile"),
                ):
                    bridge.executable(variable)


if __name__ == "__main__":
    unittest.main()
