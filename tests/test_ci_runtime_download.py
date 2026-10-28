"""Authenticate artifact provenance and bytes without forwarding credentials."""

import copy
import hashlib
from http.client import HTTPMessage
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_runtime_download as download


CANDIDATE = "a" * 40
TOKEN = "synthetic-ci-token"


def bundle(payload=b"reviewed runtime", name="runtime.nar"):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return result.getvalue()


def redirect(url):
    headers = HTTPMessage()
    headers["Location"] = url
    return HTTPError(
        "https://api.github.com/original", 302, "redirect", headers, io.BytesIO()
    )


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(reply)


class RuntimeDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime download ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "ready"
        self.payload = b"reviewed runtime"
        self.zip = bundle(self.payload)
        self.nar_digest = hashlib.sha256(self.payload).hexdigest()
        self.zip_digest = hashlib.sha256(self.zip).hexdigest()
        self.metadata = {
            "id": 7,
            "expired": False,
            "digest": "sha256:" + self.zip_digest,
            "size_in_bytes": len(self.zip),
            "workflow_run": {"id": 42, "head_sha": CANDIDATE},
        }
        environment = patch.dict(
            os.environ,
            {
                "GITHUB_REPOSITORY": "chainmandev/chainman",
                "GITHUB_RUN_ID": "42",
                "GH_TOKEN": TOKEN,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        source = patch.object(download.closure, "check_source")
        self.source = source.start()
        self.addCleanup(source.stop)

    def run_download(self, opener):
        with patch.object(download, "build_opener", return_value=opener):
            download.download_runtime(
                self.destination, CANDIDATE, "7", self.nar_digest, self.zip_digest
            )

    def test_exact_run_transfer_drops_authorization_on_storage_redirect(self):
        opener = Opener(
            [
                json.dumps(self.metadata).encode(),
                redirect("https://storage.example/runtime?signature=fixture"),
                self.zip,
            ]
        )
        self.run_download(opener)
        self.assertEqual((self.destination / "runtime.nar").read_bytes(), self.payload)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["ready"])
        self.assertEqual(
            [r.get_header("Authorization") for r in opener.requests],
            ["Bearer " + TOKEN, "Bearer " + TOKEN, None],
        )
        self.assertEqual(self.source.call_count, 2)

    def test_wrong_provenance_expiry_size_or_digest_stops_before_archive_download(self):
        changes = [
            {"id": 8},
            {"id": True},
            {"expired": True},
            {"digest": None},
            {"size_in_bytes": True},
            {"size_in_bytes": 0},
            {"size_in_bytes": download.MAX_ZIP_BYTES + 1},
            {"workflow_run": {"id": 41, "head_sha": CANDIDATE}},
            {"workflow_run": {"id": 42, "head_sha": "b" * 40}},
            {"workflow_run": None},
        ]
        for changed in changes:
            with self.subTest(changed=changed):
                metadata = {**copy.deepcopy(self.metadata), **changed}
                opener = Opener([json.dumps(metadata).encode()])
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    self.run_download(opener)
                self.assertEqual(len(opener.requests), 1)
                self.assertFalse(self.destination.exists())

    def test_missing_outputs_or_invalid_context_never_make_network_requests(self):
        for artifact, nar, archive in [
            ("", self.nar_digest, self.zip_digest),
            ("7", "", self.zip_digest),
            ("7", self.nar_digest, ""),
        ]:
            with self.subTest(artifact=artifact, nar=nar, archive=archive):
                with patch.object(download, "metadata") as metadata:
                    with self.assertRaisesRegex(ValueError, "context"):
                        download.download_runtime(
                            self.destination, CANDIDATE, artifact, nar, archive
                        )
                    metadata.assert_not_called()
        for key, value in [
            ("GITHUB_RUN_ID", ""),
            ("GITHUB_REPOSITORY", "https://attacker.invalid/repo"),
            ("GH_TOKEN", "bad\nheader"),
        ]:
            with self.subTest(key=key):
                with (
                    patch.dict(os.environ, {key: value}),
                    patch.object(download, "metadata") as metadata,
                ):
                    with self.assertRaisesRegex(ValueError, "context"):
                        download.download_runtime(
                            self.destination,
                            CANDIDATE,
                            "7",
                            self.nar_digest,
                            self.zip_digest,
                        )
                    metadata.assert_not_called()

    def test_source_mismatch_or_post_download_edit_never_publishes_archive(self):
        for failures in (
            [ValueError("wrong candidate")],
            [None, ValueError("dirty checkout")],
        ):
            with self.subTest(failures=failures):
                self.source.side_effect = failures
                opener = Opener([json.dumps(self.metadata).encode(), self.zip])
                with self.assertRaises(ValueError):
                    self.run_download(opener)
                self.assertFalse(self.destination.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_unsafe_redirects_are_rejected_before_any_second_request(self):
        for target in [
            "http://storage.example/runtime",
            "file:///etc/passwd",
            "https://user:password@storage.example/runtime",
            "/relative",
        ]:
            with self.subTest(target=target):
                opener = Opener([redirect(target)])
                with patch.object(download, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(ValueError, "unsafe"):
                        download.download_zip(
                            "https://api.github.com/artifact/zip",
                            TOKEN,
                            self.root / "zip",
                            len(self.zip),
                            self.zip_digest,
                        )
                self.assertEqual(len(opener.requests), 1)

    def test_redirect_limit_and_http_errors_do_not_expose_credentials(self):
        opener = Opener([redirect("https://storage.example/runtime")] * 5)
        with patch.object(download, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "too many"):
                download.download_zip(
                    "https://api.github.com/artifact/zip",
                    TOKEN,
                    self.root / "zip",
                    1,
                    self.zip_digest,
                )
        self.assertTrue(
            all(r.get_header("Authorization") is None for r in opener.requests[1:])
        )
        error = HTTPError(
            "https://api.github.com/artifact",
            403,
            TOKEN,
            HTTPMessage(),
            io.BytesIO(TOKEN.encode()),
        )
        with patch.object(download, "build_opener", return_value=Opener([error])):
            with self.assertRaises(ValueError) as caught:
                download.metadata("https://api.github.com/artifact", TOKEN)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_zip_hash_and_size_checked_before_extraction(self):
        for data in [self.zip[:-1], self.zip + b"extra", b"x" * len(self.zip)]:
            with self.subTest(size=len(data)):
                opener = Opener([json.dumps(self.metadata).encode(), data])
                with patch.object(download, "extract_runtime") as extract:
                    with self.assertRaisesRegex(ValueError, "size|SHA-256"):
                        self.run_download(opener)
                    extract.assert_not_called()
                self.assertFalse(self.destination.exists())
                self.assertEqual(list(self.root.iterdir()), [])

    def test_only_one_regular_named_archive_member_is_accepted(self):
        archives = [bundle(name="../runtime.nar"), bundle(name="directory/runtime.nar")]
        multiple = io.BytesIO()
        with zipfile.ZipFile(multiple, "w") as archive:
            archive.writestr("runtime.nar", self.payload)
            archive.writestr("other", "unexpected")
        archives.append(multiple.getvalue())
        linked = io.BytesIO()
        with zipfile.ZipFile(linked, "w") as archive:
            info = zipfile.ZipInfo("runtime.nar")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")
        archives.append(linked.getvalue())
        for data in archives:
            with self.subTest(data=data[:12]):
                path = self.root / "artifact.zip"
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "member"):
                    download.extract_runtime(
                        path, self.root / "runtime.nar", self.nar_digest
                    )
                self.assertFalse((self.root / "runtime.nar").exists())

    def test_nar_digest_failure_cleans_download_without_import(self):
        self.nar_digest = "0" * 64
        opener = Opener([json.dumps(self.metadata).encode(), self.zip])
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.run_download(opener)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
