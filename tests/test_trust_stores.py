"""Inspect effective runtime trust stores, including embedded client bundles."""

import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_runtime
import swift_sources
import updates

# SHA-256 of the exact DER roots covered by the NixOS incident mitigation.
BLOCKED = {
    "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
    "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    "125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f",
    "bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3",
}


class TrustStoreTests(unittest.TestCase):
    def test_git_isolation_preserves_trust_without_routing_or_credentials(self):
        bundle = os.environ["SSL_CERT_FILE"]
        with patch.dict(
            os.environ,
            {
                "GIT_SSL_CAINFO": bundle,
                "GIT_DIR": "/unrelated/repository",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": "synthetic credential fixture",
            },
        ):

            def check(environment):
                self.assertEqual(environment["GIT_SSL_CAINFO"], bundle)
                self.assertNotIn("GIT_DIR", environment)
                self.assertNotIn("synthetic credential fixture", environment.values())

            with updates.preview_git_environment():
                check(os.environ)
            check(swift_sources.git_env())
            with patch.object(
                git_runtime.tc,
                "managed_run",
                return_value=subprocess.CompletedProcess([], 0, b""),
            ) as run:
                git_runtime.git("--version")
                check(run.call_args.kwargs["env"])

    def test_python_and_download_clients_share_filtered_store(self):
        context = ssl.create_default_context()
        roots = {
            hashlib.sha256(der).hexdigest()
            for der in context.get_ca_certs(binary_form=True)
        }
        self.assertGreater(len(roots), 100)
        self.assertFalse(roots & BLOCKED)
        bundle = os.environ["SSL_CERT_FILE"]
        self.assertEqual(ssl.get_default_verify_paths().cafile, bundle)
        self.assertNotIn("BEGIN TRUSTED CERTIFICATE", Path(bundle).read_text())
        for key in (
            "NIX_SSL_CERT_FILE",
            "CURL_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE",
            "GIT_SSL_CAINFO",
            "CARGO_HTTP_CAINFO",
            "NPM_CONFIG_CAFILE",
            "npm_config_cafile",
        ):
            self.assertEqual(os.environ[key], bundle, key)
        self.assertEqual(list(Path(os.environ["SSL_CERT_DIR"]).iterdir()), [])

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_TRUST_NODE") == "1", "native Node trust lane"
    )
    def test_node_embedded_and_effective_configuration(self):
        bundle = subprocess.check_output(
            ["npm", "config", "get", "cafile"], text=True
        ).strip()
        self.assertEqual(bundle, os.environ["SSL_CERT_FILE"])
        result = json.loads(
            subprocess.check_output(
                [
                    "node",
                    "-e",
                    """
const tls = require('node:tls'), {X509Certificate} = require('node:crypto');
console.log(JSON.stringify({version: process.versions.node,
  bundled: tls.getCACertificates('bundled').map(c => new X509Certificate(c).fingerprint256),
  effective: tls.getCACertificates('default').map(c => new X509Certificate(c).fingerprint256)}));
""",
                ],
                text=True,
            )
        )
        self.assertGreaterEqual(
            tuple(map(int, result["version"].split("."))), (26, 12, 1)
        )
        self.assertGreater(len(result["bundled"]), 100)
        for store in ("bundled", "effective"):
            self.assertFalse(
                {value.replace(":", "").lower() for value in result[store]} & BLOCKED,
                store,
            )
        # Node's API does not enumerate OpenSSL's store. The Python check above
        # independently loads the exact configured file and empty CA directory.
        self.assertIn("--use-openssl-ca", os.environ["NODE_OPTIONS"].split())

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_TRUST_NODE") == "1", "native pnpm trust lane"
    )
    def test_pnpm_runs_on_fixed_node_with_managed_trust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hook = root / "inspect.cjs"
            result = root / "trust.json"
            hook.write_text(
                "const fs = require('node:fs'), tls = require('node:tls');\n"
                "const {X509Certificate} = require('node:crypto');\n"
                "fs.writeFileSync(process.env.CHAINMAN_TRUST_RESULT, JSON.stringify({\n"
                "version: process.versions.node, options: process.env.NODE_OPTIONS,\n"
                "bundle: process.env.SSL_CERT_FILE,\n"
                "roots: tls.getCACertificates('bundled').map(c => "
                "new X509Certificate(c).fingerprint256)}));\n"
            )
            environment = dict(os.environ)
            environment["CHAINMAN_TRUST_RESULT"] = str(result)
            environment["NODE_OPTIONS"] += " --require " + json.dumps(str(hook))
            version = subprocess.check_output(
                ["pnpm", "--version"], env=environment, text=True
            ).strip()
            self.assertEqual(version, "11.27.0")
            inspected = json.loads(result.read_text())
            self.assertGreaterEqual(
                tuple(map(int, inspected["version"].split("."))), (26, 12, 1)
            )
            self.assertIn("--use-openssl-ca", inspected["options"].split())
            self.assertEqual(inspected["bundle"], os.environ["SSL_CERT_FILE"])
            self.assertGreater(len(inspected["roots"]), 100)
            self.assertFalse(
                {value.replace(":", "").lower() for value in inspected["roots"]}
                & BLOCKED
            )

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_TRUST_UV") == "1", "native uv trust lane"
    )
    def test_uv_fixed_release_uses_explicit_custom_store(self):
        output = subprocess.check_output(["uv", "--version"], text=True)
        version = re.match(r"uv (\d+)\.(\d+)\.(\d+)", output)
        self.assertIsNotNone(version)
        self.assertGreaterEqual(tuple(map(int, version.groups())), (0, 13, 4))
        # uv's nonempty SSL_CERT_FILE/DIR replace its bundled/native trust source.
        self.assertTrue(Path(os.environ["SSL_CERT_FILE"]).is_file())
        self.assertEqual(os.environ["UV_SYSTEM_CERTS"], "true")


if __name__ == "__main__":
    unittest.main()
