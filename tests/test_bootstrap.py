"""Bootstrap qualification uses real Nix and neutral temporary runtime archives."""

import json
import os
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
NIX = shutil.which("nix")


@unittest.skipUnless(NIX, "real Nix is required; no host-language bootstrap fallback")
class BootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix="chainman bootstrap assets ")
        cls.tree = Path(cls.shared.name) / "runtime"
        (cls.tree / "scripts").mkdir(parents=True)
        shutil.copytree(SOURCE / "nix", cls.tree / "nix")
        (cls.tree / "scripts/chainman.py").write_text(
            "import json, os, pathlib, subprocess, sys, tempfile, time\n"
            "root = pathlib.Path(sys.argv[2])\n"
            "record = dict(argv=sys.argv[1:], runtime=os.environ['CHAINMAN_RUNTIME'], "
            "root=os.environ['CHAINMAN_ROOT'], cwd=os.getcwd(), "
            "forward=os.environ.get('CHAINMAN_TEST_VALUE'), "
            "demo=os.environ.get('DEMO_TEST_VALUE'), container=os.environ.get('TOOLCHAIN_CONTAINER'))\n"
            "record.update(uid=os.getuid(), nix_config=os.environ.get('NIX_CONFIG'), tmpdir=os.environ.get('TMPDIR'))\n"
            "record.update(active_profile=os.environ.get('CHAINMAN_ACTIVE_PROFILE'), active_fingerprint=os.environ.get('CHAINMAN_ACTIVE_FINGERPRINT'))\n"
            "if pathlib.Path('/proc/self/status').exists(): record['cap_eff'] = next(line.split()[1] for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))\n"
            "if pathlib.Path('/proc/self/status').exists(): record['no_new_privs'] = next(line.split()[1] for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('NoNewPrivs:'))\n"
            "if (root / '.git').exists(): record['git_root'] = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()\n"
            "if os.environ.get('DEMO_TEST_ADMIN'):\n"
            " record['parent_admin_visible'] = pathlib.Path(os.environ['DEMO_TEST_ADMIN']).exists()\n"
            " record['git_name'] = subprocess.run(['git', 'config', '--get', 'user.name'], text=True, capture_output=True).stdout.strip()\n"
            "if os.environ.get('DEMO_TEST_FD'): os.fstat(int(os.environ['DEMO_TEST_FD']))\n"
            "if '--home-purity' in sys.argv:\n"
            " assert os.environ.get('TOOLCHAIN_CONTAINER') == '1'\n"
            " assert not pathlib.Path('/homeless-shelter').exists()\n"
            " try: pathlib.Path('/homeless-shelter').mkdir()\n"
            " except PermissionError: record['nix_home_blocked'] = True\n"
            " else:\n"
            "  pathlib.Path('/homeless-shelter').rmdir()\n"
            "  raise AssertionError('Nix build HOME could be created')\n"
            " record['root_mode'] = pathlib.Path('/').stat().st_mode & 0o7777\n"
            " record['root_writable'] = os.access('/', os.W_OK)\n"
            " record['tmp_mode'] = pathlib.Path('/tmp').stat().st_mode & 0o7777\n"
            " for label, directory in [('project', root), ('home', pathlib.Path.home()), ('tmp', pathlib.Path('/tmp')), ('nix', pathlib.Path('/nix/var')), ('downloads', pathlib.Path(os.environ['TOOLCHAIN_DOWNLOAD_CACHE']))]:\n"
            "  with tempfile.TemporaryFile(dir=directory) as handle:\n"
            "   handle.write(b'owned neutral fixture'); handle.flush()\n"
            "  record[label + '_writable'] = True\n"
            "if os.environ.get('DEMO_TEST_CACHE'):\n"
            " cache = pathlib.Path(os.environ['TOOLCHAIN_DOWNLOAD_CACHE']) / os.environ['DEMO_TEST_CACHE']\n"
            " record['cache_hits'] = int(cache.read_text()) + 1 if cache.exists() else 1\n"
            " cache.write_text(str(record['cache_hits']))\n"
            " (pathlib.Path.home() / 'home-marker').write_text('persistent')\n"
            "(root / ('record-' + str(os.getpid()) + '.json')).write_text(json.dumps(record))\n"
            "if '--wait' in sys.argv: time.sleep(60)\n"
        )
        cls.nar_hash = subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "path",
                str(cls.tree),
            ],
            text=True,
        ).strip()
        cls.archive = Path(cls.shared.name) / "runtime archive.tar.gz"
        with tarfile.open(cls.archive, "w:gz") as archive:
            archive.add(cls.tree, arcname="runtime")

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="chainman consumer fixture "
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "scripts").mkdir()
        self.launcher = self.root / "scripts/chainman.sh"
        shutil.copy2(SOURCE / "bootstrap/chainman.sh", self.launcher)
        shutil.copy2(
            SOURCE / "bootstrap/fetch.nix", self.root / "scripts/chainman-fetch.nix"
        )
        shutil.copy2(self.archive, self.root / "bundle.tar.gz")
        self.lock = {
            "schema": 1,
            "version": "test",
            "revision": "fixture-only",
            "url": "https://example.invalid/runtime.tar.gz",
            "narHash": self.nar_hash,
            "bundled_archive": "bundle.tar.gz",
        }
        self.write_lock()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CHAINMAN_", "TOOLCHAIN_", "GIT_CONFIG_"))
        }
        self.env.update(CHAINMAN_MODE="host-nix", CHAINMAN_NIX_BIN=NIX)

    def write_lock(self):
        (self.root / "chainman.lock").write_text(json.dumps(self.lock))

    def run_bootstrap(self, *args, check=True, env=None):
        result = subprocess.run(
            [str(self.launcher), *args],
            check=False,
            cwd="/",
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if check and result.returncode:
            self.fail(result.stdout + result.stderr)
        return result

    def records(self):
        return [
            json.loads(path.read_text()) for path in self.root.glob("record-*.json")
        ]

    def test_explicit_temporary_base_survives_both_bootstrap_shells(self):
        private = self.root / "private temporary files"
        private.mkdir(mode=0o700)
        env = dict(self.env, TMPDIR=str(private))
        self.run_bootstrap("status", env=env)
        self.assertEqual(self.records()[0]["tmpdir"], str(private))

    def test_carriage_return_in_project_path_is_rejected_before_nix(self):
        directory = self.root / "project\rwith carriage return"
        scripts = directory / "scripts"
        scripts.mkdir(parents=True)
        launcher = scripts / "chainman.sh"
        shutil.copy2(SOURCE / "bootstrap/chainman.sh", launcher)
        shutil.copy2(SOURCE / "bootstrap/fetch.nix", scripts / "chainman-fetch.nix")
        result = subprocess.run(
            [str(launcher), "status"],
            check=False,
            cwd="/",
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Newlines are not supported", result.stderr)
        self.assertNotIn("Missing regular chainman.lock", result.stderr)
        self.assertFalse((directory / ".chainman").exists())

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") and os.getuid() == 0,
        "requires a real container engine inside the isolated root user namespace",
    )
    def test_real_root_container_uses_single_user_nix_without_extra_capabilities(self):
        host_temporary = self.root / "host temporary files"
        host_temporary.mkdir(mode=0o700)
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            TMPDIR=str(host_temporary),
        )
        self.run_bootstrap("status", env=env)
        record = self.records()[0]
        self.assertEqual(record["uid"], 0)
        self.assertEqual(record["nix_config"], "build-users-group =")
        self.assertEqual(record["container"], "1")
        self.assertFalse(record["tmpdir"].startswith(str(host_temporary)))
        self.assertEqual(int(record["cap_eff"], 16), 0)
        self.assertEqual(
            next(self.root.glob("record-*.json")).stat().st_uid, os.getuid()
        )

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "requires the selected real container engine",
    )
    def test_container_identity_preserves_caller_output_ownership(self):
        engine = os.environ["CHAINMAN_TEST_CONTAINER"]
        expected_uid = os.getuid()
        if engine == "docker":
            options = subprocess.check_output(
                [engine, "info", "--format", "{{json .SecurityOptions}}"], text=True
            )
            if "name=rootless" in json.loads(options):
                expected_uid = 0
        env = dict(
            self.env, CHAINMAN_MODE="container-nix", CHAINMAN_CONTAINER_ENGINE=engine
        )
        self.run_bootstrap("status", env=env)
        record = self.records()[0]
        self.assertEqual(record["uid"], expected_uid)
        self.assertEqual(record["no_new_privs"], "1")
        self.assertEqual(int(record["cap_eff"], 16), 0)
        for path in [*self.root.glob("record-*.json"), self.root / ".chainman"]:
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(path.stat().st_gid, os.getgid())

    def test_failed_docker_identity_probe_stops_before_container_execution(self):
        binary = self.root / "mock engine"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text(
            '#!/bin/sh\nif [ "$1" = info ]; then exit 17; fi\nprintf "unexpected run"\nexit 98\n'
        )
        docker.chmod(0o755)
        env = dict(
            self.env,
            PATH=str(binary) + os.pathsep + self.env["PATH"],
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE="docker",
        )
        result = self.run_bootstrap("status", env=env, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Cannot determine Docker daemon identity mapping", result.stderr)
        self.assertNotIn("unexpected run", result.stdout)
        self.assertFalse((self.root / ".chainman").exists())

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "requires the selected real container engine",
    )
    def test_container_preserves_nonexistent_nix_home_and_writable_mounts(self):
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
        )
        self.run_bootstrap("--home-purity", env=env)
        record = self.records()[0]
        self.assertTrue(record["nix_home_blocked"])
        self.assertFalse(record["root_writable"])
        self.assertEqual(record["tmp_mode"], 0o1777)
        if record["uid"] == 0:
            self.assertEqual(record["root_mode"], 0o555)
        self.assertEqual(record["no_new_privs"], "1")
        self.assertEqual(int(record["cap_eff"], 16), 0)
        for label in ("project", "home", "tmp", "nix", "downloads"):
            self.assertTrue(record[label + "_writable"], label)

    def test_core_entry_invalidates_an_inherited_external_profile_token(self):
        self.run_bootstrap(
            "status",
            env=dict(
                self.env,
                CHAINMAN_ACTIVE_PROFILE="native",
                CHAINMAN_ACTIVE_FINGERPRINT="unchanged-project-inputs",
            ),
        )
        record = self.records()[0]
        self.assertIsNone(record["active_profile"])
        self.assertIsNone(record["active_fingerprint"])

    def test_verified_bundle_dispatch_and_repeated_generation(self):
        self.run_bootstrap("status", "argument with spaces", "", "$(not-a-command)")
        first = self.records()[0]
        self.assertEqual(
            first["argv"],
            [
                "--root",
                str(self.root),
                "status",
                "argument with spaces",
                "",
                "$(not-a-command)",
            ],
        )
        self.assertEqual(first["root"], str(self.root))
        self.assertEqual(first["cwd"], str(self.root))
        runtime = Path(first["runtime"])
        self.assertEqual(runtime.parent, self.root / ".chainman")
        self.assertFalse(runtime.is_symlink())
        self.run_bootstrap("status")
        self.assertEqual(
            {record["runtime"] for record in self.records()}, {str(runtime)}
        )

    def test_wrong_hash_fails_before_runtime_evaluation(self):
        self.lock["narHash"] = "sha256-" + "A" * 43 + "="
        self.write_lock()
        result = self.run_bootstrap(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / ".chainman").exists())
        self.assertEqual(self.records(), [])

    def test_missing_identity_fails_closed(self):
        for key in ("schema", "version", "revision", "url", "narHash"):
            with self.subTest(key=key):
                value = self.lock.pop(key)
                self.write_lock()
                self.assertNotEqual(self.run_bootstrap(check=False).returncode, 0)
                self.lock[key] = value
        self.assertFalse((self.root / ".chainman").exists())

    def test_local_archive_override_and_explicit_root(self):
        self.lock.pop("bundled_archive")
        self.write_lock()
        env = dict(
            self.env,
            CHAINMAN_ARCHIVE=str(self.archive),
            CHAINMAN_PROJECT_ROOT=str(self.root),
        )
        self.run_bootstrap("status", env=env)
        self.assertEqual(self.records()[0]["root"], str(self.root))

    def test_archive_and_cache_symlinks_fail(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / ".chainman").symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.run_bootstrap(check=False).returncode, 0)
        (self.root / ".chainman").unlink()
        (self.root / "bundle.tar.gz").unlink()
        (self.root / "bundle.tar.gz").symlink_to(self.archive)
        self.assertNotEqual(self.run_bootstrap(check=False).returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_cached_bytes_and_executable_mode_are_reverified(self):
        self.run_bootstrap()
        runtime = Path(self.records()[0]["runtime"])
        target = runtime / "scripts/chainman.py"
        original = target.read_bytes()
        target.chmod(0o755)
        self.assertNotEqual(self.run_bootstrap(check=False).returncode, 0)
        target.chmod(0o644)
        target.write_bytes(original + b"\n# changed\n")
        self.assertNotEqual(self.run_bootstrap(check=False).returncode, 0)
        self.assertEqual(len(self.records()), 1)

    def test_concurrent_first_install_is_atomic(self):
        commands = [
            subprocess.Popen(
                [str(self.launcher), "parallel"],
                cwd="/",
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        for command in commands:
            stdout, stderr = command.communicate(timeout=180)
            self.assertEqual(command.returncode, 0, stdout + stderr)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(len({record["runtime"] for record in self.records()}), 1)
        self.assertEqual(list((self.root / ".chainman").glob(".install-*")), [])

    def test_interrupted_execution_does_not_hold_bootstrap_lock(self):
        command = subprocess.Popen(
            [str(self.launcher), "--wait"],
            cwd="/",
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 120
            while (
                not self.records()
                and command.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            self.assertTrue(self.records(), "runtime did not start")
            runtime = Path(self.records()[0]["runtime"])
            os.killpg(command.pid, signal.SIGTERM)
            command.communicate(timeout=10)
            self.run_bootstrap("status")
            self.assertTrue(runtime.is_dir())
            self.assertEqual(len(self.records()), 2)
        finally:
            if command.poll() is None:
                os.killpg(command.pid, signal.SIGKILL)
                command.communicate()

    def test_host_nix_override_and_mode_are_validated(self):
        for overrides in (
            {"CHAINMAN_NIX_BIN": "relative/nix"},
            {"CHAINMAN_NIX_BIN": "nix"},
            {"CHAINMAN_MODE": "host-python"},
            {"CHAINMAN_ACTIVE_MODE": "container-nix"},
        ):
            with self.subTest(overrides=overrides):
                self.assertNotEqual(
                    self.run_bootstrap(
                        check=False, env=dict(self.env, **overrides)
                    ).returncode,
                    0,
                )

    def test_verified_upgrade_retains_old_generation_and_bad_candidate(self):
        self.run_bootstrap("old")
        old_runtime = Path(self.records()[0]["runtime"])
        candidate = self.root / "candidate"
        shutil.copytree(self.tree, candidate)
        (candidate / "revision").write_text("candidate")
        nar_hash = subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "path",
                str(candidate),
            ],
            text=True,
        ).strip()
        with tarfile.open(self.root / "candidate.tar.gz", "w:gz") as archive:
            archive.add(candidate, arcname="runtime")
        self.lock.update(
            version="candidate",
            revision="candidate",
            bundled_archive="candidate.tar.gz",
            narHash=nar_hash,
        )
        self.write_lock()
        self.run_bootstrap("new")
        self.assertTrue(old_runtime.is_dir())
        self.assertEqual(len({record["runtime"] for record in self.records()}), 2)
        self.lock["narHash"] = "sha256-" + "A" * 43 + "="
        self.write_lock()
        self.assertNotEqual(self.run_bootstrap(check=False).returncode, 0)
        self.assertTrue(old_runtime.is_dir())
        self.assertEqual(len(self.records()), 2)

    def test_host_python_is_not_used_and_inherited_descriptor_survives(self):
        tools = self.root / "host tools"
        tools.mkdir()
        python = tools / "python3"
        python.write_text(
            "#!/bin/sh\nprintf 'host Python must not execute\\n' >&2\nexit 97\n"
        )
        python.chmod(0o755)
        read_fd, write_fd = os.pipe()
        try:
            env = dict(
                self.env,
                PATH=str(tools) + os.pathsep + self.env["PATH"],
                DEMO_TEST_FD=str(write_fd),
            )
            result = subprocess.run(
                [str(self.launcher)],
                check=False,
                cwd="/",
                env=env,
                pass_fds=(write_fd,),
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.records()), 1)
        finally:
            os.close(read_fd)
            os.close(write_fd)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_real_container_rejects_unsupported_or_escaping_options(self):
        options = self.root / "options"
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            CHAINMAN_CONTAINER_OPTIONS_FILE=str(options),
        )
        for contents, message in (
            ("--label\nprobe=value\r\n", "Newlines are not supported"),
            ("--privileged\ntrue\n", "Unsupported container option"),
            ("--env-pattern\nHOME\n", "Unsupported container option"),
            ("--network\ncontainer:other\n", "network must be host or bridge"),
            (
                "--platform\nlinux/riscv64\n",
                "platform must be linux/amd64 or linux/arm64",
            ),
            ("--volume\n/:/outside\n", "Blanket host or socket mounts"),
            (
                f"--mount\ntype=bind,src={self.root},dst=/cache/../nix\n",
                "target must be normalized",
            ),
        ):
            with self.subTest(contents=contents):
                options.write_text(contents)
                result = self.run_bootstrap(check=False, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.records(), [])

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_real_container_linked_worktree_forwarding_and_literal_options(self):
        # The temporary fixture is an ordinary local repository, never a consumer checkout.
        repository = self.root / "original checkout"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        worktree = self.root / "linked checkout"
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "worktree",
                "add",
                "-q",
                "--detach",
                str(worktree),
            ],
            check=True,
        )
        for path in (
            self.root / "scripts",
            self.root / "bundle.tar.gz",
            self.root / "chainman.lock",
        ):
            if path.is_dir():
                shutil.copytree(path, worktree / path.name)
            else:
                shutil.copy2(path, worktree / path.name)
        (worktree / "chainman.toml").write_text('[environment]\npass=["DEMO_*"]\n')
        options = worktree / "options"
        persistent_home = worktree / ".cache/container-home"
        persistent_home.mkdir(parents=True)
        options.write_text(
            "--label\nchainman.fixture=literal value\n--hostname\nchainman-fixture\n"
            f"--mount\ntype=bind,src={persistent_home},dst=/tmp/chainman-home\n"
        )
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            CHAINMAN_PROJECT_ROOT=str(worktree),
            CHAINMAN_CONTAINER_OPTIONS_FILE=str(options),
            DEMO_TEST_VALUE="value with spaces",
            DEMO_TEST_CACHE="fixture-" + worktree.parent.name,
        )
        self.run_bootstrap("status", env=env)
        record = json.loads(next(worktree.glob("record-*.json")).read_text())
        self.assertEqual(record["demo"], "value with spaces")
        self.assertEqual(record["container"], "1")
        self.assertEqual(record["root"], str(worktree))
        self.assertEqual(record["git_root"], str(worktree))
        self.assertEqual(record["cache_hits"], 1)
        self.assertEqual((persistent_home / "home-marker").read_text(), "persistent")
        self.run_bootstrap("status", env=env)
        self.assertEqual(
            max(
                json.loads(p.read_text())["cache_hits"]
                for p in worktree.glob("record-*.json")
            ),
            2,
        )

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_nested_consumer_does_not_mount_or_inherit_enclosing_repository(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Enclosing policy"],
            check=True,
        )
        global_config = self.root / "global-config"
        global_config.write_text("[user]\nname = Global policy\n")
        nested = self.root / "nested example"
        nested.mkdir()
        for path in (
            self.root / "scripts",
            self.root / "bundle.tar.gz",
            self.root / "chainman.lock",
        ):
            if path.is_dir():
                shutil.copytree(path, nested / path.name)
            else:
                shutil.copy2(path, nested / path.name)
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            CHAINMAN_PROJECT_ROOT=str(nested),
            CHAINMAN_FORWARD_ENV="DEMO_*",
            DEMO_TEST_ADMIN=str(self.root / ".git"),
            GIT_CONFIG_GLOBAL=str(global_config),
        )
        self.run_bootstrap("status", env=env)
        record = json.loads(next(nested.glob("record-*.json")).read_text())
        self.assertFalse(record["parent_admin_visible"])
        self.assertEqual(record["git_name"], "Global policy")


if __name__ == "__main__":
    unittest.main()
