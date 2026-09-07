"""Artifact identity, bounded origin evidence, and redirect negative controls."""

import hashlib
import json
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
            root = Path(temporary)
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
            root = Path(temporary)
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
