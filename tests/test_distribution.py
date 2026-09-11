"""Distribution identity and extraction behavior independently of the launcher."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import example
import package


class DistributionTests(unittest.TestCase):
    def test_example_keeps_one_runtime_implementation_and_its_own_sdk_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = {
                "bootstrap/chainman.sh": (b"#!/bin/sh\n", 0o755),
                "bootstrap/fetch.nix": (b"verified fetcher", 0o644),
                "nix/flake.lock": (b"consumer SDK lock", 0o644),
                "nix/flake.nix": (b"shared SDK implementation", 0o644),
                "nix/control/main.go": (b"shared controller", 0o644),
                "dependencies.toml": (b"[nix]\n[docker]\n", 0o644),
                "template/flake.nix": (b"import verified runtime", 0o644),
            }
            runtime = {
                name: item
                for name, item in source.items()
                if not name.startswith("template/")
            }
            body = package.archive_bytes(runtime, "1.0.0")
            source_body = package.archive_bytes(source, "1.0.0")
            # Exercise archive verification/extraction without spawning Nix.
            # Real host/container evaluation is an independent qualification.
            metadata = {
                "schema": 1,
                "version": "1.0.0",
                "revision": "a" * 40,
                "url": "https://example.invalid/runtime.tar.gz",
                "narHash": "digest",
                "archive_sha256": hashlib.sha256(body).hexdigest(),
                "source": {
                    "filename": "chainman-source-1.0.0.tar.gz",
                    "narHash": "digest",
                    "archive_sha256": hashlib.sha256(source_body).hexdigest(),
                },
            }
            (root / "chainman-1.0.0.tar.gz").write_bytes(body)
            (root / metadata["source"]["filename"]).write_bytes(source_body)
            manifest = root / "chainman-release.json"
            manifest.write_text(json.dumps(metadata))
            destination = root / "consumer"
            with patch.object(
                example.subprocess, "check_output", return_value="digest\n"
            ):
                example.create(destination, manifest)
            self.assertFalse((destination / "nix").exists())
            self.assertEqual(
                (destination / "flake.lock").read_bytes(), b"consumer SDK lock"
            )
            self.assertEqual(
                (destination / "flake.nix").read_bytes(), b"import verified runtime"
            )
            self.assertEqual(
                (destination / "vendor/chainman/chainman.tar.gz").read_bytes(), body
            )
            self.assertEqual(
                example.tomlkit.parse((destination / "dependencies.toml").read_text())[
                    "nix"
                ]["directory"],
                ".",
            )

    def test_release_reads_one_immutable_commit_when_head_moves(self):
        self.release_revision_case(replace=False)

    def test_release_ignores_replacement_refs_added_after_revision_capture(self):
        self.release_revision_case(replace=True)

    def test_runtime_archive_excludes_authoring_inputs(self):
        self.release_revision_case(replace=False, split=True)

    def release_revision_case(self, *, replace, split=False):
        with (
            tempfile.TemporaryDirectory(prefix="release source ") as temporary,
            patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_COUNT": "0",
                },
            ),
        ):
            root = Path(temporary) / "source"
            root.mkdir()
            git = package.git
            git(root, "init", "-b", "main")
            git(root, "config", "user.name", "Release Test")
            git(root, "config", "user.email", "release@example.invalid")
            git(root, "config", "commit.gpgsign", "false")
            git(root, "config", "core.hooksPath", os.devnull)
            (root / "VERSION").write_text("1.0.0\n")
            source = root / "input[one].sh"
            source.write_text("original bytes\n")
            source.chmod(0o644)
            inventory = root / "release-files.json"
            inventory.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "files": ["VERSION", source.name],
                        **({"runtime_files": ["VERSION"]} if split else {}),
                    }
                )
            )
            git(root, "add", ".")
            git(root, "commit", "-m", "original")
            original = git(root, "rev-parse", "HEAD").decode().strip()
            source.write_text("later bytes\n")
            source.chmod(0o755)
            (root / "unapproved.txt").write_text("later inventory input")
            inventory.write_text(
                json.dumps(
                    {"schema": 1, "files": ["VERSION", source.name, "unapproved.txt"]}
                )
            )
            git(root, "add", ".")
            git(root, "commit", "-m", "later")
            later = git(root, "rev-parse", "HEAD").decode().strip()
            git(root, "checkout", "--detach", original)

            def advancing_git(directory, *args):
                result = git(directory, *args)
                if args == ("rev-parse", "HEAD"):
                    if replace:
                        git(root, "replace", original, later)
                    else:
                        git(root, "checkout", "--detach", later)
                return result

            output = Path(temporary) / "release"
            with patch.object(package, "git", side_effect=advancing_git):
                metadata = package.release(root, output)
            self.assertEqual(metadata["revision"], original)
            self.assertEqual(
                git(root, "rev-parse", "HEAD").decode().strip(),
                original if replace else later,
            )
            self.assertEqual(
                example.read_archive(
                    (output / "chainman-1.0.0.tar.gz").read_bytes(), "1.0.0"
                ),
                {
                    "VERSION": (b"1.0.0\n", 0o644),
                    **({} if split else {source.name: (b"original bytes\n", 0o644)}),
                },
            )
            self.assertEqual(
                example.read_archive(
                    (output / metadata["source"]["filename"]).read_bytes(), "1.0.0"
                ),
                {
                    "VERSION": (b"1.0.0\n", 0o644),
                    source.name: (b"original bytes\n", 0o644),
                },
            )

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
