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

    def bare_worktrees(self):
        bare = self.root / "bare.git"
        self.git("clone", "--bare", str(self.root), str(bare))
        linked = self.root / "linked"
        sibling = self.root / "sibling"
        for path in (linked, sibling):
            self.git(
                "--git-dir=" + str(bare), "worktree", "add", "-b", path.name, str(path)
            )
        return bare, linked, sibling

    def test_bare_repository_and_all_linked_worktrees_keep_their_identity(self):
        bare, linked, sibling = self.bare_worktrees()
        hooks.install(linked)
        hooks.install(linked)
        self.assertTrue(hooks.status(linked)["installed"])
        self.assertFalse(hooks.status(sibling)["installed"])
        self.assertEqual(
            self.git("--git-dir=" + str(bare), "rev-parse", "--is-bare-repository"),
            "true",
        )
        for path in (linked, sibling):
            self.assertEqual(
                staged_format.git(
                    path, "rev-parse", "--is-bare-repository"
                ).stdout.strip(),
                b"false",
            )
            staged_format.git(path, "status", "--porcelain")
        later = self.root / "later"
        self.git("--git-dir=" + str(bare), "worktree", "add", "-b", "later", str(later))
        staged_format.git(later, "status", "--porcelain")
        hooks.uninstall(linked)
        staged_format.git(linked, "status", "--porcelain")

    def test_shared_worktree_location_stays_with_primary_checkout(self):
        self.git("config", "core.worktree", str(self.root))
        primary = self.root / ".git/config.worktree"
        primary.write_text("# preserved settings\n[custom]\n\tvalue = kept\n")
        other = self.root / "linked"
        self.git("worktree", "add", "-qb", "other", str(other))
        hooks.install(other)
        self.assertEqual(self.git("rev-parse", "--show-toplevel"), str(self.root))
        self.assertEqual(
            staged_format.git(other, "rev-parse", "--show-toplevel")
            .stdout.decode()
            .strip(),
            str(other),
        )
        self.assertEqual(
            self.git("config", "--worktree", "--get", "custom.value"), "kept"
        )
        self.assertIn("# preserved settings", primary.read_text())
        self.assertFalse(hooks.status(self.root)["installed"])

    def test_failed_or_interrupted_install_restores_configuration_and_bridges(self):
        bare, linked, sibling = self.bare_worktrees()
        target = hooks.directory(linked)
        paths = [
            bare / "config",
            bare / "config.worktree",
            target.parent / "config.worktree",
            *(target / event for event in hooks.EVENTS),
        ]
        before = {path: staged_format.identity(path) for path in paths}
        with patch.object(hooks, "status", return_value={"installed": False}):
            with self.assertRaisesRegex(ValueError, "did not select"):
                hooks.install(linked)
        self.assertEqual({path: staged_format.identity(path) for path in paths}, before)
        atomic = hooks.tc.atomic_bytes

        def interrupt(path, *args):
            if path == bare / "config" and not getattr(interrupt, "raised", False):
                interrupt.raised = True
                raise KeyboardInterrupt()
            return atomic(path, *args)

        with patch.object(hooks.tc, "atomic_bytes", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                hooks.install(linked)
        self.assertEqual({path: staged_format.identity(path) for path in paths}, before)
        for path in (linked, sibling):
            staged_format.git(path, "status", "--porcelain")
        self.assertFalse(list(bare.glob("config*.lock")))
        self.assertFalse(list(target.parent.glob("config*.lock")))

    def test_configuration_contention_preserves_other_writers_lock(self):
        config = self.root / ".git/config"
        before = config.read_bytes()
        lock = config.with_name("config.lock")
        lock.write_bytes(b"another Git process")
        with self.assertRaisesRegex(ValueError, "configuration is in use"):
            hooks.install(self.root)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(lock.read_bytes(), b"another Git process")
        self.assertFalse((hooks.directory(self.root) / "pre-commit").exists())

    def test_install_repairs_executable_permission_on_owned_bridge(self):
        hooks.install(self.root)
        (hooks.directory(self.root) / "pre-commit").chmod(0o600)
        self.assertFalse(hooks.status(self.root)["installed"])
        hooks.install(self.root)
        self.assertTrue(hooks.status(self.root)["installed"])

    def test_included_worktree_settings_are_rejected_before_mutation(self):
        included = self.root / ".git/included"
        self.git("config", "--file", str(included), "core.worktree", str(self.root))
        self.git("config", "core.worktree", str(self.root))
        self.git("config", "include.path", str(included))
        config = self.root / ".git/config"
        before = config.read_bytes()
        with self.assertRaisesRegex(ValueError, "included configuration"):
            hooks.install(self.root)
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse((self.root / ".git/config.worktree").exists())
        self.assertFalse((hooks.directory(self.root) / "pre-commit").exists())

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
            path.write_text(f"# harmless Unicode fixture {name} \u202e\n")
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


    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TROJAN_SOURCE"), "requires pinned hooks profile"
    )
    def test_nul_source_fails_cold_and_warm_and_old_cache_is_ignored(self):
        source = "// harmless fixture \u202e\0\nconst example = 1;\n"
        (self.root / "source.js").write_text(source)
        subprocess.run(["node", "--check"], input=source.encode(), check=True)
        self.git("add", ".")
        self.git("commit", "-qm", "NUL source")
        revision = self.git("rev-parse", "HEAD")
        import hashlib

        old = hashlib.sha256(b"anti-trojan-source@1.12.1:high:v1{}").hexdigest()
        old_cache = self.root / ".cache/toolchain/trojan-source" / old
        old_cache.mkdir(parents=True)
        (old_cache / self.git("rev-parse", "HEAD:source.js")).write_text("clean\n")

        def execute(root, profile, argv, **kwargs):
            return subprocess.run(
                argv, input=kwargs.get("input"), capture_output=True, check=True
            )

        with patch.object(chainman, "execute", execute):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "suspicious"):
                    trojan_source.run(self.root, [revision])


    def test_tree_deltas_keep_intermediate_coverage_and_bound_warm_traversal(self):
        base = self.git("rev-parse", "HEAD")
        trees = []
        for number in range(5):
            (self.root / "source.ts").write_text(f"const value = {number};\n")
            self.git("add", ".")
            self.git("commit", "-qm", str(number))
            trees.append(self.git("rev-parse", "HEAD"))
        real = staged_format.git
        walks = []

        def counted(root, *args, **kwargs):
            if args[0] in {"ls-tree", "diff-tree"}:
                walks.append(args[0])
            return real(root, *args, **kwargs)

        seen = []

        def execute(root, profile, argv, **kwargs):
            texts = json.loads(kwargs["input"])
            seen.extend(texts)
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([[] for _ in texts]).encode(), b""
            )

        records = f"x {trees[-1]} x {base}\n".encode()
        with (
            patch.object(staged_format, "git", counted),
            patch.object(chainman, "execute", execute),
        ):
            for _ in range(2):
                with patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(records))):
                    trojan_source.run(self.root, [])
        self.assertEqual(walks.count("ls-tree"), 2)
        self.assertEqual(walks.count("diff-tree"), 8)
        for number in range(5):
            self.assertIn(f"const value = {number};\n", seen)
        self.assertEqual(len(seen), len(set(seen)))


    def test_delta_inventory_matches_full_trees_across_merges_and_renames(self):
        base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "side")
        (self.root / "side.ts").write_text("side")
        self.git("add", "side.ts")
        self.git("commit", "-qm", "Side")
        side = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "trunk", base)
        (self.root / "trunk.ts").write_text("trunk")
        self.git("add", "trunk.ts")
        self.git("commit", "-qm", "Trunk")
        trunk = self.git("rev-parse", "HEAD")
        self.git("merge", "--no-edit", "--no-gpg-sign", "side")
        merge = self.git("rev-parse", "HEAD")
        self.git("mv", "side.ts", "renamed.ts")
        self.git("commit", "-qm", "Rename")
        revisions = [base, side, trunk, merge, self.git("rev-parse", "HEAD")]
        expected = set()
        actual = set()
        previous = None
        for revision in revisions:
            expected.update(trojan_source.tree_changes(self.root, None, revision))
            actual.update(
                entry
                for entry in trojan_source.tree_changes(self.root, previous, revision)
                if entry[0] in {"100644", "100755"}
            )
            previous = revision
        self.assertEqual(actual, expected)


    def test_exception_does_not_hide_same_blob_at_new_path(self):
        body = "const exception_fixture = 1;\n"
        (self.root / "allowed.ts").write_text(body)
        self.git("add", "allowed.ts")
        blob = self.git("rev-parse", ":allowed.ts")
        config = self.root / "chainman.toml"
        config.write_text(
            config.read_text()
            + f'\n[[hooks.trojan_source.exceptions]]\npath="allowed.ts"\nblob="{blob}"\nreason="fixture"\n'
        )
        self.git("add", "chainman.toml")
        self.git("commit", "-qm", "Excepted")
        self.git("mv", "allowed.ts", "new.ts")
        self.git("commit", "-qm", "New path")
        seen = []

        def execute(root, profile, argv, **kwargs):
            texts = json.loads(kwargs["input"])
            seen.extend(texts)
            return subprocess.CompletedProcess(
                argv, 0, json.dumps([[] for _ in texts]).encode(), b""
            )

        records = f"x {self.git('rev-parse', 'HEAD')} x {'0' * 40}\n".encode()
        with (
            patch.object(chainman, "execute", execute),
            patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(records))),
        ):
            trojan_source.run(self.root, [])
        self.assertIn(body, seen)


    def test_invalid_source_encoding_names_path_and_binary_is_not_cached(self):
        (self.root / "source.ts").write_bytes(b"\xff")
        self.git("add", ".")
        self.git("commit", "-qm", "Invalid encoding")

        def execute(root, profile, argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps([[] for _ in json.loads(kwargs["input"])]).encode(),
                b"",
            )

        with patch.object(chainman, "execute", execute):
            with self.assertRaisesRegex(ValueError, "source.ts"):
                trojan_source.run(self.root, [self.git("rev-parse", "HEAD")])
            self.git("rm", "source.ts")
            binary = self.root / "binary"
            binary.write_bytes(b"\x7fELF\0\xff")
            binary.chmod(0o755)
            self.git("add", "binary")
            self.git("commit", "-qm", "Executable")
            trojan_source.run(self.root, [self.git("rev-parse", "HEAD")])
            blob = self.git("rev-parse", "HEAD:binary")
            self.assertFalse(
                list((self.root / ".cache/toolchain/trojan-source").glob("*/" + blob))
            )
            self.git("mv", "binary", "binary.ts")
            self.git("commit", "-qm", "Explicit source")
            with self.assertRaisesRegex(ValueError, "binary.ts"):
                trojan_source.run(self.root, [self.git("rev-parse", "HEAD")])



if __name__ == "__main__":
    unittest.main()
