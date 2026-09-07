"""Artifact identity, bounded origin evidence, and redirect negative controls."""

import hashlib
import sys
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import source_artifact as artifacts

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


if __name__ == "__main__":
    unittest.main()
