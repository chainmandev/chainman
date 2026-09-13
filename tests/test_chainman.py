"""Consumer contracts across shared execution, update transactions and runtime pins."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote, unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman
import chainman_updates as consumer_updates
import registry
import toolchain
import updates
import update_staging


class ConsumerFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman consumer spaces ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project with spaces"
        self.root.mkdir()
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("GIT_", "CHAINMAN_")) and k != "TOOLCHAIN_LOCK_FD"
        }
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            CHAINMAN_MODE="host-nix",
            TOOLCHAIN_DOWNLOAD_CACHE=str(self.base / "downloads"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.write("chainman.toml", 'schema=1\n[project]\ndefault_profile="host"\n')

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def git(self, *arguments):
        return subprocess.check_output(
            ["git", *arguments], cwd=self.root, text=True
        ).strip()

    def init_git(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fixture Developer")
        self.git("config", "user.email", "developer@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial project")
        return self.git("rev-parse", "HEAD")


class ExecutionTests(ConsumerFixture):
    def test_runtime_nix_survives_external_shells_refresh_and_active_reentry(self):
        # Bind the real Nix package, never a directory containing host language tools.
        binding = str(Path(shutil.which("nix")).resolve().parent)
        competing = self.root / "project executables"
        competing.mkdir()
        for name in ("nix", "nix-store"):
            self.write(f"project executables/{name}", "#!/bin/sh\nexit 97\n").chmod(
                0o755
            )
        self.write(
            "project executables/python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
        ).chmod(0o755)
        shutil.copytree(chainman.RUNTIME / "nix", self.root / "environment")
        flake = self.root / "environment/flake.nix"
        flake.write_text(
            flake.read_text().replace(
                "ulimit -c 0",
                f'export PATH="{competing}:$PATH"\n                export CHAINMAN_RUNTIME_NIX_BIN="{competing}"\n                ulimit -c 0',
            )
        )
        self.write(
            "chainman.toml", 'schema=1\n[profiles.native]\nflake="environment#core"\n'
        )
        child = self.write(
            "reenter.py",
            "import json, os, shutil, subprocess, sys; from pathlib import Path\n"
            f"sys.path.insert(0, {str(chainman.RUNTIME / 'scripts')!r})\n"
            "import chainman\n"
            "seen=json.loads(os.environ.get('OBSERVED', '[]'))\n"
            "seen.append([shutil.which(n) for n in ('nix', 'nix-store', 'python3')])\n"
            "subprocess.run(['nix', '--version'], check=True, stdout=subprocess.DEVNULL)\n"
            "subprocess.run(['nix-store', '--version'], check=True, stdout=subprocess.DEVNULL)\n"
            "depth=int(sys.argv[1])\n"
            "if depth:\n"
            " os.environ.update(OBSERVED=json.dumps(seen), TOOLCHAIN_FRESH=str(depth % 2))\n"
            f" os.environ['PATH']={str(competing)!r}+os.pathsep+os.environ['PATH']\n"
            " chainman.execute(Path.cwd(), 'native', [sys.executable, __file__, str(depth-1)])\n"
            "else: print(json.dumps(seen))\n",
        )
        with patch.dict(os.environ, CHAINMAN_RUNTIME_NIX_BIN=binding):
            result = chainman.execute(
                self.root,
                "native",
                [sys.executable, str(child), "3"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(
            json.loads(result.stdout),
            [
                [
                    str(Path(binding) / "nix"),
                    str(Path(binding) / "nix-store"),
                    str(competing / "python3"),
                ]
            ]
            * 4,
        )

    def test_runtime_nix_binding_cannot_be_overwritten_by_configuration(self):
        for section in ("environment.values", "profiles.native.environment"):
            self.write(
                "chainman.toml",
                f'schema=1\n[profiles.native]\nruntime_profile="core"\n[{section}]\nCHAINMAN_RUNTIME_NIX_BIN="/different"\n',
            )
            with self.assertRaisesRegex(ValueError, "internal runtime Nix binding"):
                chainman.execute(self.root, "native", ["true"])

    def test_nested_nix_refreshes_preserve_selected_temporary_base(self):
        flake = self.root / "environment"
        shutil.copytree(chainman.RUNTIME / "nix", flake)
        path = flake / "flake.nix"
        path.write_text(
            path.read_text().replace(
                "ulimit -c 0",
                'export TMPDIR="$TMPDIR/consumer-shell"\n                ulimit -c 0',
            )
        )
        child = self.write(
            "reenter.py",
            "import json, os, sys; from pathlib import Path\n"
            f"sys.path.insert(0, {str(chainman.RUNTIME / 'scripts')!r})\n"
            "import chainman\n"
            "seen=json.loads(os.environ.get('TEMP_OBSERVED', '[]'))\n"
            "seen.append(os.environ['TMPDIR'])\n"
            "depth=int(sys.argv[1])\n"
            "if depth:\n"
            " os.environ.update(TOOLCHAIN_FRESH='1', TEMP_OBSERVED=json.dumps(seen))\n"
            " chainman.execute(Path.cwd(), 'native', [sys.executable, __file__, str(depth-1)])\n"
            "else: print(json.dumps(seen))\n",
        )
        caller = self.root / "caller temporary files"
        caller.mkdir(mode=0o700)
        for override in (False, True):
            with self.subTest(profile_override=override):
                config = (
                    'schema=1\n[profiles.native]\nflake="environment/flake.nix#core"\n'
                )
                expected = caller
                if override:
                    expected = self.root / "profile temporary files"
                    expected.mkdir(mode=0o700)
                    config += '[profiles.native.environment]\nTMPDIR="{root}/profile temporary files"\n'
                self.write("chainman.toml", config)
                with patch.dict(os.environ, TMPDIR=str(caller)):
                    result = chainman.execute(
                        self.root,
                        "native",
                        [sys.executable, str(child), "3"],
                        capture_output=True,
                        text=True,
                    )
                self.assertEqual(json.loads(result.stdout), [str(expected)] * 4)

    def test_temporary_routing_is_not_a_project_environment_setting(self):
        self.write(
            "chainman.toml",
            'schema=1\n[environment.values]\nCHAINMAN_TEMP_BASE="/unexpected"\n',
        )
        with self.assertRaisesRegex(ValueError, "Configure TMPDIR"):
            chainman.execute(self.root, "host", ["true"])

    def test_legacy_nix_hook_uses_shared_snapshot_resolution_and_final_audit(self):
        import dependency_api
        import source_updates

        events = []
        with (
            patch.object(
                sys, "argv", ["chainman.py", "--root", str(self.root), "nix-update"]
            ),
            patch.dict(os.environ, CHAINMAN_UPDATE_ACTIVE="1"),
            patch.object(dependency_api, "policy", return_value={}),
            patch.object(
                source_updates,
                "snapshot",
                side_effect=lambda *a: events.append("before") or {"baseline": True},
            ),
            patch.object(
                source_updates,
                "resolve",
                side_effect=lambda *a: events.append("resolve"),
            ),
            patch.object(
                source_updates,
                "audit",
                side_effect=lambda root, spec, before, *a: events.append(
                    ("audit", before)
                ),
            ),
        ):
            self.assertEqual(chainman.main(), 0)
        self.assertEqual(events, ["before", "resolve", ("audit", {"baseline": True})])

    def test_adopted_git_flake_excludes_caches_and_uses_dirty_tracked_bytes(self):
        self.write(
            "flake.nix",
            "{ outputs = { self }: { marker = builtins.readFile ./marker; cacheVisible = builtins.pathExists ./.cache; }; }",
        )
        self.write("marker", "original")
        self.write(
            "chainman.toml", 'schema=1\n[profiles.native]\nflake="flake.nix#default"\n'
        )
        self.init_git()
        self.write(".cache/large-package-download", "not source")
        self.write("marker", "edited")
        ref, _ = chainman.profile(self.root, "native")
        self.assertTrue(ref.startswith("git+file:"))
        for attribute, expected in (("marker", "edited"), ("cacheVisible", False)):
            output = subprocess.check_output(
                [
                    "nix",
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "eval",
                    "--json",
                    "--no-write-lock-file",
                    ref.rsplit("#", 1)[0] + "#" + attribute,
                ],
                cwd=self.root,
                text=True,
            )
            self.assertEqual(json.loads(output), expected)

    def test_nested_copy_does_not_adopt_parent_git_source(self):
        self.write("marker", "parent")
        self.init_git()
        nested = self.root / "copied project"
        nested.mkdir()
        (nested / "flake.nix").write_text("{}")
        self.assertEqual(
            chainman.flake_reference(nested, nested, "core"),
            "path:" + quote(str(nested), safe="/") + "#core",
        )

    def test_untracked_bracket_directory_is_not_a_tracked_git_pathspec_match(self):
        self.write("nix x/flake.nix", "{}")
        self.init_git()
        self.write("nix [x]/flake.nix", "{}")
        location = self.root / "nix [x]"
        self.assertEqual(
            chainman.flake_reference(self.root, location, "default"),
            "path:" + quote(str(location), safe="/") + "#default",
        )

    def test_pnpm_store_overrides_follow_project_then_profile_layers(self):
        self.write("flake.nix", "{}")
        self.write(
            "chainman.toml",
            """schema=1
[environment.values]
PNPM_CONFIG_STORE_DIR="{root}/canonical"
PNPM_STORE_DIR="{root}/legacy"
[profiles.native]
flake="flake.nix#default"
[profiles.native.environment]
npm_config_store_dir="{root}/profile store"
""",
        )
        for profile, expected in (("host", "canonical"), ("native", "profile store")):
            with self.subTest(profile=profile):
                with patch.object(toolchain, "managed_run") as execute:
                    chainman.execute(self.root, profile, ["true"])
                selected = execute.call_args.kwargs["env"]
                for name in toolchain.PNPM_STORE_VARIABLES:
                    self.assertEqual(selected[name], str(self.root / expected))

    def test_current_directory_flake_spellings_agree(self):
        self.write("flake.nix", "{}")
        for value in (
            ".#default",
            "./#default",
            "flake.nix#default",
            "./flake.nix#default",
        ):
            with self.subTest(value=value):
                self.write(
                    "chainman.toml",
                    "schema=1\n[profiles.native]\nflake=" + json.dumps(value) + "\n",
                )
                ref, _ = chainman.profile(self.root, "native")
                self.assertEqual(unquote(ref), f"path:{self.root}#default")

    def test_file_flake_and_spaces_preserve_literal_arguments(self):
        self.write("environments/native shell/flake.nix", "{}")
        self.write(
            "chainman.toml",
            """schema=1
[profiles.native]
flake="environments/native shell/flake.nix#default"
""",
        )
        argv = ["printf", "%s", "$(must stay literal) two words", "--dash", ""]
        with patch.object(toolchain, "managed_run") as execute:
            chainman.execute(self.root, "native", argv)
        actual = execute.call_args.args[0]
        self.assertEqual(actual[-len(argv) :], argv)
        ref = actual[actual.index("develop") + 1]
        self.assertEqual(
            unquote(ref), f"path:{self.root}/environments/native shell#default"
        )
        self.assertIn("--no-write-lock-file", actual)
        self.assertEqual(execute.call_args.kwargs["cwd"], self.root)

    def test_profile_path_escape_and_missing_shell_fail_before_spawn(self):
        for value in (
            "../outside#default",
            "/outside#default",
            "flake.nix",
            "flake.nix#bad/attr",
            "#default",
        ):
            with self.subTest(value=value):
                self.write(
                    "chainman.toml",
                    "schema=1\n[profiles.native]\nflake=" + json.dumps(value) + "\n",
                )
                with patch.object(toolchain, "managed_run") as execute:
                    with self.assertRaises(ValueError):
                        chainman.execute(self.root, "native", ["true"])
                    execute.assert_not_called()

    def test_host_command_default_profile_and_environment_unset(self):
        self.write(
            "capture.py",
            'import json,os,sys; from pathlib import Path; Path("result.json").write_text(json.dumps({"args":sys.argv[1:],"removed":os.environ.get("CARGO_TARGET_DIR"),"project":os.environ["FIXTURE_PROJECT"]}))',
        )
        self.write(
            "chainman.toml",
            """schema=1
[project]
default_profile="host"
[environment]
unset=["CARGO_TARGET_DIR"]
[environment.values]
FIXTURE_PROJECT="{root}"
[commands]
capture=[["python3","capture.py"]]
""",
        )
        os.environ["CARGO_TARGET_DIR"] = "must disappear"
        result = chainman.main(
            [
                "--root",
                str(self.root),
                "run",
                "capture",
                "--",
                "two words",
                "$(literal)",
            ]
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            json.loads((self.root / "result.json").read_text()),
            {
                "args": ["two words", "$(literal)"],
                "removed": None,
                "project": str(self.root),
            },
        )

    def test_environment_unset_cannot_remove_lock_authority(self):
        self.write(
            "chainman.toml", 'schema=1\n[environment]\nunset=["TOOLCHAIN_LOCK_FD"]\n'
        )
        with patch.object(toolchain, "managed_run") as execute:
            with self.assertRaises(ValueError):
                chainman.execute(self.root, "host", ["true"])
            execute.assert_not_called()

    def test_hook_extra_arguments_only_reach_final_command(self):
        self.write(
            "record.py",
            'import json,sys; from pathlib import Path; f=Path("arguments.jsonl").open("a"); f.write(json.dumps(sys.argv[1:])+"\\n")',
        )
        chainman.run_hook(
            self.root,
            [
                [sys.executable, "record.py", "first"],
                [sys.executable, "record.py", "last"],
            ],
            name="host",
            extra=["two words", "--switch"],
        )
        self.assertEqual(
            [
                json.loads(line)
                for line in (self.root / "arguments.jsonl").read_text().splitlines()
            ],
            [["first"], ["last", "two words", "--switch"]],
        )

    def test_changed_profile_inputs_force_environment_reentry(self):
        self.write("flake.nix", "{}")
        self.write(
            "chainman.toml", 'schema=1\n[profiles.native]\nflake="flake.nix#default"\n'
        )
        captured = []
        with patch.object(
            toolchain,
            "managed_run",
            side_effect=lambda argv, **kw: captured.append((argv, kw)),
        ):
            chainman.execute(self.root, "native", ["first"])
            environment = captured[-1][1]["env"]
            chainman.execute(self.root, "native", ["second"], env=environment)
            self.assertEqual(captured[-1][0], ["second"])
            self.write("flake.lock", '{"version":7}')
            chainman.execute(self.root, "native", ["third"], env=environment)
            self.assertIn("develop", captured[-1][0])

    def test_same_project_nested_operation_and_different_project_ownership(self):
        other = self.base / "independent project"
        other.mkdir()
        (other / "chainman.toml").write_text("schema=1\n")
        child = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0,{str(chainman.RUNTIME / 'scripts')!r}); "
            "import toolchain; "
            "root=Path(sys.argv[1]); "
            '\nwith toolchain.operation(root): print("nested accepted")\n'
        )
        with toolchain.operation(self.root):
            first = toolchain.managed_options({})["pass_fds"][0]
            identity = os.fstat(first)
            result = toolchain.managed_run(
                [sys.executable, "-c", child, str(self.root)],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("nested accepted", result.stdout)
            with toolchain.operation(other):
                second = toolchain.managed_options({})["pass_fds"][0]
                self.assertNotEqual(
                    (identity.st_dev, identity.st_ino),
                    (os.fstat(second).st_dev, os.fstat(second).st_ino),
                )
                for root in (self.root, other):
                    result = subprocess.run(
                        [sys.executable, "-c", child, str(root)],
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("active", result.stderr)
            self.assertEqual(toolchain.managed_options({})["pass_fds"][0], first)
        with toolchain.operation(other), toolchain.operation(self.root):
            pass

    def test_outer_exec_prunes_declared_stale_build_outputs(self):
        self.write(
            "chainman.toml",
            'schema=1\n[project]\ndefault_profile="host"\n'
            "[cache]\nbuild_limit_gib=0\nstale_hours=0\n",
        )
        obsolete = self.write(".cache/toolchain/work/old-context/object", "old build")
        stamp = self.write(".cache/toolchain/work/old-context/last-used", "")
        os.utime(stamp, (1, 1))
        result = subprocess.run(
            [
                sys.executable,
                str(chainman.RUNTIME / "scripts/chainman.py"),
                "--root",
                str(self.root),
                "exec",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; assert not Path(sys.argv[1]).exists()",
                str(obsolete),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(obsolete.parent.exists())

    def test_nested_exec_preserves_active_outputs_and_rejects_cleanup(self):
        self.write(
            "chainman.toml",
            'schema=1\n[project]\ndefault_profile="host"\n'
            "[cache]\nbuild_limit_gib=0\nstale_hours=0\n",
        )
        entry = [
            sys.executable,
            str(chainman.RUNTIME / "scripts/chainman.py"),
            "--root",
            str(self.root),
        ]
        with toolchain.operation(self.root):
            env = toolchain.environment(self.root)
            active = Path(env["TOOLCHAIN_WORK"]) / "active-object"
            active.write_text("parent build is using these bytes")
            os.utime(active.parent / "last-used", (1, 1))
            result = toolchain.managed_run(
                [
                    *entry,
                    "exec",
                    "--",
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; assert Path(sys.argv[1]).read_text() == 'parent build is using these bytes'",
                    str(active),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for action in ("clean", "cache-prune"):
                with self.subTest(action=action):
                    result = toolchain.managed_run(
                        [*entry, action, "--all"],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("active managed operation", result.stderr)
                    self.assertEqual(
                        active.read_text(), "parent build is using these bytes"
                    )


class UpdateHookTests(ConsumerFixture):
    def setUp(self):
        super().setUp()
        self.write(".gitignore", ".cache/\n.chainman/\n__pycache__/\n")
        self.write("deps.txt", "1.0\n")
        self.write("notes.txt", "Unrelated project intent\n")
        self.write(
            "chainman.lock",
            json.dumps(
                {
                    "schema": 1,
                    "version": "fixture",
                    "revision": "fixture",
                    "url": "https://example.invalid/runtime.tar.gz",
                    "narHash": "sha256-" + "A" * 43 + "=",
                }
            ),
        )
        self.write(
            "resolver.py",
            """import os,sys,subprocess
from pathlib import Path
mode = sys.argv[1] if len(sys.argv)>1 else "2.0"
assert os.environ["CHAINMAN_MINIMUM_RELEASE_AGE_DAYS"] == "30"
if mode == "nested":
    subprocess.run([sys.executable,"nested_launcher.py"],check=True)
else:
    Path("deps.txt").write_text(("2.0" if mode in ("fail","unexpected") else mode)+"\\n")
if mode == "unexpected": Path("notes.txt").write_text("unauthorized mutation\\n")
if mode == "fail": sys.exit(17)
""",
        )
        self.write(
            "nested_launcher.py",
            """import os
from pathlib import Path
root=Path(os.environ.get("CHAINMAN_PROJECT_ROOT", os.getcwd()))
assert root.resolve()==Path.cwd().resolve(), "nested launcher escaped copied project"
(root/"deps.txt").write_text("2.0\\n")
""",
        )
        self.write(
            "verifier.py",
            """from pathlib import Path
import sys
value=Path("deps.txt").read_text().strip()
if value == "bad": sys.exit(19)
assert value in ("2.0","alter")
Path(".cache/verified").write_text(str(Path.cwd()))
if value == "alter": Path("notes.txt").write_text("verification mutation\\n")
""",
        )
        self.write(
            "chainman.toml",
            """schema=1
[project]
default_profile="host"
[updates]
profile="host"
eligibility="resolver"
minimum_age_days=30
resolver=[["python3","resolver.py"]]
verify=[["python3","verifier.py"]]
outputs=["deps.txt"]
""",
        )
        self.initial = self.init_git()

    def update(self, *args, root=None):
        root = self.root if root is None else root
        args = ("--skip-chainman", *args)
        self.stage = Path(tempfile.mkdtemp(dir=self.base, prefix="staged-"))
        for name in ("control", "candidate"):
            (self.stage / name).mkdir()
        self.copy = self.stage / "candidate"
        update_staging.prepare(root, self.stage, list(args))
        at = (self.stage / "control/at").read_text().strip()
        update_staging.resolve(self.copy, at, list(args))
        # The neutral pin is not fetched here. Runtime authenticity has separate
        # real Nix/bootstrap tests; this transaction uses real Git and hook commands.
        with patch.object(
            update_staging, "verified_runtime", return_value=chainman.RUNTIME
        ):
            update_staging.inspect(root, self.stage)
        state = json.loads((self.stage / "control/state.json").read_text())
        if state["paths"]:
            update_staging.run(self.copy, "_update-verify", ["legacy"])
        with redirect_stdout(io.StringIO()) as stream:
            update_staging.finalize(root, self.stage)
        self.record = json.loads(stream.getvalue())
        return 0

    def test_verified_real_git_commit_then_noop(self):
        self.assertEqual(self.update("--", "2.0"), 0)
        new_head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(new_head, self.initial)
        self.assertEqual(self.git("rev-parse", "HEAD^"), self.initial)
        self.assertEqual(self.git("show", "HEAD:deps.txt"), "2.0")
        self.assertEqual(
            self.git("show", "--format=", "--name-only", "HEAD"), "deps.txt"
        )
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.copy / ".cache/verified").read_text(), str(self.copy))
        self.assertEqual(self.update("--", "2.0"), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), new_head)

    def test_exact_commit_message_survives_staging(self):
        message = "Refresh dependencies: literal $(do-not-run)\n\nVerified candidate."
        self.update("--json", "--message", message, "--", "2.0")
        self.assertEqual(self.record["schema"], 1)
        self.assertEqual(self.record["changed"], ["deps.txt"])
        self.assertEqual(self.record["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("log", "-1", "--format=%B"), message)

    def test_direct_python_update_entry_cannot_bypass_host_orchestration(self):
        result = subprocess.run(
            [
                sys.executable,
                str(chainman.RUNTIME / "scripts/chainman.py"),
                "--root",
                str(self.root),
                "deps-update",
                "--json",
                "--",
                "2.0",
            ],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("direct Python entry", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)

    def test_commit_disabled_still_verifies_without_staging(self):
        self.assertEqual(self.update("--no-commit", "--", "2.0"), 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertTrue((self.copy / ".cache/verified").is_file())
        self.assertEqual((self.root / "deps.txt").read_text(), "2.0\n")

    def test_preview_nested_launcher_cannot_target_ambient_original_root(self):
        before = updates.snapshot(self.root)
        os.environ["CHAINMAN_PROJECT_ROOT"] = str(self.root)
        self.assertEqual(self.update("--preview", "--", "nested"), 0)
        self.assertEqual(updates.snapshot(self.root), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / ".cache/verified").exists())
        self.assertEqual(os.environ["CHAINMAN_PROJECT_ROOT"], str(self.root))

    def test_failed_resolver_preserves_changes_without_commit(self):
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            self.update("--", "fail")
        self.assertEqual(raised.exception.returncode, 17)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.copy / "deps.txt").read_text(), "2.0\n")
        self.assertFalse((self.root / ".cache/verified").exists())

    def test_failed_verification_preserves_changes_without_commit(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.update("--", "bad")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.copy / "deps.txt").read_text(), "bad\n")

    def test_unexpected_resolver_output_is_rejected_before_verification(self):
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            self.update("--", "unexpected")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / ".cache/verified").exists())
        self.assertEqual(
            (self.copy / "notes.txt").read_text(), "unauthorized mutation\n"
        )

    def test_verification_cannot_change_source_even_with_success_exit(self):
        with self.assertRaisesRegex(ValueError, "Verification changed"):
            self.update("--", "alter")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(
            (self.copy / "notes.txt").read_text(), "verification mutation\n"
        )

    def test_nested_parent_repository_is_not_an_adopted_consumer(self):
        nested = self.root / "nested example"
        nested.mkdir()
        (nested / "chainman.toml").write_text((self.root / "chainman.toml").read_text())
        before = self.git("status", "--porcelain")
        with self.assertRaisesRegex(ValueError, "enclosing"):
            self.update(root=nested)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.git("status", "--porcelain"), before)

    def test_custom_hook_must_explicitly_own_release_eligibility(self):
        cfg = (
            (self.root / "chainman.toml")
            .read_text()
            .replace('eligibility="resolver"\n', "")
        )
        self.write("chainman.toml", cfg)
        with self.assertRaisesRegex(ValueError, "eligibility"):
            consumer_updates.resolve_current(
                self.root,
                toolchain.config(self.root)["updates"],
                datetime.now(timezone.utc),
                [],
            )
        self.assertEqual((self.root / "deps.txt").read_text(), "1.0\n")


class RuntimeReleaseTests(ConsumerFixture):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        self.write(
            "chainman.lock",
            json.dumps(
                {
                    "schema": 1,
                    "version": "0.1.0",
                    "revision": "a" * 40,
                    "url": "https://example.invalid/old.tar.gz",
                    "narHash": "sha256-" + "A" * 43 + "=",
                }
            ),
        )
        self.initial_lock = (self.root / "chainman.lock").read_bytes()
        self.policy = {"minimum_age_days": 30}
        self.metadata = {
            "schema": 1,
            "version": "2.0.0",
            "revision": "b" * 40,
            "url": "https://example.invalid/new.tar.gz",
            "narHash": "sha256-" + "B" * 43 + "=",
            "archive_sha256": hashlib.sha256(b"expected archive").hexdigest(),
        }
        releases = [
            registry.Release("v2.0.0", self.now - timedelta(days=40)),
            registry.Release("v3.0.0", self.now - timedelta(days=5)),
        ]
        for name, value in (
            ("github_releases", releases),
            ("github_commit", "b" * 40),
        ):
            mocked = patch.object(registry, name, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.api = "https://api.github.com/repos/chainmandev/chainman"
        dated = patch.object(
            consumer_updates.source_updates,
            "commit_time",
            return_value=releases[0].published,
        )
        dated.start()
        self.addCleanup(dated.stop)
        metadata = patch.object(registry, "data", side_effect=self.release_metadata)
        metadata.start()
        self.addCleanup(metadata.stop)
        downloaded = patch.object(registry, "fetch", side_effect=self.release_download)
        self.download = downloaded.start()
        self.addCleanup(downloaded.stop)

    def release_metadata(self, url):
        self.assertEqual(url, self.api + "/releases/tags/v2.0.0")
        published = (self.now - timedelta(days=40)).isoformat()
        return {
            "tag_name": "v2.0.0",
            "draft": False,
            "prerelease": False,
            "published_at": published,
            "assets": [
                {
                    "id": number,
                    "name": name,
                    "state": "uploaded",
                    "size": len(body),
                    "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "created_at": published,
                    "updated_at": published,
                }
                for number, name, body in (
                    (1, "chainman-release.json", json.dumps(self.metadata).encode()),
                    (2, "chainman-2.0.0.tar.gz", b"expected archive"),
                )
            ],
        }

    def release_download(self, url, *, accept):
        self.assertEqual(accept, "application/octet-stream")
        if url == self.api + "/releases/assets/1":
            return json.dumps(self.metadata).encode(), {}
        self.assertEqual(url, self.api + "/releases/assets/2")
        return b"expected archive", {}

    def assert_downloaded_exact_assets(self):
        self.assertEqual(
            [call.args[0] for call in self.download.call_args_list],
            [self.api + "/releases/assets/1", self.api + "/releases/assets/2"],
        )

    def test_ineligible_release_fails_without_fetching_or_rewriting_pin(self):
        with (
            patch.object(
                registry,
                "github_releases",
                return_value=[registry.Release("v2.0.0", self.now - timedelta(days=2))],
            ),
            patch.object(toolchain, "managed_run") as fetch,
        ):
            with self.assertRaisesRegex(ValueError, "eligible"):
                consumer_updates.runtime_candidate(
                    self.root, self.policy, self.now, gc_root=self.base / "runtime-root"
                )
            fetch.assert_not_called()
            self.download.assert_not_called()
        self.assertEqual((self.root / "chainman.lock").read_bytes(), self.initial_lock)

    def test_release_metadata_or_provenance_mismatch_is_rejected(self):
        for overrides in ({"version": "9.0.0"}, {"revision": "c" * 40}):
            with (
                self.subTest(overrides=overrides),
                patch.dict(self.metadata, overrides),
                patch.object(toolchain, "managed_run") as fetch,
            ):
                with self.assertRaises(ValueError):
                    consumer_updates.runtime_candidate(
                        self.root,
                        self.policy,
                        self.now,
                        gc_root=self.base / "runtime-root",
                    )
                fetch.assert_not_called()
                self.assert_downloaded_exact_assets()
                self.download.reset_mock()
                self.assertEqual(
                    (self.root / "chainman.lock").read_bytes(), self.initial_lock
                )

    def test_nix_hash_verification_failure_is_not_concealed(self):
        failed = subprocess.CalledProcessError(
            1, ["nix", "eval"], stderr="hash mismatch"
        )
        with patch.object(toolchain, "managed_run", side_effect=failed):
            with self.assertRaises(subprocess.CalledProcessError):
                consumer_updates.runtime_candidate(
                    self.root, self.policy, self.now, gc_root=self.base / "runtime-root"
                )
        self.assert_downloaded_exact_assets()
        self.assertEqual((self.root / "chainman.lock").read_bytes(), self.initial_lock)

    @classmethod
    def stored_candidate(cls):
        if not hasattr(cls, "_candidate_tree"):
            with tempfile.TemporaryDirectory(
                prefix="chainman release fixture "
            ) as directory:
                source = Path(directory).resolve() / "candidate"
                source.mkdir()
                for directory in ("bootstrap", "scripts", "nix", "tests"):
                    (source / directory).mkdir()
                (source / "VERSION").write_text("2.0.0\n")
                (source / "bootstrap/chainman.sh").write_text("#!/bin/sh\nexit 0\n")
                (source / "bootstrap/fetch.nix").write_text("{}\n")
                (source / "nix/flake.nix").write_text(
                    'throw "fixture must never evaluate"\n'
                )
                (source / "nix/flake.lock").write_text("{}\n")
                for name in (
                    "scripts/chainman.py",
                    "scripts/chainman_updates.py",
                    "tests/test_candidate.py",
                ):
                    (source / name).write_text(
                        "raise RuntimeError('fixture must never execute')\n"
                    )
                cls._candidate_tree = Path(
                    subprocess.check_output(
                        [
                            "nix",
                            "--extra-experimental-features",
                            "nix-command flakes",
                            "store",
                            "add-path",
                            str(source),
                        ],
                        text=True,
                    ).strip()
                )
        return cls._candidate_tree

    def test_latest_eligible_major_is_selected_with_young_release_excluded(self):
        candidate = self.stored_candidate()
        with patch.object(
            toolchain,
            "managed_run",
            return_value=subprocess.CompletedProcess(["nix"], 0, str(candidate) + "\n"),
        ) as fetch:
            selected = consumer_updates.runtime_candidate(
                self.root, self.policy, self.now, gc_root=self.base / "runtime-root"
            )
        self.assertEqual(selected, candidate)
        lock = json.loads((self.root / "chainman.lock").read_text())
        self.assertEqual(lock["version"], "2.0.0")
        self.assertEqual(lock["revision"], "b" * 40)
        self.assertEqual(lock["narHash"], self.metadata["narHash"])
        self.assertIn("--out-link", fetch.call_args.args[0])
        self.assert_downloaded_exact_assets()

    def test_bundled_checksum_failure_never_commits_or_replaces_bundle(self):
        candidate = self.stored_candidate()
        old = json.loads(self.initial_lock)
        old["bundled_archive"] = "vendor/chainman/chainman.tar.gz"
        self.write("chainman.lock", json.dumps(old))
        self.write("vendor/chainman/chainman.tar.gz", "previous verified archive")
        self.write(".gitignore", ".cache/\n.chainman/\n")
        self.write("chainman.toml", "schema=1\n[updates]\nminimum_age_days=30\n")
        initial = self.init_git()
        with (
            patch.object(
                toolchain,
                "managed_run",
                return_value=subprocess.CompletedProcess(
                    ["nix"], 0, str(candidate) + "\n"
                ),
            ),
            patch.dict(self.metadata, {"archive_sha256": "0" * 64}),
            patch.object(consumer_updates, "verify") as verify,
        ):
            with self.assertRaisesRegex(ValueError, "checksum"):
                consumer_updates.runtime_candidate(
                    self.root, self.policy, self.now, gc_root=self.base / "runtime-root"
                )
            verify.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), initial)
        self.assertEqual(
            (self.root / "vendor/chainman/chainman.tar.gz").read_text(),
            "previous verified archive",
        )
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")

    def test_non_store_fetch_result_is_rejected(self):
        with patch.object(
            toolchain,
            "managed_run",
            return_value=subprocess.CompletedProcess(["nix"], 0, str(self.base) + "\n"),
        ):
            with self.assertRaisesRegex(ValueError, "Nix store"):
                consumer_updates.runtime_candidate(
                    self.root, self.policy, self.now, gc_root=self.base / "runtime-root"
                )

    def test_candidate_runtime_drives_resolution_and_verification(self):
        candidate = Path("/nix/store/00000000000000000000000000000000-candidate")
        with (
            patch.object(consumer_updates, "runtime_candidate", return_value=candidate),
            patch.object(toolchain, "managed_run") as execute,
        ):
            selected = consumer_updates.perform(
                self.root, self.policy, self.now, ["two words"], skip_runtime=False
            )
            self.assertEqual(selected, candidate)
            consumer_updates.verify(self.root, self.policy, selected)
        commands = [call.args[0] for call in execute.call_args_list]
        self.assertTrue(
            any(
                str(candidate / "scripts/chainman_updates.py") in c
                and "--resolve-root" in c
                for c in commands
            )
        )
        self.assertTrue(
            any(
                str(candidate / "scripts/chainman_updates.py") in c
                and "--verify-root" in c
                for c in commands
            )
        )
        self.assertFalse(any(str(candidate / "tests") in c for c in commands))
        for call in execute.call_args_list:
            self.assertEqual(call.kwargs["env"]["TOOLCHAIN_FRESH"], "1")


if __name__ == "__main__":
    unittest.main()
