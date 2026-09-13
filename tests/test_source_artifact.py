"""Artifact identity, bounded origin evidence, and redirect negative controls."""

import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_artifact as artifacts
import source_artifacts as adapter

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)
URL = "https://downloads.example.org/tool/1.2.3/tool.tar.gz"
BODY = b"independently pinned example artifact"
DIGEST = "sha256:" + hashlib.sha256(BODY).hexdigest()


class Response(BytesIO):
    status = 200

    def __init__(self, body=BODY, **headers):
        super().__init__(body)
        self.headers = {
            "Last-Modified": "Wed, 01 Jul 2026 12:00:00 GMT",
            "Content-Length": str(len(body)),
            **headers,
        }

    def geturl(self):
        return URL


class ArtifactTests(unittest.TestCase):
    def network(self, addresses, responses=None):
        """Exercise the real urllib/HTTP path over an inert socket transport."""
        dns_patch = patch("socket.getaddrinfo", side_effect=addresses)
        dns = dns_patch.start()
        self.addCleanup(dns_patch.stop)
        sockets_patch = patch("socket.socket")
        sockets = sockets_patch.start()
        self.addCleanup(sockets_patch.stop)
        raw = sockets.return_value
        if responses is None:
            responses = [
                b"HTTP/1.1 200 OK\r\nLast-Modified: Wed, 01 Jul 2026 12:00:00 GMT\r\n"
                + f"Content-Length: {len(BODY)}\r\n\r\n".encode()
                + BODY
            ]
        raw.makefile.side_effect = [BytesIO(response) for response in responses]
        tls_patch = patch.object(
            ssl.SSLContext,
            "wrap_socket",
            autospec=True,
            side_effect=lambda context, connected, **kwargs: connected,
        )
        tls = tls_patch.start()
        self.addCleanup(tls_patch.stop)
        return dns, sockets, raw, tls

    @staticmethod
    def address(ip):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]

    def test_private_dns_answers_never_open_a_socket(self):
        _, sockets, _, _ = self.network([self.address("127.0.0.1")])
        with self.assertRaisesRegex(ValueError, "public"):
            artifacts.inspect(URL, DIGEST, NOW)
        sockets.assert_not_called()

    def test_public_dns_is_pinned_and_ambient_proxies_are_ignored(self):
        dns, _, raw, tls = self.network(
            [self.address("93.184.215.14"), self.address("127.0.0.1")]
        )
        with patch.dict(
            os.environ,
            {
                "https_proxy": "http://proxy-user:proxy-secret@127.0.0.1:8080",
                "HTTPS_PROXY": "http://proxy-user:proxy-secret@127.0.0.1:8080",
                "no_proxy": "",
                "NO_PROXY": "",
            },
        ):
            result = artifacts.inspect(URL, DIGEST, NOW)
        self.assertEqual(result["digest"], DIGEST)
        dns.assert_called_once_with(
            "downloads.example.org",
            443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        raw.connect.assert_called_once_with(("93.184.215.14", 443))
        self.assertEqual(
            tls.call_args.kwargs["server_hostname"], "downloads.example.org"
        )
        context = tls.call_args.args[0]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        sent = b"".join(call.args[0] for call in raw.sendall.call_args_list)
        self.assertIn(b"Host: downloads.example.org\r\n", sent)
        self.assertNotIn(b"proxy-secret", sent)
        self.assertNotIn(b"CONNECT ", sent)

    def test_mixed_empty_and_nonpublic_dns_answers_fail_before_connect(self):
        for private in ("10.0.0.1", "169.254.169.254", "224.0.0.1", "0.0.0.0"):
            with (
                self.subTest(private=private),
                patch(
                    "socket.getaddrinfo",
                    return_value=(
                        self.address("93.184.215.14") + self.address(private)
                    ),
                ),
                patch("socket.socket") as sockets,
                self.assertRaisesRegex(ValueError, "public"),
            ):
                try:
                    artifacts.public_connection(("downloads.example.org", 443), 60)
                finally:
                    sockets.assert_not_called()
        for address in ("::1", "fe80::1", "fec0::1", "ff02::1", "::ffff:127.0.0.1"):
            with (
                self.subTest(ipv6=address),
                patch(
                    "socket.getaddrinfo",
                    return_value=[
                        (
                            socket.AF_INET6,
                            socket.SOCK_STREAM,
                            socket.IPPROTO_TCP,
                            "",
                            (address, 443, 0, 0),
                        )
                    ],
                ),
                patch("socket.socket") as sockets,
                self.assertRaisesRegex(ValueError, "public"),
            ):
                try:
                    artifacts.public_connection(("downloads.example.org", 443), 60)
                finally:
                    sockets.assert_not_called()
        with (
            patch("socket.getaddrinfo", return_value=[]),
            patch("socket.socket") as sockets,
            self.assertRaisesRegex(ValueError, "public"),
        ):
            artifacts.public_connection(("downloads.example.org", 443), 60)
        sockets.assert_not_called()

    def test_redirect_dns_is_checked_before_the_next_socket(self):
        dns, sockets, _, _ = self.network(
            [self.address("93.184.215.14"), self.address("127.0.0.1")],
            [
                b"HTTP/1.1 302 Found\r\nLocation: https://other.example.org/tool/v1/file\r\nContent-Length: 0\r\n\r\n"
            ],
        )
        with self.assertRaisesRegex(ValueError, "public"):
            artifacts.inspect(URL, DIGEST, NOW)
        self.assertEqual(
            [call.args[0] for call in dns.call_args_list],
            ["downloads.example.org", "other.example.org"],
        )
        self.assertEqual(sockets.call_count, 1)

    def test_signed_cdn_transport_preserves_tls_host_and_redacts_query(self):
        origin = "https://github.com/example/tool/releases/download/v1.2.3/tool.tar.gz"
        cdn = "https://release-assets.githubusercontent.com/object?sig=transport-token"
        dns, _, _, tls = self.network(
            [self.address("93.184.215.14"), self.address("93.184.215.15")],
            [
                f"HTTP/1.1 302 Found\r\nLocation: {cdn}\r\nContent-Length: 0\r\n\r\n".encode(),
                b"HTTP/1.1 200 OK\r\nLast-Modified: Wed, 01 Jul 2026 12:00:00 GMT\r\n"
                + f"Content-Length: {len(BODY)}\r\n\r\n".encode()
                + BODY,
            ],
        )
        result = artifacts.inspect(origin, DIGEST, NOW)
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(
            [call.kwargs["server_hostname"] for call in tls.call_args_list],
            ["github.com", "release-assets.githubusercontent.com"],
        )
        self.assertEqual(result["resolved_url"], cdn.split("?")[0])
        self.assertNotIn("transport-token", json.dumps(result))

    def test_failed_tls_is_redacted_without_an_unverified_fallback(self):
        dns, _, raw, tls = self.network([self.address("93.184.215.14")])
        tls.side_effect = ssl.SSLCertVerificationError(
            "transport-token: wrong hostname"
        )
        with self.assertRaisesRegex(
            ValueError, "^Public artifact origin is unavailable$"
        ):
            artifacts.inspect(URL, DIGEST, NOW)
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(tls.call_count, 1)
        raw.sendall.assert_not_called()
        raw.close.assert_called()

    def test_public_ipv6_and_tcp_failover_use_only_the_validated_answer(self):
        answers = [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("2606:4700:4700:0000:0000:0000:0000:1111", 443, 0, 0),
            ),
            *self.address("93.184.215.14"),
        ]
        with (
            patch("socket.getaddrinfo", return_value=answers) as dns,
            patch("socket.socket") as sockets,
        ):
            raw = sockets.return_value
            raw.connect.side_effect = [OSError("unreachable"), None]
            self.assertIs(
                artifacts.public_connection(("downloads.example.org", 443), 17), raw
            )
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(
            [call.args[0] for call in raw.connect.call_args_list],
            [
                ("2606:4700:4700::1111", 443, 0, 0),
                ("93.184.215.14", 443),
            ],
        )
        self.assertEqual(raw.close.call_count, 1)
        raw.settimeout.assert_called_with(17)

    def test_proxy_tunnels_and_dns_failure_cannot_fall_back(self):
        connection = artifacts.PublicHTTPSConnection("downloads.example.org")
        connection.set_tunnel("other.example.org", 443)
        with (
            patch("socket.getaddrinfo") as dns,
            self.assertRaisesRegex(ValueError, "proxy tunnels"),
        ):
            connection.connect()
        dns.assert_not_called()
        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("secret origin")),
            patch("socket.socket") as sockets,
            self.assertRaisesRegex(
                ValueError, "^Public artifact origin is unavailable$"
            ),
        ):
            artifacts.inspect(URL, DIGEST, NOW)
        sockets.assert_not_called()

    def result(self, response, policy=None):
        with patch.object(artifacts, "build_opener") as factory:
            factory.return_value.open.return_value = response
            return artifacts.audit(URL, DIGEST, policy or {}, NOW)

    def test_hash_and_dated_artifact_evidence(self):
        result = self.result(Response())
        self.assertEqual(result["digest"], DIGEST)
        self.assertEqual(result["age_basis"], "origin-artifact-last-modified")
        self.assertEqual(result["size"], len(BODY))

    def test_changed_bytes_fail_even_when_old_date_is_retained(self):
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.result(Response(b"a mutable upstream replacement"))

    def test_missing_future_and_young_evidence_fail(self):
        for value in (
            None,
            "Wed, 01 Jul 2027 12:00:00 GMT",
            "Sun, 06 Sep 2026 12:00:00 GMT",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.result(Response(**{"Last-Modified": value}))

    def test_bounded_partial_and_transformed_downloads_fail(self):
        for headers in (
            {"Content-Length": str(artifacts.MAX_BYTES + 1)},
            {"Content-Length": str(len(BODY) + 1)},
            {"Content-Encoding": "gzip"},
        ):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.result(Response(**headers))
        with (
            patch.object(artifacts, "MAX_BYTES", 2),
            self.assertRaisesRegex(ValueError, "bounded"),
        ):
            self.result(Response(**{"Content-Length": None}))

    def test_alias_credential_private_and_redirect_urls_fail(self):
        for url in (
            "http://downloads.example.org/a",
            "https://user:secret@downloads.example.org/a",
            "https://downloads.example.org/latest/a",
            "https://downloads.example.org/a?token=secret",
            "https://127.0.0.1/a",
            "https://localhost/a",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                artifacts.artifact_url(url)
        with self.assertRaises(ValueError):
            artifacts.PublicRedirects().redirect_request(
                Request(URL), None, 302, "Found", {}, "http://downloads.example.org/a"
            )

    def test_explicit_large_limit_is_bounded_and_invalid_values_fail(self):
        for value in (True, 0, -1, "large", artifacts.MAX_DECLARED_BYTES + 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "limit"):
                artifacts.inspect(URL, DIGEST, NOW, max_bytes=value)
        with patch.object(artifacts, "build_opener") as factory:
            factory.return_value.open.return_value = Response()
            self.assertEqual(
                artifacts.inspect(URL, DIGEST, NOW, max_bytes=4 * artifacts.MAX_BYTES)[
                    "size"
                ],
                len(BODY),
            )

    def test_signed_cdn_only_follows_versioned_github_release_origin(self):
        origin = "https://github.com/example/tool/releases/download/v1.2.3/tool.tar.gz"
        cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/object?sig=transport-token"
        accepted = artifacts.PublicRedirects(origin).redirect_request(
            Request(origin), None, 302, "Found", {}, cdn
        )
        self.assertEqual(accepted.full_url, cdn)
        for source, target in (
            (URL, cdn),
            (
                origin,
                cdn.replace(
                    "release-assets.githubusercontent.com", "downloads.example.org"
                ),
            ),
            (origin, cdn.replace("https://", "https://user:secret@")),
        ):
            with (
                self.subTest(source=source, target=target),
                self.assertRaises(ValueError),
            ):
                artifacts.PublicRedirects(source).redirect_request(
                    Request(source), None, 302, "Found", {}, target
                )
        response = Response()
        response.geturl = lambda: cdn
        with patch.object(artifacts, "build_opener") as factory:
            factory.return_value.open.return_value = response
            result = artifacts.inspect(origin, DIGEST, NOW)
        self.assertEqual(result["resolved_url"], cdn.split("?")[0])
        self.assertNotIn("transport-token", json.dumps(result))

    def test_artifact_adapter_preserves_baseline_and_freezes_selected_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "sources.json"
            path.write_text(json.dumps({"sdk": {"url": URL, "digest": DIGEST}}))
            spec = {
                "adapter": "artifact",
                "entries": [{"file": "sources.json", "pointer": ["sdk"]}],
            }
            before = adapter.snapshot(root, spec)
            with patch.object(artifacts, "audit") as checked:
                before["resolution"] = adapter.resolve(root, spec, {}, NOW)
                adapter.audit(root, spec, before, {}, NOW)
                self.assertEqual(checked.call_count, 2)
            path.write_text(
                json.dumps(
                    {"sdk": {"url": URL.replace("1.2.3", "1.2.4"), "digest": DIGEST}}
                )
            )
            with self.assertRaisesRegex(ValueError, "selected artifact identity"):
                adapter.audit(root, spec, before, {}, NOW)

    def test_artifact_adapter_does_not_repair_missing_evidence_or_escape_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "sources.json"
            original = json.dumps({"sdk": {"url": URL, "digest": DIGEST}})
            path.write_text(original)
            spec = {
                "adapter": "artifact",
                "entries": [{"file": "sources.json", "pointer": ["sdk"]}],
            }
            with (
                patch.object(
                    artifacts, "audit", side_effect=ValueError("missing date")
                ),
                self.assertRaisesRegex(ValueError, "missing date"),
            ):
                adapter.resolve(root, spec, {}, NOW)
            self.assertEqual(path.read_text(), original)
            spec["entries"][0]["file"] = "../outside.json"
            with self.assertRaises(ValueError):
                adapter.snapshot(root, spec)


if __name__ == "__main__":
    unittest.main()
