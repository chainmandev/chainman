"""Managed child lifetime and disposal boundaries protect active build outputs."""

from contextlib import chdir
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import tarfile
import textwrap
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import toolchain
import native_tasks


class NixReferenceTests(unittest.TestCase):
    def test_source_entry_uses_pinned_bash_when_primary_input_cannot_supply_it(self):
        self.pinned_bash_entry_case(bootstrap=False)

    def test_bootstrap_uses_pinned_bash_when_primary_input_cannot_supply_it(self):
        self.pinned_bash_entry_case(bootstrap=True)

    def pinned_bash_entry_case(self, *, bootstrap):
        with tempfile.TemporaryDirectory(prefix="chainman shell entry ") as temporary:
            root = Path(temporary).resolve()
            (root / "scripts").mkdir()
            (root / "nix/broken").mkdir(parents=True)
            shutil.copy2(
                toolchain.RUNTIME / "scripts/enter.sh", root / "scripts/enter.sh"
            )
            # Nix develop obtains Bash from the input named nixpkgs even when
            # another input supplies the actual shell on this platform.
            (root / "nix/broken/flake.nix").write_text(
                "{ outputs = { self }: { legacyPackages = "
                'throw "fixture primary input does not support this platform"; }; }'
            )
            (root / "nix/flake.nix").write_text(
                "{ inputs.base.url = "
                + json.dumps(
                    toolchain.nix_path_reference(
                        toolchain.RUNTIME / "nix", ""
                    ).removesuffix("#")
                )
                + "; inputs.nixpkgs.url = "
                + json.dumps(
                    toolchain.nix_path_reference(root / "nix/broken", "").removesuffix(
                        "#"
                    )
                )
                + "; "
                "outputs = { base, ... }: { inherit (base) devShells packages; }; }"
            )
            subprocess.run(
                [
                    toolchain.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "flake",
                    "lock",
                ],
                cwd=root / "nix",
                check=True,
                capture_output=True,
                timeout=120,
            )
            binaries = root / "host-bin"
            binaries.mkdir()
            (binaries / "nix").symlink_to(toolchain.nix_command())
            (binaries / "bash").write_text("#!/bin/sh\nexit 77\n")
            (binaries / "bash").chmod(0o755)
            (binaries / "uname").write_text(
                '#!/bin/sh\ncase "$1" in -s) echo Darwin;; -m) echo x86_64;; *) exit 2;; esac\n'
            )
            (binaries / "uname").chmod(0o755)
            command = [
                str(root / "scripts/enter.sh"),
                "core",
                "python3",
                "-c",
                "import sys; print(sys.argv[1]); raise SystemExit(7)",
                "literal ' $ value",
            ]
            env = dict(
                os.environ,
                PATH=str(binaries) + os.pathsep + os.defpath,
                CHAINMAN_NIX_BIN=toolchain.nix_command(),
                TOOLCHAIN_FRESH="1",
            )
            if bootstrap:
                runtime = root / "runtime"
                shutil.copytree(root / "nix", runtime / "nix")
                (runtime / "scripts").mkdir()
                (runtime / "scripts/chainman.py").write_text(
                    "import sys; print(sys.argv[-1]); raise SystemExit(7)\n"
                )
                nar_hash = subprocess.check_output(
                    [
                        toolchain.nix_command(),
                        "--extra-experimental-features",
                        "nix-command",
                        "hash",
                        "path",
                        str(runtime),
                    ],
                    text=True,
                    timeout=30,
                ).strip()
                consumer = root / "consumer"
                (consumer / "scripts").mkdir(parents=True)
                shutil.copy2(
                    toolchain.RUNTIME / "bootstrap/chainman.sh",
                    consumer / "scripts/chainman.sh",
                )
                shutil.copy2(
                    toolchain.RUNTIME / "bootstrap/fetch.nix",
                    consumer / "scripts/chainman-fetch.nix",
                )
                with tarfile.open(consumer / "bundle.tar.gz", "w:gz") as archive:
                    archive.add(runtime, arcname="runtime")
                (consumer / "chainman.lock").write_text(
                    json.dumps(
                        {
                            "schema": 1,
                            "version": "fixture",
                            "revision": "fixture-only",
                            "url": "https://example.invalid/runtime.tar.gz",
                            "narHash": nar_hash,
                            "bundled_archive": "bundle.tar.gz",
                        }
                    )
                )
                command = [str(consumer / "scripts/chainman.sh"), "literal ' $ value"]
                # Keep runtime/cache identity local to this disposable fixture.
                env = {
                    key: value
                    for key, value in env.items()
                    if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
                }
                env.update(
                    CHAINMAN_MODE="host-nix",
                    CHAINMAN_NIX_BIN=toolchain.nix_command(),
                    XDG_CACHE_HOME=str(root / "cache"),
                )
            result = subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(result.stdout, "literal ' $ value\n")

    def test_native_task_from_copied_runtime_keeps_output_and_exit_status(self):
        with tempfile.TemporaryDirectory(
            prefix="chainman native runtime "
        ) as temporary:
            root = Path(temporary).resolve()
            runtime = root / "runtime # ? % ü"
            shutil.copytree(toolchain.RUNTIME / "nix", runtime / "nix")
            with patch.object(native_tasks.chainman, "RUNTIME", runtime):
                with native_tasks.command(
                    root,
                    [
                        [
                            sys.executable,
                            "-c",
                            "print('native fixture'); raise SystemExit(7)",
                        ]
                    ],
                    {},
                ) as command:
                    result = toolchain.managed_run(
                        command, text=True, capture_output=True, timeout=30
                    )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(result.stdout, "native fixture\n")

    def test_nix_resolves_runtime_paths_with_spaces_and_uri_characters(self):
        with tempfile.TemporaryDirectory(prefix="chainman reference ") as temporary:
            root = Path(temporary).resolve() / "runtime # ? % ü"
            root.mkdir()
            (root / "flake.nix").write_text(
                '{ outputs = { self }: { answer = "correct runtime"; }; }'
            )
            result = subprocess.run(
                [
                    toolchain.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "eval",
                    "--raw",
                    "--no-write-lock-file",
                    toolchain.nix_path_reference(root, "answer"),
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "correct runtime")


class SetupCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman setup stamp ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "scripts").mkdir()
        entry = self.root / "scripts/enter.sh"
        entry.write_text('#!/bin/sh\nshift\nexec "$@"\n')
        entry.chmod(0o755)
        (self.root / "toolchain.toml").write_text('schema=1\nmodules=["demo"]\n')
        self.spec = {
            "name": "demo",
            "directory": ".",
            "profile": "core",
            "inputs": ["input.txt"],
            "artifacts": ["ready.txt"],
            "commands": {
                "setup": [
                    [
                        sys.executable,
                        "-c",
                        "import os, pathlib, sys; "
                        "p=pathlib.Path('runs.txt'); p.write_text(p.read_text()+'run\\n' if p.exists() else 'run\\n'); "
                        "sys.exit(23) if os.environ.get('SETUP_FIXTURE_FAIL') else None; "
                        "pathlib.Path('ready.txt').write_text('ready')",
                    ]
                ]
            },
        }
        self.stamp = self.root / ".cache/toolchain/setup/demo.json"

    def setup(self, *, fail=False):
        toolchain.setup(
            self.spec,
            dict(os.environ, SETUP_FIXTURE_FAIL="1" if fail else ""),
            self.root,
        )

    def test_invalid_cache_shapes_rebuild_once_and_then_reuse_ready_outputs(self):
        invalid = [b"null", b"[]", b'"old stamp"', b"42", b"true", b"{", b"\xff"]
        self.stamp.parent.mkdir(parents=True)
        for body in invalid:
            with self.subTest(body=body):
                (self.root / "runs.txt").unlink(missing_ok=True)
                self.stamp.write_bytes(body)
                self.setup()
                self.setup()
                self.assertEqual((self.root / "runs.txt").read_text(), "run\n")
                self.assertEqual(
                    json.loads(self.stamp.read_text()),
                    {"fingerprint": toolchain.fingerprint(self.spec, self.root)},
                )

    def test_missing_outputs_changed_inputs_and_failed_setup_do_not_reuse_a_stamp(self):
        self.setup()
        self.setup()
        self.assertEqual((self.root / "runs.txt").read_text(), "run\n")
        (self.root / "ready.txt").unlink()
        previous = self.stamp.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.setup(fail=True)
        self.assertEqual(error.exception.returncode, 23)
        self.assertEqual(self.stamp.read_bytes(), previous)
        self.assertFalse((self.root / "ready.txt").exists())
        self.setup()
        (self.root / "input.txt").write_text("changed declared input")
        self.setup()
        self.setup()
        self.assertEqual((self.root / "runs.txt").read_text(), "run\n" * 4)


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Keep provisioning outside bounded child-command assertions, and retain
        # the actual native package while those subprocesses are being tested.
        runtime = toolchain.contextlib.ExitStack()
        cls.addClassCleanup(runtime.close)
        runtime.enter_context(
            native_tasks.command(
                Path(__file__).resolve().parents[1], [["true"]], {"shutdown_seconds": 1}
            )
        )

    def setUp(self):
        # Each fixture is an independent project. Its raw subprocesses must not
        # advertise the outer test runner's FD without inheriting that descriptor.
        fixture_env = dict(os.environ)
        fixture_env.pop("TOOLCHAIN_LOCK_FD", None)
        isolated = patch.dict(os.environ, fixture_env, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="toolchain runtime ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "toolchain.toml").write_text(
            'schema=1\nmodules=["core"]\n[cache]\nbuild_limit_gib=0\nstale_hours=0\n'
        )

    def test_gradle_defaults_bound_build_jvms_and_preserve_explicit_options(self):
        with patch.dict(os.environ):
            os.environ.pop("GRADLE_OPTS", None)
            options = toolchain.environment(self.root)["GRADLE_OPTS"]
            self.assertIn("-Dorg.gradle.daemon=false", options)
            self.assertIn(
                "-Dorg.gradle.project.kotlin.compiler.execution.strategy=in-process",
                options,
            )
            os.environ["GRADLE_OPTS"] = "-Dfixture=explicit"
            self.assertEqual(
                toolchain.environment(self.root)["GRADLE_OPTS"], "-Dfixture=explicit"
            )

    def cache_fixture(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        enter = scripts / "enter.sh"
        enter.write_text('#!/bin/sh\nshift\nexec "$@"\n')
        enter.chmod(0o755)
        server = scripts / "sccache"
        server.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent("""\
                import os, socket, sys, time
                from pathlib import Path
                endpoint = os.environ["SCCACHE_SERVER_UDS"]
                if os.environ.get("CACHE_DIAGNOSTICS"):
                    print("cache stop" if "--stop-server" in sys.argv else "cache supervisor", flush=True)
                if "--stop-server" in sys.argv:
                    if os.environ.get("FAIL_STOP"):
                        sys.exit(42)
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(endpoint)
                        client.sendall(b"stop")
                    if os.environ.get("REPLACE_SOCKET"):
                        deadline = time.monotonic() + 5
                        while not Path("server-exited").exists():
                            if time.monotonic() > deadline:
                                sys.exit(44)
                            time.sleep(0.01)
                        Path(endpoint).unlink()
                        Path(endpoint).write_bytes(b"replacement")
                else:
                    os.fstat(int(os.environ["TOOLCHAIN_LOCK_FD"]))
                    Path("server-idle-timeout").write_text(os.environ["SCCACHE_IDLE_TIMEOUT"])
                    with socket.socket(socket.AF_UNIX) as listener:
                        listener.bind(endpoint)
                        listener.listen()
                        listener.settimeout(0.05)
                        while True:
                            if Path("exit-server").exists():
                                listener.close()
                                time.sleep(float(os.environ.get("EXIT_DELAY", "0")))
                                sys.exit(int(Path("exit-server").read_text()))
                            try:
                                connection, _ = listener.accept()
                            except socket.timeout:
                                continue
                            with connection:
                                if connection.recv(16) == b"stop":
                                    break
                    Path("server-exited").write_text("yes")
                """)
        )
        server.chmod(0o755)
        env = toolchain.environment(self.root)
        env["PATH"] = str(scripts) + os.pathsep + env["PATH"]
        return env

    def test_cache_diagnostics_preserve_project_output_and_exit_status(self):
        env = self.cache_fixture()
        env["CACHE_DIAGNOSTICS"] = "1"
        (self.root / "scripts/enter.sh").write_text(
            "#!/bin/sh\nshift\n"
            'if [ "${5:-}" = "chainman-compiler" ]; then echo "cache setup"; fi\n'
            'exec "$@"\n'
        )
        wrapper = textwrap.dedent("""\
            import os, subprocess, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import toolchain
            root = Path(sys.argv[2])
            spec = {
                "profile": "rust",
                "directory": ".",
                "commands": {"verify": [[sys.executable, "-c",
                    "import sys; print('project output'); print('project diagnostic', file=sys.stderr); sys.exit(" + sys.argv[3] + ")"]]},
            }
            try:
                with toolchain.operation(root):
                    toolchain.run_commands(spec, "verify", dict(os.environ), root)
            except subprocess.CalledProcessError as failure:
                sys.exit(failure.returncode)
            """)
        for status in (0, 23):
            with self.subTest(status=status):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        wrapper,
                        str(Path(toolchain.__file__).parent),
                        str(self.root),
                        str(status),
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout, "project output\n")
                for diagnostic in (
                    "cache setup",
                    "cache supervisor",
                    "cache stop",
                    "project diagnostic",
                ):
                    self.assertIn(diagnostic + "\n", result.stderr)
                self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())

    def test_dead_launcher_does_not_authorize_unlinking_a_live_compiler_socket(self):
        env = self.cache_fixture()
        real_popen = subprocess.Popen
        launchers = []

        def launch(argv, **kwargs):
            process = real_popen(argv, **kwargs)
            if kwargs.get("env", {}).get("SCCACHE_START_SERVER") == "1":
                launchers.append(process)
            return process

        endpoint = Path(env["SCCACHE_SERVER_UDS"])
        with (
            toolchain.operation(self.root),
            patch.object(toolchain.subprocess, "Popen", side_effect=launch),
        ):
            try:
                with self.assertRaisesRegex(ValueError, "still active"):
                    with toolchain.compiler_cache("rust", env, self.root):
                        launchers[0].kill()
                        launchers[0].wait(timeout=5)
                self.assertTrue(
                    endpoint.exists(), "A surviving compiler owns this endpoint"
                )
            finally:
                subprocess.run(
                    [str(self.root / "scripts/sccache"), "--stop-server"],
                    env=env,
                    check=True,
                )
                # The compiler is a child of the killed launcher. Its inherited
                # lease, rather than parentage, establishes completion here.
                deadline = time.monotonic() + 5
                for lease in (self.root / ".cache/toolchain").glob("compiler-*.lock"):
                    with lease.open("a") as stream:
                        while True:
                            try:
                                toolchain.fcntl.flock(
                                    stream,
                                    toolchain.fcntl.LOCK_EX | toolchain.fcntl.LOCK_NB,
                                )
                                break
                            except BlockingIOError:
                                if time.monotonic() >= deadline:
                                    self.fail("Owned compiler did not exit")
                                time.sleep(0.01)

    def test_owned_cache_exits_and_releases_lock_after_success_and_failure(self):
        env = self.cache_fixture()
        for status in (0, 23):
            with self.subTest(status=status):
                spec = {
                    "name": "rust",
                    "profile": "rust",
                    "directory": ".",
                    "commands": {
                        "verify": [
                            [sys.executable, "-c", f"import sys; sys.exit({status})"]
                        ]
                    },
                }
                with toolchain.operation(self.root):
                    if status:
                        with self.assertRaises(subprocess.CalledProcessError) as raised:
                            toolchain.run_commands(spec, "verify", env, self.root)
                        self.assertEqual(raised.exception.returncode, status)
                    else:
                        toolchain.run_commands(spec, "verify", env, self.root)
                with toolchain.operation(self.root):
                    self.assertEqual((self.root / "server-exited").read_text(), "yes")
                self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())
                (self.root / "server-exited").unlink()

    def test_cache_shutdown_does_not_reenter_the_environment(self):
        env = self.cache_fixture()
        with toolchain.operation(self.root):
            with toolchain.compiler_cache("rust", env, self.root):
                # Simulate an unavailable launcher after successful startup.
                (self.root / "scripts/enter.sh").write_text("#!/bin/sh\nexit 37\n")
            self.assertEqual((self.root / "server-exited").read_text(), "yes")
            self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())
            self.assertEqual(
                list((self.root / ".cache/toolchain").glob("compiler-*.lock")), []
            )
        with toolchain.operation(self.root):
            pass

    def test_cache_refuses_existing_endpoint_without_mutating_it(self):
        env = self.cache_fixture()
        endpoint = Path(env["SCCACHE_SERVER_UDS"])
        endpoint.write_bytes(b"unowned")
        self.addCleanup(endpoint.unlink)
        with toolchain.operation(self.root):
            with self.assertRaisesRegex(ValueError, "already exists"):
                with toolchain.compiler_cache("rust", env, self.root):
                    self.fail("Started work at an unowned endpoint")
        self.assertEqual(endpoint.read_bytes(), b"unowned")

    def test_owned_server_disables_idle_exit_and_reaps_early_exit(self):
        env = self.cache_fixture()
        env["SCCACHE_IDLE_TIMEOUT"] = "1"
        env["EXIT_DELAY"] = "0.3"
        endpoint = Path(env["SCCACHE_SERVER_UDS"])
        for status in (0, 29):
            with self.subTest(status=status):
                with toolchain.operation(self.root):
                    with (
                        self.assertRaisesRegex(ValueError, "status 29")
                        if status
                        else toolchain.contextlib.nullcontext()
                    ):
                        with toolchain.compiler_cache("rust", env, self.root):
                            self.assertEqual(
                                (self.root / "server-idle-timeout").read_text(), "0"
                            )
                            (self.root / "exit-server").write_text(str(status))
                            deadline = time.monotonic() + 5
                            while True:
                                try:
                                    with toolchain.socket.socket(
                                        toolchain.socket.AF_UNIX
                                    ) as connection:
                                        connection.connect(str(endpoint))
                                except ConnectionRefusedError:
                                    break
                                if time.monotonic() >= deadline:
                                    self.fail("Owned test server did not exit")
                                time.sleep(0.01)
                self.assertFalse(endpoint.exists())
                with toolchain.operation(self.root):
                    pass
                (self.root / "exit-server").unlink()

    def test_failed_environment_realization_never_starts_a_cache_server(self):
        env = self.cache_fixture()
        enter = self.root / "scripts/enter.sh"
        enter.write_text("#!/bin/sh\nexit 37\n")
        with toolchain.operation(self.root):
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                with toolchain.compiler_cache("rust", env, self.root):
                    self.fail("Unrealized environment entered work")
            self.assertEqual(failure.exception.returncode, 37)
        with toolchain.operation(self.root):
            self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())

    def test_cache_stop_failure_preserves_command_error_and_reaps_owned_group(self):
        env = self.cache_fixture()
        env["FAIL_STOP"] = "1"
        original_popen = subprocess.Popen
        processes = []

        def capture(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        spec = {
            "name": "rust",
            "profile": "rust",
            "directory": ".",
            "commands": {
                "verify": [[sys.executable, "-c", "import sys; sys.exit(23)"]]
            },
        }
        try:
            with patch.object(subprocess, "Popen", side_effect=capture):
                with toolchain.operation(self.root):
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        toolchain.run_commands(spec, "verify", env, self.root)
            self.assertEqual(raised.exception.returncode, 23)
            self.assertIn("cleanup also failed", raised.exception.__notes__[0])
            with toolchain.operation(self.root):
                self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())
        finally:
            for process in processes:
                process.wait(timeout=5)

    def test_slow_server_startup_is_terminated_and_releases_its_lifetime(self):
        env = self.cache_fixture()
        server = self.root / "scripts/sccache"
        body = server.read_text().replace(
            'os.fstat(int(os.environ["TOOLCHAIN_LOCK_FD"]))',
            'os.fstat(int(os.environ["TOOLCHAIN_LOCK_FD"]))\n    Path("starting-server").write_text(str(os.getpid()))\n    time.sleep(30)',
        )
        server.write_text(body)
        with (
            toolchain.operation(self.root),
            patch.object(toolchain, "_COMPILER_STARTUP_SECONDS", 1),
        ):
            with self.assertRaisesRegex(ValueError, "startup timed out"):
                with toolchain.compiler_cache("rust", env, self.root):
                    self.fail("Unready compiler entered work")
        self.assertTrue((self.root / "starting-server").exists())
        with toolchain.operation(self.root):
            self.assertFalse(Path(env["SCCACHE_SERVER_UDS"]).exists())
            self.assertEqual(
                list((self.root / ".cache/toolchain").glob("compiler-*.lock")), []
            )

    def test_cache_preserves_endpoint_replaced_during_shutdown(self):
        env = self.cache_fixture()
        env["REPLACE_SOCKET"] = "1"
        endpoint = Path(env["SCCACHE_SERVER_UDS"])
        self.addCleanup(lambda: endpoint.unlink(missing_ok=True))
        with toolchain.operation(self.root):
            with self.assertRaisesRegex(ValueError, "changed"):
                with toolchain.compiler_cache("rust", env, self.root):
                    pass
        with toolchain.operation(self.root):
            self.assertEqual(endpoint.read_bytes(), b"replacement")

    def test_relative_download_cache_overrides_fail_before_creation(self):
        for overrides in (
            {"TOOLCHAIN_DOWNLOAD_CACHE": "relative-cache"},
            {"XDG_CACHE_HOME": "relative-cache"},
        ):
            with self.subTest(overrides=overrides):
                env = dict(os.environ)
                env.pop("TOOLCHAIN_DOWNLOAD_CACHE", None)
                env.update(overrides)
                with patch.dict(os.environ, env, clear=True), chdir(self.root):
                    with self.assertRaisesRegex(ValueError, "absolute"):
                        toolchain.environment(self.root)
        self.assertFalse((self.root / "relative-cache").exists())

    def test_pnpm_store_defaults_ignore_unselected_caller_settings(self):
        downloads = self.root / "shared downloads"
        overrides = dict.fromkeys(toolchain.PNPM_STORE_VARIABLES, "/unselected")
        overrides["TOOLCHAIN_DOWNLOAD_CACHE"] = str(downloads)
        with patch.dict(os.environ, overrides):
            env = toolchain.environment(self.root)
        for name in toolchain.PNPM_STORE_VARIABLES:
            self.assertEqual(env[name], str(downloads / "pnpm"))

    def test_preserved_pnpm_store_alias_controls_every_interpreter(self):
        for selected in toolchain.PNPM_STORE_VARIABLES:
            with self.subTest(selected=selected):
                (self.root / "toolchain.toml").write_text(
                    'schema=1\nmodules=["core"]\n[cache]\n'
                    f'preserve_environment=["{selected}"]\n'
                )
                overrides = dict.fromkeys(toolchain.PNPM_STORE_VARIABLES, "/ignored")
                overrides[selected] = str(self.root / "selected store")
                with patch.dict(os.environ, overrides):
                    env = toolchain.environment(self.root)
                for name in toolchain.PNPM_STORE_VARIABLES:
                    self.assertEqual(env[name], overrides[selected])

    def test_pnpm_task_policy_does_not_inherit_nested_install_defaults(self):
        unsafe = {
            "pnpm_config_verify_deps_before_run": "install",
            "npm_config_enable_global_virtual_store": "true",
        }
        with patch.dict(os.environ, unsafe):
            env = toolchain.environment(self.root)
        for aliases, value in zip(
            toolchain.PNPM_SETTING_VARIABLES[1:], ("false", "error"), strict=True
        ):
            for name in aliases:
                self.assertEqual(env[name], value)

    def test_explicit_pnpm_policy_layers_reconcile_all_aliases(self):
        env = toolchain.environment(self.root)
        toolchain.pnpm_environment(
            env, {"PNPM_CONFIG_ENABLE_GLOBAL_VIRTUAL_STORE": "true"}
        )
        for name in toolchain.PNPM_SETTING_VARIABLES[1]:
            self.assertEqual(env[name], "true")
        toolchain.pnpm_environment(env, {"npm_config_verify_deps_before_run": "warn"})
        for name in toolchain.PNPM_SETTING_VARIABLES[2]:
            self.assertEqual(env[name], "warn")

    def test_special_operation_lock_fails_without_blocking(self):
        directory = self.root / ".cache/toolchain"
        directory.mkdir(parents=True)
        os.mkfifo(directory / "operation.lock")
        command = "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import toolchain; toolchain.operation(Path(sys.argv[2])).__enter__()"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                command,
                str(Path(toolchain.__file__).parent),
                str(self.root),
            ],
            capture_output=True,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"regular file", result.stderr)

    def test_generated_inputs_and_outputs_refuse_symlink_escapes(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("generate.py", "toolchain_build.py", "toolchain.py"):
            shutil.copyfile(Path(toolchain.__file__).parent / name, scripts / name)
        source = self.root / "examples/core"
        source.mkdir(parents=True)
        (source / "labels.json").write_text('{"a":"b"}')
        (source / "labels.txt").write_text("a=b\n")
        (source / "greeting.py").write_text("print('hello')\n")
        outside = self.root / "kept"
        outside.mkdir()
        for script, target in (
            ("generate.py", source / "labels.txt"),
            ("toolchain_build.py", self.root / "dist"),
            ("generate.py", source / "labels.json"),
            ("toolchain_build.py", source / "greeting.py"),
        ):
            with self.subTest(script=script, target=target.name):
                saved = target.read_bytes() if target.exists() else None
                if target.exists():
                    target.unlink()
                canary = outside if target.name == "dist" else outside / "canary"
                if canary != outside:
                    canary.write_bytes(saved)
                target.symlink_to(canary, target_is_directory=canary.is_dir())
                before = {
                    p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()
                }
                result = subprocess.run(
                    [sys.executable, str(scripts / script)],
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(b"symlink", result.stderr)
                self.assertEqual(
                    before,
                    {p.name: p.read_bytes() for p in outside.iterdir() if p.is_file()},
                )
                target.unlink()
                if saved is not None:
                    target.write_bytes(saved)

    def test_atomic_output_failure_keeps_old_bytes_and_cleans_temporary(self):
        target = self.root / "output"
        target.write_bytes(b"original")
        before = set(self.root.iterdir())
        with patch.object(toolchain.os, "fsync", side_effect=OSError("disk fault")):
            with self.assertRaisesRegex(OSError, "disk fault"):
                toolchain.atomic_bytes(target, b"replacement", 0o644)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_symlinked_cache_escape_is_rejected_without_deleting(self):
        outside = self.root / "kept"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        base = self.root / ".cache/toolchain/work"
        base.mkdir(parents=True)
        (base / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            toolchain.prune(self.root, all_outputs=True)
        self.assertEqual((outside / "precious").read_text(), "keep")

    def test_deletion_failure_is_not_reported_as_reclaimed_space(self):
        entry = self.root / ".cache/toolchain/work/old"
        entry.mkdir(parents=True)
        (entry / "output").write_text("build")
        with patch.object(
            toolchain.shutil, "rmtree", side_effect=PermissionError("in use")
        ):
            with self.assertRaises(PermissionError):
                toolchain.prune(self.root, all_outputs=True)
        self.assertTrue(entry.exists())

    def test_internal_build_alias_is_counted_once_and_disposable(self):
        entry = self.root / ".cache/toolchain/work/swift"
        (entry / "target/debug").mkdir(parents=True)
        (entry / "target/debug/program").write_bytes(b"executable")
        (entry / "debug").symlink_to("target/debug", target_is_directory=True)
        self.assertEqual(
            toolchain.size(entry), len(b"executable") + len("target/debug")
        )
        with toolchain.operation(self.root):
            self.assertEqual(
                toolchain.prune(self.root, all_outputs=True),
                [".cache/toolchain/work/swift"],
            )
        self.assertFalse(entry.exists())

    def test_nested_escape_rejects_whole_prune_before_any_deletion(self):
        base = self.root / ".cache/toolchain/work"
        for name in ("old", "new"):
            (base / name).mkdir(parents=True)
            (base / name / "build").write_text("keep")
        (base / "new/escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlink"):
            toolchain.prune(self.root, all_outputs=True)
        self.assertEqual((base / "old/build").read_text(), "keep")

    def test_cache_report_does_not_follow_outside_links(self):
        entry = self.root / "report"
        entry.mkdir()
        target = self.root / "outside"
        target.write_bytes(b"x" * 1000)
        (entry / "link").symlink_to(target)
        self.assertEqual(toolchain.size(entry, reporting=True), len(str(target)))

    def test_virtual_environment_interpreter_readiness_is_narrow(self):
        interpreter = self.root / "example/.venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        expected = os.environ["UV_PYTHON"]
        interpreter.symlink_to(expected)
        artifact = {"path": "example/.venv/bin/python", "interpreter": "python"}
        self.assertTrue(
            toolchain.artifact_ready(self.root, artifact, {"UV_PYTHON": expected})
        )
        interpreter.unlink()
        interpreter.symlink_to(self.root / "unrelated")
        self.assertFalse(
            toolchain.artifact_ready(self.root, artifact, {"UV_PYTHON": expected})
        )
        with self.assertRaisesRegex(ValueError, "symlink"):
            toolchain.contained(self.root, "example/.venv/bin/python")

    def test_direct_child_retains_lock_after_wrapper_termination(self):
        self.child_retains_lock_after_wrapper_termination(exclusive=True)

    def test_concurrent_child_retains_lease_after_wrapper_termination(self):
        self.child_retains_lock_after_wrapper_termination(exclusive=False)

    def test_child_retains_both_project_leases_after_wrapper_termination(self):
        self.child_retains_lock_after_wrapper_termination(exclusive=False, another=True)

    def child_retains_lock_after_wrapper_termination(self, *, exclusive, another=False):
        scripts = Path(toolchain.__file__).parent
        ready = self.root / "ready"
        child = self.root / "child.py"
        child.write_text(
            "import os,sys,time\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(30)\n"
        )
        wrapper = self.root / "wrapper.py"
        other = self.root / "other"
        other.mkdir()
        (other / "toolchain.toml").write_text('schema=1\nmodules=["core"]\n')
        extra = (
            ", toolchain.operation(Path(sys.argv[2]) / 'other', exclusive=False)"
            if another
            else ""
        )
        wrapper.write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nimport toolchain\n"
            f"with toolchain.operation(Path(sys.argv[2]), exclusive={exclusive!r}){extra}:\n"
            " toolchain.managed_run([sys.executable,sys.argv[3],sys.argv[4]],check=True)\n"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                str(wrapper),
                str(scripts),
                str(self.root),
                str(child),
                str(ready),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while (
            not ready.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not ready.exists():
            output = process.communicate(timeout=2)[1].decode()
            self.fail(f"Managed child did not start: {output}")
        pid = int(ready.read_text())
        try:
            process.terminate()
            process.wait(timeout=5)
            with self.assertRaisesRegex(ValueError, "active"):
                with toolchain.operation(self.root):
                    self.fail("Orphaned build lost cleanup protection")
            if another:
                with self.assertRaisesRegex(ValueError, "active"):
                    with toolchain.operation(other):
                        self.fail("Nested project lost cleanup protection")
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
