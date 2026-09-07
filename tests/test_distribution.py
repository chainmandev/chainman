"""Distribution identity and extraction behavior independently of the launcher."""

import io
from pathlib import Path
import sys
import tarfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import example
import package


class DistributionTests(unittest.TestCase):
    def test_deterministic_source_archive_preserves_bytes_and_executable_modes(self):
        files = {
            "dir with spaces/run.sh": (b"#!/bin/sh\nprintf 'hi\\n'\n", 0o755),
            "VERSION": (b"1.2.3\n", 0o644),
        }
        left = package.archive_bytes(files, "1.2.3")
        right = package.archive_bytes(dict(reversed(list(files.items()))), "1.2.3")
        self.assertEqual(left, right)
        self.assertEqual(example.read_archive(left, "1.2.3"), files)

    def test_unsafe_paths_and_modes_are_rejected(self):
        for name, mode in (
            ("../outside", 0o644),
            ("/outside", 0o644),
            (".git/config", 0o644),
            ("ok", 0o777),
        ):
            with self.subTest(name=name, mode=mode), self.assertRaises(ValueError):
                package.archive_bytes({name: (b"x", mode)}, "1.0.0")

    def test_archive_links_are_rejected(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            member = tarfile.TarInfo("chainman-1.0.0/link")
            member.type, member.linkname = tarfile.SYMTYPE, "/outside"
            archive.addfile(member)
        with self.assertRaises(ValueError):
            example.read_archive(buffer.getvalue(), "1.0.0")

    def test_version_mismatch_is_not_an_adoptable_release(self):
        body = package.archive_bytes({"VERSION": (b"1.2.3", 0o644)}, "1.2.3")
        with self.assertRaises(ValueError):
            example.read_archive(body, "1.2.4")


if __name__ == "__main__":
    unittest.main()
