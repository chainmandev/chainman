"""Tag identity refresh bypasses cached metadata, including annotated tag hops."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry


class GitReleaseTransportTests(unittest.TestCase):
    def setUp(self):
        registry.fetch.cache_clear()
        self.addCleanup(registry.fetch.cache_clear)
        self.api = "https://api.github.com/repos/chainmandev/chainman"
        self.ref = self.api + "/git/ref/tags/v0.1.0"
        self.original = "a" * 40
        self.changed = "b" * 40
        self.requests = []
        self.moved = False
        self.annotated = False
        self.failure = None

    def transport(self, request, timeout):
        self.assertEqual(timeout, 30)
        url = request.full_url
        self.requests.append((url, request.get_header("Cache-control")))
        if self.failure == "http":
            error = HTTPError(url, 403, "denied", {}, io.BytesIO(b"denied"))
            self.addCleanup(error.close)
            raise error
        if self.failure == "transport":
            raise URLError("unavailable")
        revision = self.changed if self.moved else self.original
        if url == self.ref:
            obj = (
                {"type": "tag", "sha": "c" * 40}
                if self.annotated
                else {"type": "commit", "sha": revision}
            )
        elif url == self.api + "/git/tags/" + "c" * 40:
            obj = {"type": "tag", "sha": "d" * 40}
        elif url == self.api + "/git/tags/" + "d" * 40:
            obj = {"type": "commit", "sha": revision}
        else:
            self.fail("Unexpected URL: " + url)
        response = io.BytesIO(json.dumps({"object": obj}).encode())
        response.headers = {"Content-Type": "application/json"}
        return response

    def resolve(self, *, fresh=False):
        return registry.github_commit("chainmandev/chainman", "v0.1.0", fresh=fresh)

    def test_refresh_observes_moved_lightweight_and_annotated_tags(self):
        for annotated in (False, True):
            with (
                self.subTest(annotated=annotated),
                patch.object(registry, "urlopen", side_effect=self.transport),
            ):
                registry.fetch.cache_clear()
                self.requests.clear()
                self.annotated, self.moved = annotated, False
                self.assertEqual(self.resolve(), self.original)
                count = len(self.requests)
                self.moved = True
                self.assertEqual(self.resolve(), self.original)
                self.assertEqual(len(self.requests), count)
                self.assertEqual(self.resolve(fresh=True), self.changed)
                self.assertEqual(len(self.requests), count * 2)
                self.assertTrue(
                    all(header == "no-cache" for _, header in self.requests[count:])
                )

    def test_refresh_failure_never_falls_back_to_cached_identity(self):
        with (
            patch.object(registry, "urlopen", side_effect=self.transport),
            patch.object(registry.time, "sleep"),
        ):
            self.assertEqual(self.resolve(), self.original)
            self.failure = "http"
            with self.assertRaisesRegex(registry.RegistryHTTPError, "HTTP 403"):
                self.resolve(fresh=True)
            self.failure = "transport"
            with self.assertRaisesRegex(ValueError, "Registry unavailable"):
                self.resolve(fresh=True)
