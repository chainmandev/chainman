"""Verified internal service entry uses real children and held Nix closures."""

import json
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
import storage
import toolchain as tc
from internal_service_fixture import immutable_runtime

SOURCE = Path(__file__).resolve().parents[1]
NIX = shutil.which("nix")


@unittest.skipUnless(NIX, "real managed Nix runtime is required")
class InternalServiceEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.runtime, cls.bootstrap_python = immutable_runtime(
            cls.addClassCleanup
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="case-", dir=self.base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pin = "a" * 40
        (self.root / "chainman.lock").write_text(self.pin + "\n")
        # This probe validates activation and real lease imports, rather than
        # measuring the application deadline covered by the timeout fixtures.
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[profiles.tools]
runtime_profile="bootstrap"
[services.worker]
profile="tools"
command=["python3","payload.py","service"]
readiness={command=["python3","payload.py","probe"],timeout_seconds=10}
shutdown_command=["python3","payload.py","shutdown"]
""")
        (self.root / "payload.py").write_text("""import json,os,sys
from pathlib import Path
sys.path.insert(0,os.environ['CHAINMAN_RUNTIME']+'/scripts')
import storage,toolchain as tc
paths,fds=storage.inherited()
assert paths and fds
assert tc.inherited_operation()[0] is not None
assert os.environ['CHAINMAN_ACTIVE_PROFILE']=='tools'
Path(sys.argv[1]+'.json').write_text(json.dumps({'profile':os.environ['CHAINMAN_ACTIVE_PROFILE'],'runtime':os.environ['CHAINMAN_RUNTIME'],'leased':bool(fds)}))
""")
        self.env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        self.env.update(
            CHAINMAN_MODE="host-nix",
            CHAINMAN_SETUP="auto",
            CHAINMAN_ROOT=str(self.root),
            CHAINMAN_PROJECT_ROOT=str(self.root),
            CHAINMAN_RUNTIME=str(self.runtime),
            CHAINMAN_RUNTIME_PYTHON=self.bootstrap_python,
            CHAINMAN_RUNTIME_NIX_BIN=str(Path(NIX).resolve().parent),
            CHAINMAN_RUNTIME_STORAGE_ACTIVE=self.pin,
            CHAINMAN_ACTIVE_MODE="host-nix",
            CHAINMAN_ACTIVE_PIN=self.pin,
            CHAINMAN_INTERNAL_ENTRY="1",
            XDG_CACHE_HOME=str(self.root / "cache"),
        )
        context = patch.dict(os.environ, self.env, clear=True)
        context.start()
        self.addCleanup(context.stop)
        pool = (
            Path(self.env["XDG_CACHE_HOME"]) / "chainman/runtime-roots" / storage.POOL
        )
        self.holder = storage.use(pool, self.pin, "runtime")
        entry = self.holder.__enter__()
        self.addCleanup(self.holder.__exit__, None, None, None)
        (entry / "source").symlink_to(self.runtime)
        (entry / "bootstrap").symlink_to((self.base / "bootstrap").resolve())
        self.env = dict(os.environ)
        self.fingerprint = self.fingerprints()["launch"]

    def fingerprints(self):
        code = (
            f"import sys;sys.path.insert(0,{str(self.runtime / 'scripts')!r});"
            "import json,services,toolchain as tc,workflows;from pathlib import Path;"
            f"root=Path({str(self.root)!r});"
            "cfg=workflows.configuration(root);env=tc.environment(root,create=False);"
            "print(json.dumps({'launch':services.config_fingerprint(root,cfg,env=env),"
            "'lifecycle':services.config_fingerprint(root,cfg,env=env,include_setup_inputs=False)}))"
        )
        return json.loads(
            tc.managed_run(
                [sys.executable, "-I", "-B", "-c", code],
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
                timeout=15,
            ).stdout
        )

    def child(
        self, action="_workflow-service", *, env=None, name="worker", fingerprint=None
    ):
        return tc.managed_run(
            [
                str(self.runtime / "bootstrap/chainman.sh"),
                action,
                name,
                self.fingerprint if fingerprint is None else fingerprint,
            ],
            env=self.env if env is None else env,
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )

    def test_service_probe_and_shutdown_activate_declared_profile_with_real_leases(
        self,
    ):
        self.assertNotIn("CHAINMAN_ACTIVE_PROFILE", self.env)
        for action, record in (
            ("_workflow-service", "service"),
            ("_workflow-probe", "probe"),
            ("_workflow-shutdown", "shutdown"),
        ):
            with self.subTest(action=action):
                result = self.child(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                proof = json.loads((self.root / (record + ".json")).read_text())
                self.assertEqual(
                    proof,
                    {"profile": "tools", "runtime": str(self.runtime), "leased": True},
                )

    def recording_nix(self, *, reject_hashes=False):
        selected = self.root / "selected-nix"
        selected.mkdir()
        hashes = self.root / "runtime-hashes"
        queries = self.root / "bootstrap-queries"
        real_nix = Path(NIX).resolve()
        for name, body in (
            (
                "nix",
                'if [ "${3:-}" = hash ] && [ "${4:-}" = path ]; then\n'
                + '    printf "%s\\n" "$5" >> '
                + shlex.quote(str(hashes))
                + "\n"
                + ("    exit 91\n" if reject_hashes else "")
                + "fi\nexec "
                + shlex.quote(str(real_nix))
                + ' "$@"\n',
            ),
            (
                "nix-store",
                'if [ "${1:-}" = --query ] && [ "${2:-}" = --requisites ]; then\n'
                + '    printf "%s\\n" "$3" >> '
                + shlex.quote(str(queries))
                + "\nfi\nexec "
                + shlex.quote(str(real_nix.with_name("nix-store")))
                + ' "$@"\n',
            ),
        ):
            executable = selected / name
            executable.write_text("#!/bin/sh\nset -eu\n" + body)
            executable.chmod(0o700)
        return dict(self.env, CHAINMAN_RUNTIME_NIX_BIN=str(selected)), hashes, queries

    def test_identical_immutable_runtime_skips_only_redundant_hashes(self):
        env, hashes, queries = self.recording_nix(reject_hashes=True)
        for action, record in (
            ("_workflow-service", "service"),
            ("_workflow-probe", "probe"),
            ("_workflow-shutdown", "shutdown"),
        ):
            with self.subTest(action=action):
                result = self.child(action, env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads((self.root / (record + ".json")).read_text()),
                    {"profile": "tools", "runtime": str(self.runtime), "leased": True},
                )
        self.assertFalse(hashes.exists(), "An immutable tree was hashed against itself")
        self.assertEqual(
            queries.read_text().splitlines(),
            [str((self.base / "bootstrap").resolve())] * 3,
        )

    def test_shutdown_rejects_unleased_handoff_and_changed_scope(self):
        for label, changes in (
            ("changed-pin", {"CHAINMAN_ACTIVE_PIN": "b" * 40}),
            ("unleased", {storage.FDS: "[]", storage.PATHS: "[]"}),
        ):
            with self.subTest(handoff=label):
                result = subprocess.run(
                    [
                        str(self.runtime / "bootstrap/chainman.sh"),
                        "_workflow-shutdown",
                        "worker",
                        self.fingerprint,
                    ],
                    cwd=self.root,
                    env=dict(self.env, **changes),
                    pass_fds=storage.inherited()[1],
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "shutdown.json").exists())
        result = self.child("_workflow-shutdown", fingerprint="0" * 64)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "shutdown.json").exists())

    def test_real_lifecycle_keeps_admitted_setup_scope_and_rejects_stale_launch(self):
        configuration = self.root / "chainman.toml"
        configuration.write_text(
            configuration.read_text().replace(
                "[services.worker]\n", '[services.worker]\nsetup=["dependencies"]\n'
            )
            + '\n[setup.dependencies]\nprofile="tools"\ninputs=["setup.lock"]\nartifacts=["installed"]\ncommands=[["true"]]\n'
        )
        inputs = self.root / "setup.lock"
        inputs.write_text("admitted setup inputs")
        admitted = self.fingerprints()
        self.assertNotEqual(admitted["launch"], admitted["lifecycle"])
        inputs.write_text("HMR changed setup inputs")
        updated = self.fingerprints()
        self.assertNotEqual(updated["launch"], admitted["launch"])
        self.assertEqual(updated["lifecycle"], admitted["lifecycle"])
        for action, record in (
            ("_workflow-probe", "probe"),
            ("_workflow-shutdown", "shutdown"),
        ):
            with self.subTest(action=action):
                result = self.child(action, fingerprint=admitted["lifecycle"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue((self.root / (record + ".json")).is_file())
        result = self.child(fingerprint=admitted["launch"])
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("inputs changed", result.stderr)
        self.assertFalse((self.root / "service.json").exists())

    def test_fresh_export_keeps_real_runtime_hash_comparison(self):
        env, hashes, queries = self.recording_nix()
        exported = self.base / "source"
        entry = exported / "bootstrap/reenter.sh"
        argv = [
            str(entry),
            str(self.root),
            "--entry",
            "_workflow-service",
            "worker",
            self.fingerprint,
        ]
        result = tc.managed_run(
            argv,
            env=env,
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "service.json").is_file())
        self.assertEqual(
            hashes.read_text().splitlines(), [str(exported), str(self.runtime)]
        )
        self.assertEqual(
            queries.read_text().splitlines(), [str((self.base / "bootstrap").resolve())]
        )
        (self.root / "service.json").unlink()
        version = exported / "VERSION"
        original = version.read_bytes()
        try:
            version.write_bytes(original + b"\ndisposable mismatched source\n")
            rejected = tc.managed_run(
                argv,
                env=env,
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
                timeout=60,
            )
            self.assertNotEqual(rejected.returncode, 0, rejected.stderr)
            self.assertIn(
                "runtime differs from the verified Git source", rejected.stderr
            )
            self.assertFalse((self.root / "service.json").exists())
            self.assertEqual(
                hashes.read_text().splitlines(),
                [str(exported), str(self.runtime)] * 2,
            )
            self.assertEqual(len(queries.read_text().splitlines()), 1)
        finally:
            version.write_bytes(original)

    def test_tampered_or_unleased_handoff_never_runs_payload(self):
        cases = {
            "other-root": {"CHAINMAN_ROOT": str(self.root / "other")},
            "changed-pin": {"CHAINMAN_ACTIVE_PIN": "b" * 40},
            "wrong-runtime": {"CHAINMAN_RUNTIME": str(self.root)},
            "missing-marker": {"CHAINMAN_INTERNAL_ENTRY": "wrong"},
            "wrong-mode": {"CHAINMAN_ACTIVE_MODE": "host"},
            "closed-descriptor": {storage.FDS: "[999999]"},
            "marker-only": {storage.FDS: "[]", storage.PATHS: "[]"},
            "other-storage-path": {storage.PATHS: json.dumps([str(self.root)])},
            "wrong-interpreter": {"CHAINMAN_RUNTIME_PYTHON": "/usr/bin/python3"},
        }
        for label, changes in cases.items():
            with self.subTest(handoff=label):
                # Pass exactly the selected descriptor labels, including invalid
                # cases, rather than having managed_options repair them.
                result = subprocess.run(
                    [
                        str(self.runtime / "bootstrap/chainman.sh"),
                        "_workflow-service",
                        "worker",
                        self.fingerprint,
                    ],
                    cwd=self.root,
                    env=dict(self.env, **changes),
                    pass_fds=storage.inherited()[1],
                    text=True,
                    capture_output=True,
                    timeout=60,
                )
                self.assertNotEqual(result.returncode, 0, label)
                self.assertFalse((self.root / "service.json").exists())
        for label, arguments in (
            ("stale-fingerprint", {"fingerprint": "0" * 64}),
            ("undeclared-service", {"name": "missing"}),
        ):
            with self.subTest(handoff=label):
                result = self.child(**arguments)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "service.json").exists())
        # A live source lease alone cannot authorize a different interpreter
        # closure, even when the caller supplies all the expected labels.
        invalid_pin = self.root / "chainman.lock"
        invalid_pin.write_text("x" * 40 + "\n")
        try:
            result = self.child(env=dict(self.env, CHAINMAN_ACTIVE_PIN="x" * 40))
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse((self.root / "service.json").exists())
        finally:
            invalid_pin.write_text(self.pin + "\n")
        rejected = tc.managed_run(
            [
                str(self.runtime / "bootstrap/reenter.sh"),
                str(self.root),
                "--entry",
                "_workflow-task",
                "worker",
                self.fingerprint,
            ],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.root / "service.json").exists())
        entry = Path(json.loads(self.env[storage.PATHS])[0])
        bootstrap = entry / "bootstrap"
        bootstrap.unlink()
        bootstrap.symlink_to(self.runtime / "VERSION")
        result = self.child()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn("bootstrap", result.stderr)
        self.assertFalse((self.root / "service.json").exists())

    def test_wrong_interpreter_is_rejected_before_it_can_execute(self):
        marker = self.root / "wrong-interpreter-executed"
        wrapper = self.root / "wrapper"
        (wrapper / "bin").mkdir(parents=True)
        executable = wrapper / "bin/python3"
        executable.write_text(
            "#!/bin/sh\n: > "
            + shlex.quote(str(marker))
            + "\nexec "
            + shlex.quote(self.bootstrap_python)
            + ' "$@"\n'
        )
        executable.chmod(0o755)
        stored = subprocess.check_output(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command",
                "store",
                "add-path",
                "--name",
                "chainman-interpreter-wrapper",
                str(wrapper),
            ],
            text=True,
            timeout=30,
        ).strip()
        subprocess.run(
            [
                str(Path(NIX).with_name("nix-store")),
                "--realise",
                stored,
                "--add-root",
                str(self.root / "wrapper-root"),
                "--indirect",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=30,
        )
        result = self.child(
            env=dict(self.env, CHAINMAN_RUNTIME_PYTHON=stored + "/bin/python3")
        )
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            marker.exists(), "An unaccepted interpreter executed before validation"
        )
        self.assertFalse((self.root / "service.json").exists())


if __name__ == "__main__":
    unittest.main()
