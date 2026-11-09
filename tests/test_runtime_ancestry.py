"""Default runtime updates use complete verified ancestry, independent of caches."""

from contextlib import chdir
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import git_runtime as subject


class RuntimeAncestryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime ancestry fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.origin = self.root / "upstream"
        self.origin.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Runtime ancestry fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.a = self.commit("first")
        self.b = self.commit("second")
        self.c = self.commit("third")
        self.git("checkout", "--quiet", "-b", "side", self.b)
        self.d = self.commit("side")
        self.git("checkout", "--quiet", "--orphan", "unrelated")
        self.e = self.commit("unrelated")
        self.git("checkout", "--quiet", "main")
        repository = patch.object(subject, "REPOSITORY", str(self.origin))
        repository.start()
        self.addCleanup(repository.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(
            [
                "git",
                "-C",
                str(cwd or self.origin),
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, value):
        (self.origin / "VERSION").write_text(value + "\n")
        self.git("add", "VERSION")
        self.git("-c", "commit.gpgsign=false", "commit", "--quiet", "-m", value)
        return self.git("rev-parse", "HEAD")

    def test_default_updates_advance_or_retain_according_to_git_ancestry(self):
        self.assertEqual(subject.update_revision(self.b, self.c), self.c)
        self.assertEqual(subject.update_revision(self.c, self.b), self.c)
        self.assertEqual(subject.update_revision(self.a, self.c), self.c)

    def test_identical_snapshots_need_no_fetch(self):
        with patch.object(
            subject, "git", side_effect=AssertionError("unchanged pin queried")
        ):
            self.assertEqual(subject.update_revision(self.c, self.c), self.c)

    def test_divergent_and_unrelated_histories_require_explicit_reconciliation(self):
        for candidate in (self.d, self.e):
            with (
                self.subTest(candidate=candidate),
                self.assertRaisesRegex(
                    ValueError, "diverged.*explicit runtime revision"
                ),
            ):
                subject.update_revision(self.c, candidate)

    def test_missing_and_shallow_history_cannot_decide_ancestry(self):
        with self.assertRaisesRegex(ValueError, "complete Git ancestry"):
            subject.update_revision(self.c, "f" * 40)
        shallow = self.root / "shallow.git"
        self.git(
            "clone",
            "--quiet",
            "--bare",
            "--depth=1",
            self.origin.as_uri(),
            str(shallow),
        )
        with (
            patch.object(subject, "REPOSITORY", str(shallow)),
            self.assertRaisesRegex(ValueError, "complete Git"),
        ):
            subject.update_revision(self.b, self.c)

    def test_cache_grafts_replacements_and_configuration_are_not_ancestry_authority(
        self,
    ):
        home = self.root / "cache"
        cache = (
            home / "chainman/git/github.com-chainmandev-chainman" / (self.c + ".git")
        )
        cache.parent.mkdir(parents=True)
        self.git("clone", "--quiet", "--bare", str(self.origin), str(cache))
        (cache / "refs/replace").mkdir(parents=True)
        (cache / "refs/replace" / self.c).write_text(self.e + "\n")
        (cache / "info/grafts").write_text(self.c + "\n")
        (cache / "shallow").write_text(self.c + "\n")
        with (cache / "config").open("a") as stream:
            stream.write(
                f'\n[url "file:///untrusted-cache-source/"]\n    insteadOf = {self.origin}\n'
            )
        configuration = {
            "XDG_CACHE_HOME": str(home),
            "GIT_DIR": str(cache),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.file:///injected-source/.insteadOf",
            "GIT_CONFIG_VALUE_0": str(self.origin),
        }
        with patch.dict(os.environ, configuration):
            self.assertEqual(subject.update_revision(self.c, self.b), self.c)

    def test_default_advertisement_ignores_consumer_repository_configuration(self):
        consumer = self.root / "consumer"
        consumer.mkdir()
        self.git("init", "--quiet", cwd=consumer)
        self.git(
            "config",
            "url.file:///untrusted-consumer/.insteadOf",
            str(self.origin),
            cwd=consumer,
        )
        with chdir(consumer):
            self.assertEqual(subject.default_revision(), self.c)


if __name__ == "__main__":
    unittest.main()
