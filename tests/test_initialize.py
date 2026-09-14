"""Public initialization checks release evidence before creating a project."""

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import initialize
import package


class InitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman init test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "new project"
        self.version = "0.1.0"
        self.revision = "a" * 40
        self.published = datetime.now(timezone.utc) - timedelta(hours=1)
        files = {
            "bootstrap/chainman.sh": (b"#!/bin/sh\n", 0o755),
            "bootstrap/fetch.nix": (b"fixture", 0o644),
            "nix/flake.lock": (b"{}", 0o644),
            "dependencies.toml": (b"[nix]\n[docker]\n", 0o644),
            "template/chainman.toml": (
                b"schema=3\n[updates]\nminimum_age_days=30\n",
                0o644,
            ),
            "template/flake.nix": (b"fixture", 0o644),
        }
        runtime = {
            name: value
            for name, value in files.items()
            if not name.startswith("template/")
        }
        base = "https://github.com/chainmandev/chainman/releases/download/v0.1.0/"
        self.bodies = {
            "chainman-0.1.0.tar.gz": package.archive_bytes(runtime, self.version),
            "chainman-source-0.1.0.tar.gz": package.archive_bytes(files, self.version),
        }
        self.metadata = {
            "schema": 1,
            "version": self.version,
            "revision": self.revision,
            "url": base + "chainman-0.1.0.tar.gz",
            "narHash": "fixture",
            "archive_sha256": hashlib.sha256(
                self.bodies["chainman-0.1.0.tar.gz"]
            ).hexdigest(),
            "source": {
                "filename": "chainman-source-0.1.0.tar.gz",
                "url": base + "chainman-source-0.1.0.tar.gz",
                "narHash": "fixture",
                "archive_sha256": hashlib.sha256(
                    self.bodies["chainman-source-0.1.0.tar.gz"]
                ).hexdigest(),
            },
        }
        self.immutable = True
        for target, kwargs in (
            ("registry.data", {"side_effect": self.release}),
            ("registry.github_commit", {"return_value": self.revision}),
            ("source_updates.commit_time", {"return_value": self.published}),
            ("registry.fetch", {"side_effect": self.fetch}),
            ("example.subprocess.check_output", {"return_value": "fixture\n"}),
        ):
            context = patch(target, **kwargs)
            context.start()
            self.addCleanup(context.stop)

    def release(self, _url):
        self.bodies["chainman-release.json"] = json.dumps(self.metadata).encode()
        return {
            "tag_name": "v0.1.0",
            "draft": False,
            "prerelease": False,
            "immutable": self.immutable,
            "published_at": self.published.isoformat(),
            "assets": [
                {
                    "id": index,
                    "name": name,
                    "state": "uploaded",
                    "size": len(body),
                    "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "created_at": self.published.isoformat(),
                    "updated_at": self.published.isoformat(),
                }
                for index, (name, body) in enumerate(self.bodies.items(), 1)
            ],
        }

    def fetch(self, url, **_kwargs):
        return list(self.bodies.values())[int(url.rsplit("/", 1)[1]) - 1], {}

    def test_explicit_fresh_release_creates_url_only_consumer(self):
        initialize.initialize(self.destination, self.version)
        pin = json.loads((self.destination / "chainman.lock").read_text())
        self.assertEqual(pin["revision"], self.revision)
        self.assertNotIn("bundled_archive", pin)
        self.assertFalse((self.destination / "vendor").exists())
        self.assertIn(
            "minimum_age_days=30", (self.destination / "chainman.toml").read_text()
        )

    def test_mutable_or_inconsistent_release_cannot_create_destination(self):
        self.immutable = False
        with self.assertRaisesRegex(ValueError, "immutable"):
            initialize.initialize(self.destination, self.version)
        self.immutable = True
        self.metadata["revision"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "metadata"):
            initialize.initialize(self.destination, self.version)
        self.assertFalse(self.destination.exists())

    def test_corrupt_source_hash_cannot_create_destination(self):
        self.metadata["source"]["archive_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum"):
            initialize.initialize(self.destination, self.version)
        self.assertFalse(self.destination.exists())

    def test_nonempty_destination_and_symlink_are_preserved(self):
        self.destination.mkdir()
        sentinel = self.destination / "keep"
        sentinel.write_text("original")
        with self.assertRaisesRegex(ValueError, "empty"):
            initialize.initialize(self.destination, self.version)
        link = self.root / "link"
        link.symlink_to(self.destination)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            initialize.initialize(link, self.version)
        self.assertEqual(sentinel.read_text(), "original")

    def test_explicit_numeric_version_required(self):
        for version in ("latest", "main", "../0.1.0", "v0.1.0", "0.1.0;echo bad"):
            with (
                self.subTest(version=version),
                self.assertRaisesRegex(ValueError, "numeric"),
            ):
                initialize.initialize(self.destination, version)


class InitializationShellTests(unittest.TestCase):
    def test_nonempty_destination_fails_before_starting_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "keep").write_text("original")
            script = package.ROOT / "scripts/init.sh"
            result = subprocess.run(
                [str(script), str(root), "0.1.0"],
                env={**os.environ, "CHAINMAN_MODE": "container-nix"},
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("empty", result.stderr)
            self.assertEqual((root / "keep").read_text(), "original")


@unittest.skipUnless(
    os.environ.get("CHAINMAN_TEST_INIT"), "opt in with just init-test ENGINE"
)
class InitializationNativeTests(unittest.TestCase):
    def test_shell_initializes_and_runs_independent_url_only_starter(self):
        """Only HTTP transport is substituted; archives and Nix are real."""
        with tempfile.TemporaryDirectory(prefix="chainman native init ") as temporary:
            root = Path(temporary).resolve()
            source = root / "source with spaces"
            source.mkdir()
            inventory = json.loads((package.ROOT / "release-files.json").read_text())
            for name in inventory["files"]:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package.ROOT / name, target)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CHAINMAN_", "TOOLCHAIN_", "GIT_"))
                and key not in {"GITHUB_TOKEN", "GH_TOKEN"}
            }
            env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            with patch.dict(os.environ, env, clear=True):
                package.git(source, "init", "-b", "main")
                package.git(source, "config", "user.name", "Initialization Fixture")
                package.git(source, "config", "user.email", "fixture@example.invalid")
                package.git(source, "add", ".")
                package.git(source, "commit", "-m", "Disposable source fixture")
                metadata = package.release(source, root / "release")
            version, revision = metadata["version"], metadata["revision"]
            published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            release = {
                "tag_name": f"v{version}",
                "draft": False,
                "prerelease": False,
                "immutable": True,
                "published_at": published,
                "assets": [],
            }
            api = "https://api.github.com/repos/chainmandev/chainman"
            responses = {}
            for number, name in enumerate(
                (
                    "chainman-release.json",
                    f"chainman-{version}.tar.gz",
                    f"chainman-source-{version}.tar.gz",
                ),
                1,
            ):
                body = (root / "release" / name).read_bytes()
                release["assets"].append(
                    {
                        "id": number,
                        "name": name,
                        "state": "uploaded",
                        "size": len(body),
                        "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                        "created_at": published,
                        "updated_at": published,
                    }
                )
                responses[f"{api}/releases/assets/{number}"] = body
            for url, value in {
                f"{api}/releases/tags/v{version}": release,
                f"{api}/git/ref/tags/v{version}": {
                    "object": {"type": "commit", "sha": revision}
                },
                f"{api}/commits/{revision}": {
                    "sha": revision,
                    "commit": {"committer": {"date": published}},
                },
            }.items():
                responses[url] = json.dumps(value).encode()
            (source / "fixture-responses.json").write_text(
                json.dumps(
                    {
                        url: base64.b64encode(body).decode()
                        for url, body in responses.items()
                    }
                )
            )
            with (source / "scripts/registry.py").open("a") as stream:
                stream.write(
                    "\ndef _fetch(url, *args, **kwargs):\n"
                    "    from pathlib import Path\n"
                    "    responses = json.loads((Path(__file__).resolve().parents[1] / 'fixture-responses.json').read_text())\n"
                    "    return base64.b64decode(responses[url]), {}\n"
                )
            # The outer process has only OS utilities and the declared prerequisites.
            # In container mode even Nix is absent from that PATH.
            engine = os.environ["CHAINMAN_TEST_INIT"]
            binaries = root / "host utilities"
            binaries.mkdir()
            for name in (
                "sh",
                "dirname",
                "basename",
                "readlink",
                "uname",
                "cksum",
                "ls",
                "mkdir",
                "cat",
                "chmod",
                "cp",
                "cut",
                "tr",
                "sed",
                "grep",
                "awk",
                "head",
                "tail",
                "env",
                "id",
                "date",
                "sleep",
                "touch",
                "sort",
                "wc",
                "find",
                "xargs",
                "gzip",
                "tar",
                "mktemp",
                "rm",
                "rmdir",
                "mv",
                "git",
                "just",
                "nix" if engine == "host-nix" else engine,
            ):
                binary = shutil.which(name)
                self.assertIsNotNone(binary, name)
                (binaries / name).symlink_to(binary)
            env["PATH"] = str(binaries)
            env["CHAINMAN_MODE"] = (
                "host-nix" if engine == "host-nix" else "container-nix"
            )
            if engine != "host-nix":
                env["CHAINMAN_CONTAINER_ENGINE"] = engine
            destination = root / "new project with spaces"
            result = subprocess.run(
                [
                    str(binaries / "just"),
                    "--justfile",
                    str(source / "justfile"),
                    "init",
                    str(destination),
                    version,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((destination / "vendor").exists())
            pin = json.loads((destination / "chainman.lock").read_text())
            self.assertNotIn("bundled_archive", pin)
            self.assertEqual(pin["revision"], revision)
            self.assertEqual((destination / "chainman.lock").stat().st_uid, os.getuid())
            # Remove the source to prove that ordinary consumer commands do not use it.
            shutil.rmtree(source)
            env["CHAINMAN_ARCHIVE"] = str(
                root / "release" / f"chainman-{version}.tar.gz"
            )
            for arguments in (
                ["git", "init", "-b", "main"],
                ["git", "add", "."],
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-m",
                    "Adopt fixture",
                ],
                ["just", "setup"],
                ["just", "exec", "python3", "examples/core/greeting.py"],
                ["just", "config", "validate"],
                ["just", "verify"],
            ):
                result = subprocess.run(
                    arguments,
                    cwd=destination,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=destination, env=env
                ),
                b"",
            )


if __name__ == "__main__":
    unittest.main()
