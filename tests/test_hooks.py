"""Disposable hook ownership/setup tests, with no real pushes or app builds."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman
import hooks
import staged_format
import trojan_source


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hook project ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = patch.dict(
            os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.root / "chainman.lock").write_text("a" * 40 + "\n")
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[hooks]
enabled=true
[setup.first]
inputs=["input"]
artifacts=["ready"]
commands=[["sh","-c","printf ready > ready"]]
[tasks.extension]
commands=[["sh","-c","test -f ready && printf done > extended"]]
[recipes]
setup=["extension"]
""")
        (self.root / "input").write_text("input")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")

    def git(self, *args):
        return staged_format.git(self.root, *args).stdout.decode().strip()

    def test_idempotent_install_status_uninstall(self):
        with contextlib.redirect_stdout(io.StringIO()):
            hooks.install(self.root)
            hooks.install(self.root)
            self.assertTrue(hooks.status(self.root)["installed"])
            hooks.uninstall(self.root)
        self.assertFalse(hooks.status(self.root)["installed"])

    def test_existing_manager_and_default_hook_are_preserved(self):
        self.git("config", "core.hooksPath", "my-hooks")
        with self.assertRaisesRegex(ValueError, "managed at"):
            hooks.install(self.root)
        self.assertEqual(
            self.git("config", "--get", "core.hooksPath"), "/dev/null"
        )  # helper itself overrides hooks
        self.git("config", "--unset", "core.hooksPath")
        existing = self.root / ".git/hooks/pre-push"
        existing.write_text("custom\n")
        with self.assertRaisesRegex(ValueError, "Existing Git hooks"):
            hooks.install(self.root)
        self.assertEqual(existing.read_text(), "custom\n")

    def test_modified_bridge_is_never_removed(self):
        hooks.install(self.root)
        path = hooks.directory(self.root) / "pre-commit"
        path.write_text("custom")
        with self.assertRaisesRegex(ValueError, "Modified hook"):
            hooks.uninstall(self.root)
        self.assertEqual(path.read_text(), "custom")

    def test_linked_worktree_install_does_not_reconfigure_other_checkout(self):
        other = self.root / "linked"
        self.git("worktree", "add", "-qb", "other", str(other))
        hooks.install(other)
        self.assertTrue(hooks.status(other)["installed"])
        self.assertFalse(hooks.status(self.root)["installed"])

    def test_setup_is_complete_raw_and_recipe_no_hooks_is_explicit(self):
        calls = []

        def install(root, args):
            self.assertTrue((root / "ready").is_file())
            self.assertTrue((root / "extended").is_file())
            calls.append(args)
            return 0

        with patch.object(hooks, "execute", install):
            self.assertEqual(chainman.main(["--root", str(self.root), "setup"]), 0)
            self.assertEqual(calls, [["install"]])
            self.assertEqual(
                chainman.main(["--root", str(self.root), "setup", "--no-hooks"]), 0
            )
            self.assertEqual(
                chainman.main(["--root", str(self.root), "setup", "first"]), 0
            )
            self.assertEqual(len(calls), 1)

    def test_effective_preset_has_only_formatting_at_pre_commit(self):
        path = hooks.effective(self.root, self.root / "generated")
        config = json.loads(path.read_text())
        self.assertEqual(set(config["pre-commit"]["commands"]), {"format-staged"})
        self.assertEqual(set(config["pre-push"]["commands"]), {"trojan-source"})

    def test_outgoing_intermediate_multi_ref_deletion_and_missing_base(self):
        base = self.git("rev-parse", "HEAD")
        (self.root / "source.ts").write_text("first")
        self.git("add", ".")
        self.git("commit", "-qm", "First")
        first = self.git("rev-parse", "HEAD")
        (self.root / "source.ts").write_text("second")
        self.git("add", ".")
        self.git("commit", "-qm", "Second")
        tip = self.git("rev-parse", "HEAD")
        zero = "0" * 40
        records = f"refs/heads/main {tip} refs/heads/main {base}\n(delete) {zero} refs/heads/old {base}\nrefs/heads/alias {tip} refs/heads/alias {base}\n".encode()
        self.assertEqual(set(trojan_source.outgoing(self.root, records)), {first, tip})
        with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
            all_history = trojan_source.outgoing(
                self.root, f"x {tip} x {'f' * 40}\n".encode()
            )
        self.assertIn(base, all_history)
        self.assertIn("unavailable", diagnostic.getvalue())

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TROJAN_SOURCE"), "requires pinned hooks profile"
    )
    def test_default_scan_covers_consumer_languages_and_executable_scripts(self):
        paths = [
            "main.dart",
            "page.astro",
            "flake.nix",
            "justfile",
            "nested/Justfile",
            "Dockerfile",
            "shell/bin/entry",
            "template.rs.j2",
        ]
        for name in paths:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# harmless Unicode fixture \u202e\n")
        (self.root / "shell/bin/entry").chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "Language coverage fixture")
        revision = self.git("rev-parse", "HEAD")

        def execute(root, name, argv, **kwargs):
            return subprocess.run(
                argv, input=kwargs.get("input"), capture_output=True, check=True
            )

        with patch.object(chainman, "execute", execute):
            with contextlib.redirect_stderr(io.StringIO()) as diagnostic:
                with self.assertRaisesRegex(ValueError, "suspicious"):
                    trojan_source.run(self.root, [revision])
            for name in paths:
                self.assertIn(repr(name), diagnostic.getvalue())
            config = self.root / "chainman.toml"
            config.write_text(
                config.read_text() + '\n[hooks.trojan_source]\npaths=["*.ts"]\n'
            )
            self.assertEqual(trojan_source.run(self.root, [revision]), 0)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TROJAN_SOURCE"), "requires pinned hooks profile"
    )
    def test_real_upstream_scanner_checks_committed_content_and_exact_exception(self):
        (self.root / "source.ts").write_text("// harmless fixture \u202e\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        revision = self.git("rev-parse", "HEAD")
        (self.root / "source.ts").write_text("// clean working tree\n")

        def execute(root, name, argv, **kwargs):
            return subprocess.run(
                argv, input=kwargs.get("input"), capture_output=True, check=True
            )

        with patch.object(chainman, "execute", execute):
            with self.assertRaisesRegex(ValueError, "suspicious"):
                trojan_source.run(self.root, [revision])
            blob = self.git("rev-parse", "HEAD:source.ts")
            config = self.root / "chainman.toml"
            config.write_text(
                config.read_text()
                + f'\n[[hooks.trojan_source.exceptions]]\npath="source.ts"\nblob="{blob}"\nreason="Harmless scanner fixture"\n'
            )
            self.assertEqual(trojan_source.run(self.root, [revision]), 0)


if __name__ == "__main__":
    unittest.main()
