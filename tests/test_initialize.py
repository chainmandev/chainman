"""Explicit adoption uses the selected templates and preserves Git failure output."""

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import initialize
import registry

SOURCE = Path(__file__).resolve().parents[1]


class InitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman init ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.destination = self.root / "new project"
        self.revision = "a" * 40
        self.release = registry.Release("v0.1.0", datetime.now(timezone.utc))
        runtime = self.root / "selected-runtime"
        for directory in ("bootstrap", "scripts", "nix", "template"):
            shutil.copytree(
                SOURCE / directory,
                runtime / directory,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        shutil.copy2(SOURCE / "VERSION", runtime / "VERSION")
        patches = [
            patch.object(initialize.git_runtime, "store", return_value=runtime),
            patch.object(registry, "github_releases", return_value=[self.release]),
            patch.object(
                initialize.chainman_updates,
                "published_revision",
                return_value=self.revision,
            ),
            patch.object(registry, "github_commit", return_value=self.revision),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def test_explicit_fresh_release_creates_minimal_git_consumer(self):
        result = initialize.initialize(self.destination, "v0.1.0")
        self.assertEqual(result["revision"], self.revision)
        self.assertEqual(
            (self.destination / "chainman.lock").read_text(), self.revision + "\n"
        )
        self.assertIn(
            (SOURCE / "bootstrap/chainman.just").read_bytes(),
            (self.destination / "justfile").read_bytes(),
        )
        self.assertFalse((self.destination / "scripts/chainman.sh").exists())
        self.assertFalse((self.destination / "scripts/chainman.just").exists())
        self.assertFalse(list(self.destination.rglob("*.tar.gz")))
        self.assertFalse((self.destination / "examples").exists())
        self.assertNotIn(
            "chainman", (self.destination / "flake.nix").read_text().lower()
        )
        self.assertEqual(self.mocks[2].call_args.args[1], {"minimum_age_days": 0})
        subprocess.run(
            [sys.executable, str(self.destination / "scripts/demo.py"), "--check"],
            check=True,
            capture_output=True,
        )

    def test_full_sha_does_not_query_release_metadata(self):
        initialize.initialize(self.destination, self.revision)
        self.mocks[1].assert_not_called()
        self.mocks[2].assert_not_called()

    def test_moved_tag_fails_before_generation(self):
        self.mocks[3].return_value = "b" * 40
        with self.assertRaisesRegex(ValueError, "tag changed"):
            initialize.initialize(self.destination, "0.1.0")
        self.assertFalse(self.destination.exists())

    def test_nonempty_destination_and_symlink_are_preserved(self):
        self.destination.mkdir()
        (self.destination / "keep").write_text("unchanged")
        with self.assertRaises(ValueError):
            initialize.initialize(self.destination, self.revision)
        self.assertEqual((self.destination / "keep").read_text(), "unchanged")
        link = self.root / "link"
        link.symlink_to(self.destination)
        with self.assertRaises(ValueError):
            initialize.initialize(link, self.revision)
        self.mocks[0].assert_not_called()

    def test_moving_and_malformed_selectors_are_rejected(self):
        for ref in ("main", "latest", "v0.1", "0.1.0-beta", "A" * 40, "a" * 39):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                initialize.initialize(self.destination, ref)
        self.mocks[0].assert_not_called()


class InitializationShellTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman host Git init ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        scripts = self.checkout / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(SOURCE / "scripts/init.sh", scripts / "init.sh")
        # Native Git is real; only generation/Nix entry is stubbed in this layer.
        # The generator and verified Git object tests cover that boundary separately.
        (scripts / "generate-fixture.py").write_text(
            'import pathlib,sys\np=pathlib.Path(sys.argv[-2]); p.mkdir(); (p/"README.md").write_text("generated\\n")\n'
        )
        (scripts / "enter.sh").write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(str(Path(sys.executable)))
            + " "
            + shlex.quote(str(scripts / "generate-fixture.py"))
            + ' "$@"\n'
        )
        (scripts / "enter.sh").chmod(0o755)
        self.destination = self.root / "new project"
        self.home = self.root / "home"
        self.home.mkdir()
        self.policy = self.home / ".gitconfig"
        self.policy.write_text(
            "[user]\n name = Starter author\n email = starter@example.invalid\n[init]\n defaultBranch = custom-default\n[commit]\n gpgsign = false\n"
        )
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "CHAINMAN_", "TOOLCHAIN_"))
        }
        self.env.update(HOME=str(self.home), CHAINMAN_MODE="host-nix")

    def run_init(self, *args):
        return subprocess.run(
            [
                str(self.checkout / "scripts/init.sh"),
                str(self.destination),
                "a" * 40,
                *args,
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_default_commit_uses_host_identity_and_branch_defaults(self):
        result = self.run_init()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.destination), "branch", "--show-current"],
                text=True,
            ).strip(),
            "custom-default",
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.destination), "log", "-1", "--format=%an <%ae>"],
                text=True,
            ).strip(),
            "Starter author <starter@example.invalid>",
        )
        self.assertIn("have not been run", result.stdout)

    def test_no_git_generates_files_only(self):
        result = self.run_init("--no-git")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.destination / "README.md").exists())
        self.assertFalse((self.destination / ".git").exists())

    def test_signing_failure_preserves_files_and_recovery_instructions(self):
        self.policy.write_text(
            self.policy.read_text()
            + "[commit]\n gpgsign = true\n[gpg]\n program = /nonexistent-chainman-fixture-signer\n"
        )
        result = self.run_init()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.destination / "README.md").exists())
        self.assertIn("Project files are preserved", result.stderr)
        self.assertIn("Fix Git identity/signing", result.stderr)

    def test_nonempty_destination_fails_before_starting_tools(self):
        self.destination.mkdir()
        (self.destination / "keep").write_text("unchanged")
        result = self.run_init()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.destination.iterdir()), [self.destination / "keep"])


if __name__ == "__main__":
    unittest.main()
