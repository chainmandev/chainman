"""Bootstrap qualification uses real Nix and neutral temporary runtime archives."""

import hashlib
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
            "import json, os, pathlib, shutil, socket, subprocess, sys, tempfile, time\n"
            "root = pathlib.Path(sys.argv[2])\n"
            "record = dict(argv=sys.argv[1:], runtime=os.environ['CHAINMAN_RUNTIME'], "
            "root=os.environ['CHAINMAN_ROOT'], cwd=os.getcwd(), "
            "forward=os.environ.get('CHAINMAN_TEST_VALUE'), "
            "demo=os.environ.get('DEMO_TEST_VALUE'), container=os.environ.get('TOOLCHAIN_CONTAINER'))\n"
            "record.update(nix=shutil.which('nix'), uid=os.getuid(), nix_config=os.environ.get('NIX_CONFIG'), tmpdir=os.environ.get('TMPDIR'))\n"
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
            "if '--inspect-sdk' in sys.argv:\n"
            " sdk = pathlib.Path(os.environ['DEMO_SDK_FILE'])\n"
            " record['sdk_data'] = sdk.read_text()\n"
            " try: sdk.write_text('unexpected mutation')\n"
            " except OSError as error: record['sdk_readonly'] = error.errno == 30\n"
            " else: record['sdk_readonly'] = False\n"
            "if os.environ.get('DEMO_TEST_CACHE'):\n"
            " cache = pathlib.Path(os.environ['TOOLCHAIN_DOWNLOAD_CACHE']) / os.environ['DEMO_TEST_CACHE']\n"
            " record['cache_hits'] = int(cache.read_text()) + 1 if cache.exists() else 1\n"
            " cache.write_text(str(record['cache_hits']))\n"
            " (pathlib.Path.home() / 'home-marker').write_text('persistent')\n"
            "(root / ('record-' + str(os.getpid()) + '.json')).write_text(json.dumps(record))\n"
            "if '--wait' in sys.argv: time.sleep(60)\n"
            "if '--resolve-service' in sys.argv: print(socket.gethostbyname(os.environ['DEMO_SERVICE_ALIAS']))\n"
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

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_owned_private_bridge_resolves_service_alias_without_host_exposure(self):
        engine = os.environ["CHAINMAN_TEST_CONTAINER"]
        key = hashlib.sha256(str(self.root).encode()).hexdigest()[:24]
        network, alias = "chainman-" + key, "cm-" + key
        network_id = subprocess.check_output(
            [engine, "network", "create", "--driver", "bridge", network], text=True
        ).strip()
        self.addCleanup(
            lambda: subprocess.run(
                [engine, "network", "rm", network_id], capture_output=True, check=True
            )
        )
        image = (SOURCE / "nix/container-image.txt").read_text().strip()
        container_id = subprocess.check_output(
            [
                engine,
                "run",
                "--detach",
                "--rm",
                "--network",
                network,
                "--network-alias",
                alias,
                "--user",
                "1000:1000",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                image,
                "sleep",
                "120",
            ],
            text=True,
        ).strip()
        self.addCleanup(
            lambda: subprocess.run(
                [engine, "stop", "--time", "1", container_id],
                capture_output=True,
                check=True,
            )
        )
        expected = json.loads(
            subprocess.check_output([engine, "inspect", container_id], text=True)
        )[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=engine,
            CHAINMAN_CONTAINER_BRIDGE=network,
            CHAINMAN_FORWARD_ENV="DEMO_SERVICE_ALIAS",
            DEMO_SERVICE_ALIAS=alias,
        )
        result = self.run_bootstrap("--resolve-service", env=env)
        self.assertEqual(result.stdout.strip(), expected)
        self.assertEqual(self.records()[0]["cap_eff"], "0000000000000000")
        self.assertEqual(self.records()[0]["no_new_privs"], "1")

    def test_script_transport_preserves_code_and_literal_arguments(self):
        script = self.root / "recipe with spaces"
        body = "cat <<'EOF'\n$literal `data`\nEOF\n\n"
        script.write_text(body)
        argument = "a 'quote' $(not-a-command); *"
        self.run_bootstrap("script", "--profile", "host", str(script), argument)
        self.assertEqual(
            self.records()[0]["argv"][2:],
            [
                "exec",
                "--profile",
                "host",
                "--",
                "bash",
                "--noprofile",
                "--norc",
                "-eu",
                "-o",
                "pipefail",
                "-c",
                body,
                str(script),
                argument,
            ],
        )

    def test_script_transport_rejects_invalid_inputs_before_nix(self):
        script = self.root / "recipe"
        script.write_text("true\n")
        link = self.root / "linked recipe"
        link.symlink_to(script)
        for arguments in (
            (),
            ("--profile",),
            ("--profile", "", str(script)),
            (str(self.root / "missing"),),
            (str(self.root),),
            (str(link),),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap("script", *arguments, check=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn("script", result.stderr)
        self.assertEqual(self.records(), [])

    def test_explicit_temporary_base_survives_bootstrap_and_runtime_entry(self):
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
        for path in self.root.glob("record-*.json"):
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(path.stat().st_gid, os.getgid())

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") == "docker",
        "requires Docker for the capability-free UID-0 image regression",
    )
    def test_stock_image_builds_read_only_owned_outputs_without_capabilities(self):
        image = (SOURCE / "nix/container-image.txt").read_text().strip()
        command = """chmod 0555 /
mkdir -p "$HOME"
SHELL=$(readlink -f "$(command -v sh)")
export SHELL
nix --extra-experimental-features nix-command build --no-link --impure --print-out-paths --expr '
  derivation {
    name = "chainman-stock-nix-owned-output";
    system = builtins.currentSystem;
    builder = builtins.getEnv "SHELL";
    PATH = builtins.getEnv "PATH";
    args = [ "-eu" "-c" "mkdir -p $out/owned; echo fixture > $out/owned/value; chmod 0555 $out/owned $out" ];
  }'
"""
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--env",
                "NIX_CONFIG=build-users-group =",
                "--env",
                "HOME=/tmp/chainman-home",
                image,
                "sh",
                "-eu",
                "-c",
                command,
            ],
            text=True,
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip().startswith("/nix/store/"))

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
        self.assertEqual(runtime.parent, Path("/nix/store"))
        self.assertFalse((self.root / ".chainman").exists())
        self.assertEqual(Path(first["nix"]).resolve(), Path(NIX).resolve())
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

    def test_project_local_runtime_shadow_is_never_executed(self):
        content_id = hashlib.sha256(self.nar_hash.encode()).hexdigest()
        shadow = self.root / ".chainman" / content_id / "scripts"
        shadow.mkdir(parents=True)
        (shadow / "chainman.py").write_text("raise SystemExit(97)\n")
        self.run_bootstrap("status")
        self.assertEqual(Path(self.records()[0]["runtime"]).parent, Path("/nix/store"))

    def test_selected_nix_compatibility_failure_does_not_dispatch(self):
        fake = self.root / "unsupported nix"
        fake.write_text("#!/bin/sh\nexit 42\n")
        fake.chmod(0o755)
        result = self.run_bootstrap(
            check=False, env=dict(self.env, CHAINMAN_NIX_BIN=str(fake))
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Nix compatibility check failed", result.stderr)
        self.assertFalse(self.records())

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

    def test_runtime_source_has_registered_gc_root_and_resists_collection(self):
        candidate = self.root / "unique-runtime"
        shutil.copytree(self.tree, candidate)
        (candidate / "unique-source").write_text(str(self.root))
        self.lock["narHash"] = subprocess.check_output(
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
        with tarfile.open(self.root / "bundle.tar.gz", "w:gz") as archive:
            archive.add(candidate, arcname="runtime")
        self.write_lock()
        cache = self.root / "private-cache"
        env = dict(self.env, XDG_CACHE_HOME=str(cache))
        self.run_bootstrap("first", env=env)
        runtime = Path(self.records()[0]["runtime"])
        root = (
            cache
            / "chainman/runtime-roots"
            / hashlib.sha256(self.lock["narHash"].encode()).hexdigest()
        )
        self.assertEqual(root.resolve(), runtime)
        nix_store = str(Path(NIX).resolve().with_name("nix-store"))
        roots = subprocess.check_output(
            [nix_store, "--query", "--roots", str(runtime)], text=True
        )
        self.assertIn(str(root), roots)
        # Only this unique neutral fixture is addressed, never a global GC.
        result = subprocess.run(
            [nix_store, "--delete", str(runtime)], capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((runtime / "scripts/chainman.py").is_file())
        self.run_bootstrap("second", env=env)
        self.assertEqual(
            {record["runtime"] for record in self.records()}, {str(runtime)}
        )

    def test_runtime_gc_cache_rejects_directory_links_and_regular_root_files(self):
        cache = self.root / "private-cache"
        cache.mkdir()
        outside = self.root / "outside-cache"
        outside.mkdir()
        (cache / "chainman").symlink_to(outside, target_is_directory=True)
        env = dict(self.env, XDG_CACHE_HOME=str(cache))
        self.assertIn(
            "must not contain symlinks", self.run_bootstrap(check=False, env=env).stderr
        )
        self.assertEqual(list(outside.iterdir()), [])
        (cache / "chainman").unlink()
        roots = cache / "chainman/runtime-roots"
        roots.mkdir(parents=True)
        root = roots / hashlib.sha256(self.lock["narHash"].encode()).hexdigest()
        root.write_text("preserve this ordinary file")
        self.assertIn(
            "must be a symlink", self.run_bootstrap(check=False, env=env).stderr
        )
        self.assertEqual(root.read_text(), "preserve this ordinary file")

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
        # A global Nix profile may also contain unrelated host language tools.
        (tools / "nix").symlink_to(NIX)
        read_fd, write_fd = os.pipe()
        try:
            env = dict(
                self.env,
                PATH=str(tools) + os.pathsep + self.env["PATH"],
                CHAINMAN_NIX_BIN=str(tools / "nix"),
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
    def test_real_container_environment_mount_is_literal_readonly_and_required(self):
        outside = tempfile.TemporaryDirectory(prefix="chainman explicit SDK ")
        self.addCleanup(outside.cleanup)
        sdk = Path(outside.name) / "literal $(never-executed) SDK"
        sdk.write_text("explicit SDK fixture")
        (self.root / "chainman.toml").write_text(
            'schema=2\n[environment]\npass=["DEMO_SDK_FILE"]\n[tasks.probe]\ncommands=[["true"]]\n[tasks.probe.transport]\nmounts=[{source_env="DEMO_SDK_FILE"}]\n'
        )
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            DEMO_SDK_FILE=str(sdk),
        )
        self.run_bootstrap("run", "probe", "--inspect-sdk", env=env)
        self.assertEqual(self.records()[0]["sdk_data"], "explicit SDK fixture")
        self.assertTrue(self.records()[0]["sdk_readonly"])
        self.assertEqual(sdk.read_text(), "explicit SDK fixture")
        for value, message in (
            (None, "unset"),
            ("", "empty"),
            ("/", "Blanket host"),
            (str(sdk) + "\n", "Newlines"),
        ):
            with self.subTest(value=value):
                selected = dict(env)
                if value is None:
                    selected.pop("DEMO_SDK_FILE")
                else:
                    selected["DEMO_SDK_FILE"] = value
                result = self.run_bootstrap("run", "probe", check=False, env=selected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.assertEqual(len(self.records()), 1)

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
