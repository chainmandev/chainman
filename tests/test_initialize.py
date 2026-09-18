"""Explicit adoption uses the selected templates and preserves Git failure output."""

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

SOURCE = Path(__file__).resolve().parents[1]


class InitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman init ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.destination = self.root / "new project"
        self.revision = "a" * 40
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
            patch.object(
                initialize.git_runtime, "default_revision", return_value=self.revision
            ),
        ]
        self.mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)

    def test_default_branch_creates_minimal_git_consumer(self):
        result = initialize.initialize(self.destination)
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
        git = ["git", "-C", str(self.destination)]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run(
            [
                *git,
                "-c",
                "core.autocrlf=true",
                "add",
                ".gitattributes",
                "chainman.lock",
            ],
            check=True,
        )
        (self.destination / "chainman.lock").unlink()
        subprocess.run(
            [*git, "-c", "core.autocrlf=true", "checkout-index", "chainman.lock"],
            check=True,
        )
        self.assertEqual(
            (self.destination / "chainman.lock").read_bytes(),
            (self.revision + "\n").encode(),
        )
        self.mocks[1].assert_called_once_with()
        subprocess.run(
            [sys.executable, str(self.destination / "scripts/demo.py"), "--check"],
            check=True,
            capture_output=True,
        )

    def test_full_sha_does_not_discover_default_branch(self):
        initialize.initialize(self.destination, self.revision)
        self.mocks[1].assert_not_called()

    def test_branch_movement_during_initialization_keeps_snapshot(self):
        self.mocks[1].side_effect = [self.revision, "b" * 40]
        result = initialize.initialize(self.destination)
        self.assertEqual(result["revision"], self.revision)
        self.mocks[1].assert_called_once_with()

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
        for ref in (
            "main",
            "master",
            "HEAD",
            "latest",
            "0.1.0",
            "v0.1.0",
            "v0.1",
            "0.1.0-beta",
            "A" * 40,
            "a" * 39,
        ):
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
            'import pathlib,sys\np=pathlib.Path(sys.argv[sys.argv.index(next(a for a in sys.argv if a.endswith("/initialize.py")))+1]); p.mkdir(); (p/"README.md").write_text("generated\\n")\n'
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
        result = self.run_init("a" * 40, "--no-git")
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
