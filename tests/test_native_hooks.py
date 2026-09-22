"""Native Git is the oracle: real indexes/commits, isolated managed formatter."""

import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
CONTROL = os.environ.get("CHAINMAN_TEST_HOOK_CONTROL")


@unittest.skipUnless(CONTROL, "run just hooks-test for native hook fixtures")
class NativeHookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native hook ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.control = self.base / "control"
        self.control.mkdir()
        self.env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL="/dev/null",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_ATTR_NOSYSTEM="1",
        )
        self.git("init", "-q", "--template=")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "chainman.lock").write_text("a" * 40 + "\n")
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[hooks]
enabled=true
[formatters.text]
paths=["*.txt"]
profile="host"
write=["python3", "formatter.py"]
check=["true"]
""")
        (self.root / ".gitignore").write_text(".cache/\n.chainman/\n")
        (self.root / "formatter.py").write_text(
            "import pathlib,sys\nfor name in sys.argv[1:]:\n p=pathlib.Path(name);p.write_bytes(p.read_bytes().replace(b'BAD',b'GOOD'))\n"
        )
        (self.root / "a.txt").write_text("initial\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        worker = self.control / "worker.py"
        worker.write_text(
            f"import os,sys\nfrom pathlib import Path\nsys.path.insert(0,{str(SOURCE / 'scripts')!r})\nimport hook_worker\nsys.exit(hook_worker.run(Path(os.environ['CHAINMAN_PROJECT_ROOT']),sys.argv[2:]))\n"
        )
        self.launcher = self.control / "launcher.sh"
        self.launcher.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(worker))} "$@"\n'
        )
        validator = self.control / "lefthook"
        validator.write_text("#!/bin/sh\nexit 0\n")
        validator.chmod(0o755)
        self.plan = {
            "root": str(self.root),
            "git": shutil.which("git"),
            "launcher": str(self.launcher),
            "lefthook": str(validator),
            "directory": str(self.control),
            "enabled": True,
            "config": "/unused",
            "authority": {},
        }
        self.save_plan()

    def save_plan(self):
        self.plan["authority"] = {
            name: base64.b64encode((self.root / name).read_bytes()).decode()
            for name in ("chainman.toml", "chainman.lock")
        }
        (self.control / "plan.json").write_text(json.dumps(self.plan))

    def git(self, *args, check=True, **kwargs):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env,
            check=check,
            capture_output=True,
            **kwargs,
        )

    def hook(self, *args, check=True, **kwargs):
        result = subprocess.run(
            [CONTROL, "hook", str(self.control / "plan.json"), *args],
            env=self.env,
            capture_output=True,
            **kwargs,
        )
        if check:
            self.assertEqual(
                result.returncode, 0, (result.stdout + result.stderr).decode()
            )
        return result

    def stage(self, name="a.txt", body=b"BAD\n"):
        (self.root / name).write_bytes(body)
        self.git("add", "--", name)

    def install_format_fixture(self):
        path = self.root / ".git/hooks"
        path.mkdir(exist_ok=True)
        hook = path / "pre-commit"
        hook.write_text(
            f"#!/bin/sh\nexec {shlex.quote(CONTROL)} hook {shlex.quote(str(self.control / 'plan.json'))} format-staged\n"
        )
        hook.chmod(0o755)

    def test_commit_a_includes_pre_staged_and_tracked_work(self):
        self.install_format_fixture()
        self.stage()
        (self.root / "b.txt").write_text("initial\n")
        self.git("add", "b.txt")
        (self.root / "b.txt").write_text("BAD\n")
        self.git("commit", "-am", "Formatted")
        for name in ("a.txt", "b.txt"):
            self.assertEqual(self.git("show", "HEAD:" + name).stdout, b"GOOD\n")
        self.assertEqual(self.git("status", "--porcelain").stdout, b"")

    def test_partial_refusal_applies_nothing(self):
        self.stage(body=b"BAD\nkeep\n")
        self.stage("b.txt")
        (self.root / "a.txt").write_text("BAD\nunstaged\n")
        before = (self.root / ".git/index").read_bytes()
        result = self.hook("format-staged", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"partially staged", result.stderr)
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        self.assertEqual((self.root / "b.txt").read_bytes(), b"BAD\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"BAD\nunstaged\n")

    def test_formatted_partial_file_passes_unchanged(self):
        self.stage(body=b"GOOD\nkeep\n")
        (self.root / "a.txt").write_text("GOOD\nunstaged\n")
        before = (self.root / ".git/index").read_bytes()
        self.hook("format-staged")
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"GOOD\nunstaged\n")

    def test_literal_names_and_modes(self):
        name = "-space 'quote\nλ.txt"
        self.stage(name)
        (self.root / name).chmod(0o755)
        self.git("add", "--", name)
        self.hook("format-staged")
        self.assertEqual(self.git("show", ":" + name).stdout, b"GOOD\n")
        self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o755)

    def test_crlf_and_host_conditional_policy(self):
        external = self.base / "policy"
        external.write_text("[core]\nautocrlf=true\n")
        self.env["GIT_CONFIG_GLOBAL"] = str(external)
        self.stage(body=b"BAD\r\n")
        self.hook("format-staged")
        self.assertEqual(self.git("show", ":a.txt").stdout, b"GOOD\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"GOOD\r\n")

    def test_alternate_index_and_existing_lock_preserved(self):
        alternate = self.base / "alternate index"
        shutil.copyfile(self.root / ".git/index", alternate)
        original = (self.root / ".git/index").read_bytes()
        self.env["GIT_INDEX_FILE"] = str(alternate)
        self.stage()
        lock = Path(str(alternate) + ".lock")
        lock.write_text("Git-owned")
        result = self.hook("format-staged", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(lock.read_text(), "Git-owned")
        lock.unlink()
        self.hook("format-staged")
        self.assertEqual((self.root / ".git/index").read_bytes(), original)
        self.assertEqual(self.git("show", ":a.txt").stdout, b"GOOD\n")

    def test_filters_not_executed(self):
        (self.root / ".gitattributes").write_text("*.txt filter=fixture\n")
        self.git("add", ".gitattributes")
        self.stage()
        self.git("config", "filter.fixture.clean", "touch SHOULD-NOT-EXIST; cat")
        self.assertNotEqual(self.hook("format-staged", check=False).returncode, 0)
        self.assertFalse((self.root / "SHOULD-NOT-EXIST").exists())

    def test_install_uninstall_and_foreign_manager(self):
        self.hook("install")
        self.hook("install")
        self.assertTrue(json.loads(self.hook("status").stdout)["installed"])
        self.hook("uninstall")
        self.assertFalse(json.loads(self.hook("status").stdout)["installed"])
        self.git("config", "core.hooksPath", "foreign")
        self.assertNotEqual(self.hook("install", check=False).returncode, 0)
        self.assertEqual(
            self.git("config", "--get", "core.hooksPath").stdout, b"foreign\n"
        )

    def test_modified_owned_bridge_preserved(self):
        self.hook("install")
        bridge = self.root / ".git/chainman-hooks/pre-commit"
        bridge.write_text("foreign change")
        self.assertNotEqual(self.hook("uninstall", check=False).returncode, 0)
        self.assertEqual(bridge.read_text(), "foreign change")

    def test_linked_worktree_isolation(self):
        linked = self.base / "linked project"
        self.git("worktree", "add", "-qb", "fixture", str(linked))
        original = self.root
        self.root = linked
        self.plan["root"] = str(linked)
        self.save_plan()
        self.hook("install")
        self.assertTrue(json.loads(self.hook("status").stdout)["installed"])
        self.root = original
        self.plan["root"] = str(original)
        self.save_plan()
        self.assertFalse(json.loads(self.hook("status").stdout)["installed"])
        self.assertEqual(
            self.git("rev-parse", "--is-bare-repository").stdout, b"false\n"
        )

    def test_amend_path_limited_preserves_other_staging(self):
        self.install_format_fixture()
        self.stage()
        self.stage("b.txt")
        self.git("commit", "--amend", "--no-edit", "--", "a.txt")
        self.assertEqual(self.git("show", "HEAD:a.txt").stdout, b"GOOD\n")
        self.assertEqual(self.git("show", ":b.txt").stdout, b"BAD\n")

    def test_old_recovery_material_blocks_new_application(self):
        self.stage()
        old = self.root / ".chainman/staged-format/transaction-old"
        old.mkdir(parents=True)
        (old / "apply.json").write_text("legacy")
        result = self.hook("format-staged", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"interrupted formatting", result.stderr)
        self.assertEqual((old / "apply.json").read_text(), "legacy")

    def test_scanner_includes_removed_intermediate_content(self):
        base = self.git("rev-parse", "HEAD").stdout.decode().strip()
        (self.root / "source.ts").write_text("// harmless fixture \u202e\n")
        self.git("add", "source.ts")
        self.git("commit", "-qm", "Intermediate")
        first = self.git("rev-parse", "HEAD").stdout.decode().strip()
        self.git("rm", "source.ts")
        self.git("commit", "-qm", "Removed")
        tip = self.git("rev-parse", "HEAD").stdout.decode().strip()
        result = self.hook(
            "trojan-source",
            input=f"refs/heads/main {tip} refs/heads/main {base}\n".encode(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(first.encode(), result.stderr)
        self.assertIn(b"source.ts", result.stderr)
        self.assertIn(b"suspicious", result.stderr)

    def test_empty_push_has_no_scanner_work(self):
        self.hook("trojan-source", input=b"")

    def test_inactive_malformed_include_does_not_break_native_hooks(self):
        malformed = self.base / "unused"
        malformed.write_text("malformed config\n")
        global_config = self.base / "global"
        global_config.write_text(
            f'[includeIf "onbranch:unused-branch"]\n path = "{malformed}"\n'
        )
        self.env["GIT_CONFIG_GLOBAL"] = str(global_config)
        self.stage()
        self.hook("format-staged")
        self.assertEqual(self.git("show", ":a.txt").stdout, b"GOOD\n")

    def test_symlinked_relative_condition_uses_native_git(self):
        home = self.base / "home"
        policy = self.base / "dotfiles"
        home.mkdir()
        policy.mkdir()
        original = self.root
        (policy / "work").mkdir()
        self.root = policy / "work/project"
        shutil.move(str(original), self.root)
        (policy / "policy").write_text(
            '[includeIf "gitdir:./work/"]\n path = "attrs"\n'
        )
        (home / ".gitconfig").symlink_to(policy / "policy")
        for directory in (home, policy):
            (directory / "attrs").write_text("[core]\nautocrlf=true\n")
        self.env["GIT_CONFIG_GLOBAL"] = str(home / ".gitconfig")
        self.plan["root"] = str(self.root)
        self.save_plan()
        self.stage(body=b"BAD\r\n")
        self.hook("format-staged")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"GOOD\r\n")

    def test_nested_repository_resolves_relative_global_attributes(self):
        parent = self.base / "parent"
        parent.mkdir()
        self.git("init", "-q", str(parent))
        (parent / "relative-attributes").write_text("*.txt text eol=crlf\n")
        global_config = self.base / "global"
        global_config.write_text("[core]\nattributesFile=relative-attributes\n")
        old = self.root
        self.root = parent / "nested"
        shutil.move(str(old), self.root)
        (self.root / "relative-attributes").write_text("*.txt text eol=lf\n")
        self.env["GIT_CONFIG_GLOBAL"] = str(global_config)
        self.plan["root"] = str(self.root)
        self.save_plan()
        self.stage()
        self.hook("format-staged")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"GOOD\n")

    def change_formatter(self, extra):
        file = self.root / "formatter.py"
        file.write_text(file.read_text() + extra)
        self.git("add", "formatter.py")

    def test_concurrent_index_edit_is_preserved(self):
        self.stage()
        (self.root / "b.txt").write_text("concurrent\n")
        self.change_formatter(
            f"import subprocess\nsubprocess.run(['git','-C',{str(self.root)!r},'add','b.txt'],check=True)\n"
        )
        result = self.hook("format-staged", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"changed during formatting", result.stderr)
        self.assertEqual(self.git("show", ":b.txt").stdout, b"concurrent\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"BAD\n")

    def test_out_of_scope_output_and_failed_formatter_apply_nothing(self):
        self.stage()
        self.change_formatter("pathlib.Path('unexpected').write_text('side effect')\n")
        before = (self.root / ".git/index").read_bytes()
        result = self.hook("format-staged", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"outside selected content", result.stderr)
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        self.assertFalse((self.root / "unexpected").exists())

    def test_interrupted_application_retains_originals(self):
        if os.geteuid() == 0:
            self.skipTest("fixture requires ordinary filesystem permissions")
        self.stage()
        parent = self.root / "blocked"
        parent.mkdir()
        self.stage("blocked/z.txt")
        parent.chmod(0o555)
        self.addCleanup(parent.chmod, 0o755)
        before = (self.root / ".git/index").read_bytes()
        result = self.hook("format-staged", check=False)
        self.assertNotEqual(result.returncode, 0)
        journals = list((self.root / ".chainman/staged-format").glob("*/apply.json"))
        self.assertEqual(len(journals), 1)
        self.assertEqual((journals[0].parent / "original-index").read_bytes(), before)
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"GOOD\n")
        self.assertEqual((self.root / "blocked/z.txt").read_bytes(), b"BAD\n")
        self.assertIn(
            b"interrupted formatting", self.hook("format-staged", check=False).stderr
        )

    def test_concurrent_formatter_and_update_admission_and_signal(self):
        import time

        self.stage()
        marker = self.base / "formatter-ready"
        self.change_formatter(
            f"import time,os\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n"
        )
        child = subprocess.Popen(
            [CONTROL, "hook", str(self.control / "plan.json"), "format-staged"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not marker.exists()
                and child.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            self.assertIn(
                b"already running", self.hook("format-staged", check=False).stderr
            )
            code = f"import sys\nfrom pathlib import Path\nsys.path.insert(0,{str(SOURCE / 'scripts')!r})\nimport toolchain as tc\nwith tc.operation(Path({str(self.root)!r})): pass\n"
            update = subprocess.run(
                [sys.executable, "-c", code], env=self.env, capture_output=True
            )
            self.assertNotEqual(update.returncode, 0)
            self.assertIn(b"managed operation is active", update.stderr)
            child.terminate()
            child.communicate(timeout=12)
            self.assertEqual(child.returncode, 143)
            self.assertEqual((self.root / "a.txt").read_bytes(), b"BAD\n")
            self.assertEqual(
                list((self.root / ".chainman/staged-format").glob("*/apply.json")), []
            )
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()

    def test_initial_commit_and_local_configuration_contention(self):
        self.git("update-ref", "-d", "HEAD")
        self.stage()
        self.hook("format-staged")
        self.assertEqual(self.git("show", ":a.txt").stdout, b"GOOD\n")
        locked = self.root / ".git/config.lock"
        locked.write_text("other writer")
        before = (self.root / ".git/config").read_bytes()
        self.assertNotEqual(self.hook("install", check=False).returncode, 0)
        self.assertEqual(locked.read_text(), "other writer")
        self.assertEqual((self.root / ".git/config").read_bytes(), before)

    def test_bare_backed_worktree_preserves_primary_identity(self):
        bare = self.base / "bare.git"
        self.git("clone", "--bare", str(self.root), str(bare))
        linked = self.base / "bare worktree"
        subprocess.run(
            ["git", "-C", str(bare), "worktree", "add", "-qb", "linked", str(linked)],
            env=self.env,
            check=True,
            capture_output=True,
        )
        self.root = linked
        self.plan["root"] = str(linked)
        self.save_plan()
        self.hook("install")
        self.assertTrue(json.loads(self.hook("status").stdout)["installed"])
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(bare), "rev-parse", "--is-bare-repository"],
                env=self.env,
            ),
            b"true\n",
        )
        self.assertEqual(
            self.git("rev-parse", "--is-bare-repository").stdout, b"false\n"
        )

    def test_dormant_worktree_configuration_preserved(self):
        before = (self.root / ".git/config").read_bytes()
        dormant = self.root / ".git/config.worktree"
        dormant.write_text("[include]\npath=../foreign\n")
        result = self.hook("install", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"dormant", result.stderr)
        self.assertEqual((self.root / ".git/config").read_bytes(), before)
        self.assertEqual(dormant.read_text(), "[include]\npath=../foreign\n")
        self.assertFalse((self.root / ".git/chainman-hooks/pre-commit").exists())

    def test_included_repository_identity_refused_before_installation(self):
        identity = self.root / ".git/identity"
        identity.write_text("[core]\nbare=false\n")
        self.git("config", "include.path", "identity")
        before = (self.root / ".git/config").read_bytes()
        result = self.hook("install", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"included configuration", result.stderr)
        self.assertEqual((self.root / ".git/config").read_bytes(), before)
        self.assertFalse((self.root / ".git/chainman-hooks/pre-commit").exists())

    def test_invalid_utf8_path_fails_without_application(self):
        name = os.fsencode(self.root) + b"/invalid-\xff.txt"
        with open(name, "wb") as file:
            file.write(b"BAD\n")
        self.git("add", "--", os.fsdecode(name))
        before = (self.root / ".git/index").read_bytes()
        self.assertNotEqual(self.hook("format-staged", check=False).returncode, 0)
        self.assertEqual((self.root / ".git/index").read_bytes(), before)
        with open(name, "rb") as file:
            self.assertEqual(file.read(), b"BAD\n")

    def test_scanner_languages_exceptions_and_unchanged_worktree(self):
        paths = [
            "source.cjs",
            "main.dart",
            "page.astro",
            "flake.nix",
            "nested/Justfile",
            "entry",
        ]
        for name in paths:
            target = self.root / name
            target.parent.mkdir(exist_ok=True)
            target.write_text(f"// {name} harmless fixture \u202e\0\n")
        (self.root / "entry").chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "Source fixtures")
        revision = self.git("rev-parse", "HEAD").stdout.decode().strip()
        for name in paths:
            (self.root / name).write_text("clean working file\n")
        for _ in range(2):
            result = self.hook("trojan-source", revision, check=False)
            self.assertNotEqual(result.returncode, 0)
            for name in paths:
                self.assertIn(name.encode(), result.stderr)
        config = self.root / "chainman.toml"
        for name in paths:
            blob = self.git("rev-parse", "HEAD:" + name).stdout.decode().strip()
            with config.open("a") as out:
                out.write(
                    f'\n[[hooks.trojan_source.exceptions]]\npath="{name}"\nblob="{blob}"\nreason="Harmless fixture"\n'
                )
        self.save_plan()
        self.hook("trojan-source", revision)
        self.git("show", "HEAD:source.cjs")
        (self.root / "copy.cjs").write_bytes(self.git("show", "HEAD:source.cjs").stdout)
        self.git("add", "copy.cjs")
        self.git("commit", "-qm", "Same blob new path")
        result = self.hook(
            "trojan-source",
            self.git("rev-parse", "HEAD").stdout.decode().strip(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"copy.cjs", result.stderr)

    def test_scanner_tags_deletions_and_missing_base(self):
        (self.root / "source.ts").write_text("// harmless fixture \u202e\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Source fixture")
        self.git("tag", "-a", "fixture", "-m", "Tag fixture")
        tag = self.git("rev-parse", "fixture").stdout.decode().strip()
        zero = "0" * 40
        result = self.hook(
            "trojan-source",
            input=f"refs/tags/fixture {tag} refs/tags/fixture {'f' * 40}\n(delete) {zero} refs/heads/old {tag}\n".encode(),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"unavailable", result.stderr)
        self.assertIn(b"source.ts", result.stderr)
        self.hook(
            "trojan-source", input=f"(delete) {zero} refs/heads/old {tag}\n".encode()
        )
        self.assertNotEqual(
            self.hook("trojan-source", input=b"not a record\n", check=False).returncode,
            0,
        )

    def test_scanner_invalid_source_and_native_binary(self):
        (self.root / "source.ts").write_bytes(b"\xff\x00")
        self.git("add", ".")
        self.git("commit", "-qm", "Encoding fixture")
        revision = self.git("rev-parse", "HEAD").stdout.decode().strip()
        result = self.hook("trojan-source", revision, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"unsupported source encoding", result.stderr)
        self.assertIn(b"source.ts", result.stderr)
        self.git("rm", "source.ts")
        (self.root / "binary").write_bytes(b"\x7fELF\xff\x00")
        (self.root / "binary").chmod(0o755)
        self.git("add", ".")
        self.git("commit", "-qm", "Binary fixture")
        revision = self.git("rev-parse", "HEAD").stdout.decode().strip()
        for _ in range(2):
            result = self.hook("trojan-source", revision)
            self.assertIn(b"native executable not scanned", result.stderr)
