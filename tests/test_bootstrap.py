"""Bootstrap qualification uses real Nix and neutral temporary runtime archives."""

import base64
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
            "record['nix_remote'] = os.environ.get('NIX_REMOTE')\n"
            "if record['container'] == '1': record['nix_store'] = subprocess.check_output(['nix', '--extra-experimental-features', 'nix-command', 'config', 'show', 'store'], text=True).strip()\n"
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
            "if '--hold-profile' in sys.argv:\n"
            " held = tempfile.TemporaryDirectory(prefix='chainman-held-profile-')\n"
            " profile = str(pathlib.Path(held.name) / 'profile')\n"
            " expression = 'derivation { name = \"chainman-held-profile\"; system = builtins.currentSystem; builder = ' + json.dumps(str(pathlib.Path(shutil.which('bash')).resolve())) + '; args = [ \"-c\" \"echo held > $out\" ]; identity = ' + json.dumps(str(root)) + '; }'\n"
            " output = subprocess.check_output(['nix', '--extra-experimental-features', 'nix-command', 'build', '--impure', '--expr', expression, '--out-link', profile, '--print-out-paths'], text=True).strip()\n"
            " (root / 'held.json').write_text(json.dumps(dict(profile=profile, output=output)))\n"
            "if '--check-held-profile' in sys.argv:\n"
            " held_record = json.loads((root / 'held.json').read_text())\n"
            " roots = subprocess.check_output(['nix-store', '--query', '--roots', held_record['output']], text=True)\n"
            " assert held_record['profile'] in roots, roots\n"
            " deletion = subprocess.run(['nix-store', '--delete', held_record['output']], capture_output=True, text=True)\n"
            " assert deletion.returncode != 0, deletion.stdout + deletion.stderr\n"
            " assert pathlib.Path(held_record['output']).read_text() == 'held\\n'\n"
            " record['held_profile_protected'] = True\n"
            "(root / ('record-' + str(os.getpid()) + '.json')).write_text(json.dumps(record))\n"
            "if '--hold-profile' in sys.argv:\n"
            " deadline = time.monotonic() + 120\n"
            " while not (root / 'release-profile').exists() and time.monotonic() < deadline: time.sleep(0.1)\n"
            " held.cleanup()\n"
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

    def authority_projection(self, *, container=False):
        self.use_real_runtime()
        subprocess.run(["git", "init", str(self.root)], check=True, capture_output=True)
        original = 'schema=3\n[project]\ndefault_profile="host"\n[tasks.probe]\ncommands=[["true"]]\n'
        (self.root / "chainman.toml").write_text(original)
        exported = tempfile.TemporaryDirectory(prefix="chainman entry authority ")
        self.addCleanup(exported.cleanup)
        authority = Path(exported.name)
        for source, name in (
            (SOURCE / "bootstrap/chainman.sh", "chainman.sh"),
            (SOURCE / "bootstrap/fetch.nix", "chainman-fetch.nix"),
            (self.root / "chainman.lock", "chainman.lock"),
            (self.root / "bundle.tar.gz", "bundle.tar.gz"),
        ):
            shutil.copy2(source, authority / name)
        (authority / "authority-root").write_text(str(self.root) + "\n")
        (authority / "git-directories").write_text(".git\nnested input/.git\n")
        subprocess.run(
            ["git", "init", str(self.root / "nested input")],
            check=True,
            capture_output=True,
        )
        (authority / "chainman.toml").write_text(original)
        # Neither a replacement pin nor new host mount authority may influence
        # the next launch after a resolver has edited the writable candidate.
        (self.root / "chainman.lock").write_text('{"schema":999}')
        (self.root / "chainman.toml").write_text(
            original
            + '\n[container]\nmounts=[{source="/candidate-chosen-source",target="/original-write-access",read_only=false}]\n'
        )
        env = dict(self.env, CHAINMAN_PROJECT_ROOT=str(self.root))
        if container:
            env.update(
                CHAINMAN_MODE="container-nix",
                CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            )
        result = subprocess.run(
            [str(authority / "chainman.sh"), "config", "show", "--json"],
            env=env,
            text=True,
            capture_output=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        cfg = json.loads(result.stdout)["configuration"]
        self.assertNotIn("container", cfg)
        self.assertEqual(cfg["tasks"]["probe"]["commands"], [["true"]])
        if container:
            probe = """import os, pathlib
for path in (pathlib.Path(os.environ['CHAINMAN_ENTRY_AUTHORITY']) / 'chainman.lock', pathlib.Path('.git/config'), pathlib.Path('nested input/.git/config')):
    try: path.write_text('must remain protected')
    except OSError: pass
    else: raise AssertionError(str(path) + ' was writable')
print('entry and Git authority are read-only')
"""
            checked = subprocess.run(
                [str(authority / "chainman.sh"), "exec", "--", "python3", "-c", probe],
                env=env,
                text=True,
                capture_output=True,
                timeout=180,
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("entry and Git authority are read-only", checked.stdout)

    def test_candidate_entry_uses_frozen_runtime_and_configuration(self):
        self.authority_projection()

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER"),
        "explicit real container qualification",
    )
    def test_container_candidate_entry_uses_frozen_runtime_and_configuration(self):
        self.authority_projection(container=True)

    def use_real_runtime(self):
        runtime = self.root / "real-runtime"
        inventory = json.loads((SOURCE / "release-files.json").read_text())[
            "runtime_files"
        ]
        for name in inventory:
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, target)
        self.bundle_runtime(runtime)
        return runtime

    def bundle_runtime(self, runtime):
        self.lock["narHash"] = subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "hash",
                "path",
                str(runtime),
            ],
            text=True,
        ).strip()
        with tarfile.open(self.root / "bundle.tar.gz", "w:gz") as archive:
            archive.add(runtime, arcname="runtime")
        self.write_lock()

    def lifecycle_git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], text=True, env=self.env
        ).strip()

    def update_lifecycle(self, *, reject=False, block=False):
        runtime = self.use_real_runtime()
        self.lock["version"] = (runtime / "VERSION").read_text().strip()
        self.write_lock()
        self.launcher.chmod(0o755)
        (self.root / "scripts/chainman-fetch.nix").chmod(0o644)
        # Reuse only the fixture's Nix/download cache between cases. Each test
        # owns its consumer and clears its transaction directory during cleanup.
        cache = Path(self.shared.name) / "lifecycle cache"
        self.env["XDG_CACHE_HOME"] = str(cache)
        self.update_cache = cache / "chainman/updates"
        self.addCleanup(shutil.rmtree, self.update_cache, ignore_errors=True)
        (self.root / ".gitignore").write_text(".cache/\nreal-runtime/\n")
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[updates]
profile="host"
outputs=["dependency.lock"]
targets=["assets"]
verify_task="verify"
[[updates.steps]]
targets=["assets"]
commands=[["python3", "workflow.py", "resolve"]]
[tasks.verify]
commands=[["python3", "workflow.py", "verify"]]
[tasks.format-write]
commands=[["python3", "workflow.py", "format-write"]]
[tasks.format-check]
commands=[["python3", "workflow.py", "format-check"]]
[recipes]
format-write=["format-write"]
format-check=["format-check"]
""")
        (self.root / "dependency.lock").write_text("old\n")
        for name in ("selected.txt", "excluded.txt", "partial.txt"):
            (self.root / name).write_text("original\n")
        (self.root / "workflow.py").write_text(
            "import os, pathlib, sys, time\n"
            "root = pathlib.Path.cwd()\n"
            # Every project command must execute in the isolated candidate.
            f"assert str(root) != {str(self.root)!r}\n"
            "action = sys.argv[1]\n"
            "if action == 'resolve':\n"
            f"    (root / 'dependency.lock').write_text({'rejected\n' if reject else 'accepted\n'!r})\n"
            "elif action == 'verify':\n"
            "    assert sys.stdin.read() == ''\n"
            f"    if {block!r} and not (root / '.cache/continue').exists():\n"
            "        (root / '.cache/verifier.pid').write_text(str(os.getpid()))\n"
            "        time.sleep(120)\n"
            "    assert (root / 'dependency.lock').read_text() == 'accepted\\n', 'fixture verification rejected'\n"
            "elif action == 'format-write':\n"
            "    for name in ('selected.txt', 'excluded.txt', 'partial.txt'):\n"
            "        path = root / name\n"
            "        path.write_text(path.read_text().strip() + '\\n')\n"
            "elif action == 'format-check':\n"
            "    assert (root / 'selected.txt').read_text() == 'selected\\n'\n"
            "    assert (root / 'excluded.txt').read_text() == 'excluded  \\n'\n"
            "    assert (root / 'partial.txt').read_text() == 'unstaged  \\n'\n"
            "print('fixture phase: ' + action, file=sys.stderr)\n"
        )
        subprocess.run(
            ["python3", str(runtime / "scripts/recipes.py"), str(self.root)],
            check=True,
            env=self.env,
            capture_output=True,
        )
        self.lifecycle_git("init", "-b", "main")
        self.lifecycle_git("config", "user.name", "Fixture")
        self.lifecycle_git("config", "user.email", "fixture@example.invalid")
        self.lifecycle_git("config", "commit.gpgsign", "false")
        self.lifecycle_git("config", "core.hooksPath", "/dev/null")
        self.lifecycle_git("add", ".")
        self.lifecycle_git("commit", "-m", "Initial fixture")
        return self.lifecycle_git("rev-parse", "HEAD")

    def test_public_update_lifecycle_commits_and_cleans_up(self):
        before = self.update_lifecycle()
        result = self.run_bootstrap("deps-update", "--json")
        accepted = json.loads(result.stdout)
        self.assertEqual(accepted["changed"], ["dependency.lock"])
        self.assertEqual(accepted["verification"], "passed")
        self.assertEqual(accepted["commit"], self.lifecycle_git("rev-parse", "HEAD"))
        self.assertEqual(self.lifecycle_git("rev-parse", "HEAD^"), before)
        self.assertEqual(self.lifecycle_git("status", "--porcelain"), "")
        self.assertEqual((self.root / "dependency.lock").read_text(), "accepted\n")
        self.assertIn("fixture phase: resolve", result.stderr)
        self.assertIn("fixture phase: verify", result.stderr)
        self.assertEqual(list(self.update_cache.iterdir()), [])
        unchanged = json.loads(self.run_bootstrap("deps-update", "--json").stdout)
        self.assertEqual(unchanged["verification"], "no changes")
        self.assertIsNone(unchanged["commit"])
        self.assertEqual(list(self.update_cache.iterdir()), [])

    def test_public_update_lifecycle_preserves_failure_and_reverifies_resume(self):
        before = self.update_lifecycle(reject=True)
        rejected = self.run_bootstrap("deps-update", "--json", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("fixture verification rejected", rejected.stderr)
        self.assertEqual(self.lifecycle_git("rev-parse", "HEAD"), before)
        self.assertEqual(self.lifecycle_git("status", "--porcelain"), "")
        self.assertEqual((self.root / "dependency.lock").read_text(), "old\n")
        candidates = list(self.update_cache.glob("candidate.*"))
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0] / "candidate"
        self.assertEqual((candidate / "dependency.lock").read_text(), "rejected\n")
        (candidate / "dependency.lock").write_text("accepted\n")
        resumed = self.run_bootstrap("deps-update", f"resume={candidates[0]}")
        self.assertEqual(json.loads(resumed.stdout)["verification"], "passed")
        self.assertIn("fixture phase: verify", resumed.stderr)
        self.assertNotIn("fixture phase: resolve", resumed.stderr)
        self.assertEqual(self.lifecycle_git("rev-parse", "HEAD^"), before)
        self.assertEqual(self.lifecycle_git("status", "--porcelain"), "")
        self.assertEqual((self.root / "dependency.lock").read_text(), "accepted\n")
        self.assertEqual(list(self.update_cache.iterdir()), [])

    def test_public_update_lifecycle_interruption_can_resume(self):
        before = self.update_lifecycle(block=True)

        def running(pid):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            if Path("/proc").is_dir():
                try:
                    return (
                        Path(f"/proc/{pid}/stat")
                        .read_text()
                        .rpartition(") ")[2]
                        .split()[0]
                        != "Z"
                    )
                except FileNotFoundError:
                    return False
            return True

        with tempfile.TemporaryFile(mode="w+") as output:
            command = subprocess.Popen(
                [str(self.launcher), "deps-update", "--json"],
                cwd="/",
                env=self.env,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            pid = None
            try:
                deadline = time.monotonic() + 120
                markers = []
                while command.poll() is None and time.monotonic() < deadline:
                    markers = list(
                        self.update_cache.glob(
                            "candidate.*/candidate/.cache/verifier.pid"
                        )
                    )
                    if markers:
                        break
                    time.sleep(0.05)
                if not markers:
                    output.seek(0)
                    self.fail("Verifier did not start: " + output.read())
                pid = int(markers[0].read_text())
                transaction = markers[0].parents[2]
                os.killpg(command.pid, signal.SIGTERM)
                command.wait(timeout=15)
                self.assertNotEqual(command.returncode, 0)
                self.assertEqual(self.lifecycle_git("rev-parse", "HEAD"), before)
                self.assertEqual(self.lifecycle_git("status", "--porcelain"), "")
                self.assertEqual((self.root / "dependency.lock").read_text(), "old\n")
                deadline = time.monotonic() + 5
                while running(pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(running(pid), "Interrupted verifier is still running")
                (markers[0].parent / "continue").touch()
                result = self.run_bootstrap("deps-update", f"resume={transaction}")
                self.assertEqual(json.loads(result.stdout)["verification"], "passed")
                self.assertEqual(self.lifecycle_git("rev-parse", "HEAD^"), before)
                self.assertEqual(list(self.update_cache.iterdir()), [])
            finally:
                if command.poll() is None:
                    os.killpg(command.pid, signal.SIGKILL)
                    command.wait(timeout=15)
                if pid is not None and running(pid):
                    os.kill(pid, signal.SIGKILL)

    def test_public_update_lifecycle_staged_format_preserves_other_bytes_and_index(
        self,
    ):
        before = self.update_lifecycle()
        (self.root / "selected.txt").write_text("selected  \n")
        (self.root / "partial.txt").write_text("staged  \n")
        self.lifecycle_git("add", "selected.txt", "partial.txt")
        (self.root / "partial.txt").write_text("unstaged  \n")
        (self.root / "excluded.txt").write_text("excluded  \n")
        partial = self.lifecycle_git("ls-files", "--stage", "partial.txt")
        result = self.run_bootstrap("format", "--staged", "--json")
        accepted = json.loads(result.stdout)
        self.assertEqual(accepted["changed"], ["selected.txt"])
        self.assertIsNone(accepted["commit"])
        self.assertEqual(self.lifecycle_git("rev-parse", "HEAD"), before)
        self.assertEqual((self.root / "selected.txt").read_text(), "selected\n")
        self.assertEqual(self.lifecycle_git("show", ":selected.txt"), "selected")
        self.assertEqual((self.root / "excluded.txt").read_text(), "excluded  \n")
        self.assertEqual((self.root / "partial.txt").read_text(), "unstaged  \n")
        self.assertEqual(
            self.lifecycle_git("ls-files", "--stage", "partial.txt"), partial
        )
        self.assertIn("fixture phase: format-check", result.stderr)
        self.assertEqual(list(self.update_cache.iterdir()), [])

    def test_public_runtime_update_lifecycle_switches_only_after_verification(self):
        self.update_lifecycle()
        temporary = tempfile.TemporaryDirectory(prefix="chainman release fixture ")
        self.addCleanup(temporary.cleanup)
        previous = self.root / "real-runtime"
        next_runtime = Path(temporary.name) / "next-runtime"
        shutil.copytree(previous, next_runtime)
        (next_runtime / "VERSION").write_text("0.2.0\n")
        with (next_runtime / "bootstrap/chainman.sh").open("a") as stream:
            stream.write("\n# Neutral second-generation bootstrap fixture.\n")
        self.bundle_runtime(next_runtime)
        body = (self.root / "bundle.tar.gz").read_bytes()
        revision = "b" * 40
        metadata = dict(
            schema=1,
            version="0.2.0",
            revision=revision,
            url="https://example.invalid/chainman-0.2.0.tar.gz",
            narHash=self.lock["narHash"],
            archive_sha256=hashlib.sha256(body).hexdigest(),
        )
        published = "2026-01-01T00:00:00Z"
        release = dict(
            tag_name="v0.2.0",
            draft=False,
            prerelease=False,
            published_at=published,
            assets=[],
        )
        api = "https://api.github.com/repos/chainmandev/chainman"
        responses = {}
        for number, name, data in (
            (1, "chainman-release.json", json.dumps(metadata).encode()),
            (2, "chainman-0.2.0.tar.gz", body),
        ):
            release["assets"].append(
                dict(
                    id=number,
                    name=name,
                    state="uploaded",
                    size=len(data),
                    digest="sha256:" + hashlib.sha256(data).hexdigest(),
                    created_at=published,
                    updated_at=published,
                )
            )
            responses[f"{api}/releases/assets/{number}"] = data
        for url, value in {
            f"{api}/releases?per_page=100&page=1": [release],
            f"{api}/releases/tags/v0.2.0": release,
            f"{api}/git/ref/tags/v0.2.0": {
                "object": {"type": "commit", "sha": revision}
            },
            f"{api}/commits/{revision}": {
                "sha": revision,
                "commit": {"committer": {"date": published}},
            },
        }.items():
            responses[url] = json.dumps(value).encode()
        # Replace only the remote transport in the old immutable fixture runtime.
        # Selection, asset/date/hash checks, Nix unpacking, public orchestration,
        # candidate verification and application use the actual implementations.
        encoded = {
            url: base64.b64encode(data).decode() for url, data in responses.items()
        }
        with (previous / "scripts/registry.py").open("a") as stream:
            stream.write(
                f"\n_fixture_responses = {encoded!r}\n"
                "def _fetch(url, *args, **kwargs):\n"
                "    return base64.b64decode(_fixture_responses[url]), {}\n"
            )
        self.bundle_runtime(previous)
        workflow = self.root / "workflow.py"
        workflow.write_text(
            workflow.read_text().replace(
                "assert (root / 'dependency.lock').read_text() == 'accepted\\n', 'fixture verification rejected'",
                "assert (pathlib.Path(os.environ['CHAINMAN_RUNTIME']) / 'VERSION').read_text().strip() == '0.2.0'\n"
                f"    assert __import__('json').loads(pathlib.Path({str(self.root / 'chainman.lock')!r}).read_text())['version'] == '0.1.0'",
            )
        )
        self.lifecycle_git("add", ".")
        self.lifecycle_git(
            "commit", "-m", "Declare immutable release transport fixture"
        )
        before = self.lifecycle_git("rev-parse", "HEAD")
        probe = "import os; print(os.environ['CHAINMAN_RUNTIME'])"
        old_path = Path(
            self.run_bootstrap(
                "exec", "--profile", "host", "--", "python3", "-c", probe
            ).stdout.strip()
        )
        result = self.run_bootstrap("chainman-update", "--json")
        accepted = json.loads(result.stdout)
        self.assertEqual(
            set(accepted["changed"]),
            {"chainman.lock", "bundle.tar.gz", "scripts/chainman.sh"},
        )
        self.assertEqual(accepted["commit"], self.lifecycle_git("rev-parse", "HEAD"))
        self.assertEqual(self.lifecycle_git("rev-parse", "HEAD^"), before)
        self.assertEqual(self.lifecycle_git("status", "--porcelain"), "")
        self.assertEqual(
            json.loads((self.root / "chainman.lock").read_text())["version"], "0.2.0"
        )
        new_path = Path(
            self.run_bootstrap(
                "exec", "--profile", "host", "--", "python3", "-c", probe
            ).stdout.strip()
        )
        self.assertNotEqual(old_path, new_path)
        self.assertEqual((old_path / "VERSION").read_text(), "0.1.0\n")
        self.assertEqual((new_path / "VERSION").read_text(), "0.2.0\n")
        self.assertIn("fixture phase: verify", result.stderr)
        self.assertEqual(list(self.update_cache.iterdir()), [])

    def schema_three_recovery(self, mode):
        self.use_real_runtime()
        config = self.root / "chainman.toml"
        config.write_text(
            'schema=3\n[project]\ndefault_profile="host"\n[tasks.probe]\nservices=["worker"]\ncommands=[["true"]]\n[services.worker]\ncommand=["sleep","600"]\nshutdown_seconds=2\n'
        )
        cache = tempfile.TemporaryDirectory(prefix="chainman recovery host state ")
        self.addCleanup(cache.cleanup)
        env = dict(self.env, CHAINMAN_MODE=mode, XDG_CACHE_HOME=cache.name)
        if mode == "container-nix":
            env["CHAINMAN_CONTAINER_ENGINE"] = os.environ["CHAINMAN_TEST_CONTAINER"]
        try:
            self.run_bootstrap("services-up", "probe", env=env)
            with config.open("a") as stream:
                stream.write('\n[templates.tasks.invalid]\nextends="missing"\n')
            self.assertNotEqual(
                self.run_bootstrap(
                    "config", "validate", env=env, check=False
                ).returncode,
                0,
            )
            status = json.loads(self.run_bootstrap("services-status", env=env).stdout)
            self.assertTrue(status["running"])
            self.run_bootstrap("services-stop", env=env)
            status = json.loads(self.run_bootstrap("services-status", env=env).stdout)
            self.assertFalse(status["running"])
        finally:
            self.run_bootstrap("services-stop", env=env)

    def test_schema_three_host_recovery_ignores_broken_templates(self):
        self.schema_three_recovery("host-nix")

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "requires real container engine",
    )
    def test_schema_three_container_recovery_ignores_broken_templates(self):
        self.schema_three_recovery("container-nix")

    def test_schema_three_actual_host_compilation_and_command(self):
        self.use_real_runtime()
        (self.root / "chainman.toml").write_text(
            'schema=3\n[project]\ndefault_profile="host"\n[templates.tasks.base]\ncommands=[["printf", "composed-command"]]\n[tasks.probe]\nextends="base"\n'
        )
        result = self.run_bootstrap("config", "validate")
        self.assertEqual(
            json.loads(result.stdout),
            {"schema": 1, "configuration_schema": 3, "valid": True},
        )
        self.assertEqual(self.run_bootstrap("run", "probe").stdout, "composed-command")

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_schema_three_inherited_mount_uses_verified_compiler(self):
        self.use_real_runtime()
        outside = tempfile.TemporaryDirectory(prefix="chainman composed mount ")
        self.addCleanup(outside.cleanup)
        sdk = Path(outside.name) / "literal $(never-executed) SDK"
        sdk.write_text("composed SDK")
        program = "import os,pathlib; p=pathlib.Path(os.environ['DEMO_SDK_FILE']); print(p.read_text());\ntry: p.write_text('wrong')\nexcept OSError as e: assert e.errno == 30\nelse: raise AssertionError('mount was writable')"
        (self.root / "chainman.toml").write_text(
            'schema=3\n[project]\ndefault_profile="host"\n[environment]\npass=["DEMO_SDK_FILE"]\n[templates.tasks.base]\ncommands=[["python3", "-c", '
            + json.dumps(program)
            + ']]\n[templates.tasks.base.transport]\nmounts=[{source_env="DEMO_SDK_FILE"}]\n[tasks.probe]\nextends="base"\n'
        )
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
            DEMO_SDK_FILE=str(sdk),
        )
        result = self.run_bootstrap("run", "probe", env=env)
        self.assertEqual(result.stdout.strip(), "composed SDK")
        self.assertEqual(sdk.read_text(), "composed SDK")
        missing = dict(env)
        missing.pop("DEMO_SDK_FILE")
        result = self.run_bootstrap("run", "probe", env=missing, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unset", result.stderr)

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
        self.assertEqual(record["nix_config"], "build-users-group =\nstore = daemon")
        self.assertEqual(record["nix_store"], "daemon")
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
        self.assertEqual(record["nix_remote"], "daemon")
        self.assertEqual(record["nix_store"], "daemon")
        self.assertTrue(record["tmpdir"].startswith("/nix/tmp"))
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
        root_inode = root.lstat().st_ino
        self.run_bootstrap("second", env=env)
        self.assertEqual(root.lstat().st_ino, root_inode)
        self.assertEqual(
            {record["runtime"] for record in self.records()}, {str(runtime)}
        )

    def test_runtime_root_left_unregistered_is_repaired(self):
        self.run_bootstrap("first")
        runtime = Path(self.records()[0]["runtime"])
        cache = self.root / "private-cache"
        root = (
            cache
            / "chainman/runtime-roots"
            / hashlib.sha256(self.lock["narHash"].encode()).hexdigest()
        )
        root.parent.mkdir(parents=True)
        root.symlink_to(runtime)
        nix_store = str(Path(NIX).resolve().with_name("nix-store"))
        self.assertNotIn(
            str(root),
            subprocess.check_output(
                [nix_store, "--query", "--roots", str(runtime)], text=True
            ),
        )
        self.run_bootstrap("repair", env=dict(self.env, XDG_CACHE_HOME=str(cache)))
        self.assertIn(
            str(root),
            subprocess.check_output(
                [nix_store, "--query", "--roots", str(runtime)], text=True
            ),
        )

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_container_concurrent_cold_and_warm_runtime_roots(self):
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
        consumers = []
        for index in range(6):
            consumer = self.root / f"parallel-{index}"
            consumer.mkdir()
            shutil.copytree(self.root / "scripts", consumer / "scripts")
            for name in ("chainman.lock", "bundle.tar.gz"):
                shutil.copy2(self.root / name, consumer / name)
            consumers.append(consumer)
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
        )
        for phase in ("cold", "warm"):
            with self.subTest(phase=phase):
                processes = [
                    subprocess.Popen(
                        [str(consumer / "scripts/chainman.sh"), phase],
                        cwd="/",
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    for consumer in consumers
                ]
                try:
                    for process in processes:
                        stdout, stderr = process.communicate(timeout=180)
                        self.assertEqual(process.returncode, 0, stdout + stderr)
                finally:
                    for process in processes:
                        if process.poll() is None:
                            process.terminate()
                            process.communicate(timeout=30)
                for consumer in consumers:
                    records = [
                        json.loads(path.read_text())
                        for path in consumer.glob("record-*.json")
                    ]
                    self.assertTrue(
                        any(record["argv"][-1] == phase for record in records)
                    )

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") in ("docker", "podman"),
        "set CHAINMAN_TEST_CONTAINER to execute the real container engine",
    )
    def test_container_daemon_preserves_another_clients_temporary_profile(self):
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE=os.environ["CHAINMAN_TEST_CONTAINER"],
        )
        process = subprocess.Popen(
            [str(self.launcher), "--hold-profile"],
            cwd="/",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 120
            while (
                not (self.root / "held.json").exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
            self.assertTrue(
                (self.root / "held.json").exists(),
                "first client's profile did not become ready",
            )
            self.run_bootstrap("--check-held-profile", env=env)
            self.assertTrue(
                any(record.get("held_profile_protected") for record in self.records())
            )
        finally:
            (self.root / "release-profile").touch()
            stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, stdout + stderr)

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_CONTAINER") == "docker",
        "requires Docker for the isolated daemon migration fixture",
    )
    def test_daemon_migration_refuses_live_legacy_clients_and_restarts_owned_daemon(
        self,
    ):
        volume = (
            "chainman-daemon-fixture-"
            + hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        )
        daemon = volume + "-daemon"
        legacy = volume + "-legacy"
        image = (SOURCE / "nix/container-image.txt").read_text().strip()
        env = dict(
            self.env,
            CHAINMAN_MODE="container-nix",
            CHAINMAN_CONTAINER_ENGINE="docker",
            CHAINMAN_NIX_VOLUME=volume,
        )

        def engine(*arguments):
            return subprocess.run(
                ["docker", *arguments], check=True, capture_output=True, text=True
            ).stdout.strip()

        try:
            engine(
                "run",
                "--detach",
                "--name",
                legacy,
                "--mount",
                f"type=volume,src={volume},dst=/nix",
                image,
                "sleep",
                "300",
            )
            result = self.run_bootstrap("status", check=False, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Stop existing containers", result.stderr)
            self.assertEqual(
                engine("inspect", "--format", "{{.State.Running}}", legacy), "true"
            )
            engine("rm", "--force", legacy)
            self.run_bootstrap("first", env=env)
            identity = engine("inspect", "--format", "{{.Id}}", daemon)
            engine("stop", daemon)
            self.run_bootstrap("restart", env=env)
            self.assertEqual(engine("inspect", "--format", "{{.Id}}", daemon), identity)
            self.assertEqual(
                engine("inspect", "--format", "{{.State.Running}}", daemon), "true"
            )
            engine("rm", "--force", daemon)
            engine("create", "--name", daemon, image, "sleep", "300")
            result = self.run_bootstrap("status", check=False, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("incompatible identity or isolation", result.stderr)
            self.assertEqual(
                engine("inspect", "--format", "{{.State.Status}}", daemon), "created"
            )
        finally:
            subprocess.run(
                ["docker", "rm", "--force", "--volumes", legacy, daemon],
                capture_output=True,
            )
            subprocess.run(
                ["docker", "volume", "rm", volume, volume + "-downloads"],
                capture_output=True,
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
