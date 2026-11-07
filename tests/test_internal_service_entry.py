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

SOURCE = Path(__file__).resolve().parents[1]
NIX = shutil.which("nix")


@unittest.skipUnless(NIX, "real managed Nix runtime is required")
class InternalServiceEntryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="chainman internal runtime ")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.base = Path(cls.temporary.name).resolve()
        exported = cls.base / "source"
        inventory = subprocess.check_output(
            [
                "git",
                "-C",
                str(SOURCE),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            text=True,
        ).splitlines()
        for name in inventory:
            if (SOURCE / name).is_file():
                target = exported / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(SOURCE / name, target)
        cls.runtime = Path(
            subprocess.check_output(
                [
                    NIX,
                    "--extra-experimental-features",
                    "nix-command",
                    "store",
                    "add-path",
                    "--name",
                    "chainman-source",
                    str(exported),
                ],
                text=True,
                timeout=60,
            ).strip()
        )
        subprocess.run(
            [
                str(Path(NIX).with_name("nix-store")),
                "--realise",
                str(cls.runtime),
                "--add-root",
                str(cls.base / "runtime-root"),
                "--indirect",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [
                NIX,
                "--extra-experimental-features",
                "nix-command flakes",
                "print-dev-env",
                f"path:{cls.runtime}/nix#bootstrap",
                "--no-write-lock-file",
                "--profile",
                str(cls.base / "bootstrap"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=120,
        )
        cls.bootstrap_python = str(tc.bootstrap_interpreter(cls.base / "bootstrap"))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="case-", dir=self.base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pin = "a" * 40
        (self.root / "chainman.lock").write_text(self.pin + "\n")
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[profiles.tools]
runtime_profile="bootstrap"
[services.worker]
profile="tools"
command=["python3","payload.py","service"]
readiness={command=["python3","payload.py","probe"]}
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
        code = (
            f"import sys;sys.path.insert(0,{str(self.runtime / 'scripts')!r});"
            "import services,toolchain as tc,workflows;from pathlib import Path;"
            f"root=Path({str(self.root)!r});"
            "print(services.config_fingerprint(root,workflows.configuration(root),env=tc.environment(root,create=False)))"
        )
        self.fingerprint = tc.managed_run(
            [sys.executable, "-I", "-B", "-c", code],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
            timeout=15,
        ).stdout.strip()

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

    def test_service_and_probe_activate_declared_profile_with_real_leases(self):
        self.assertNotIn("CHAINMAN_ACTIVE_PROFILE", self.env)
        for action, record in (
            ("_workflow-service", "service"),
            ("_workflow-probe", "probe"),
        ):
            with self.subTest(action=action):
                result = self.child(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                proof = json.loads((self.root / (record + ".json")).read_text())
                self.assertEqual(
                    proof,
                    {"profile": "tools", "runtime": str(self.runtime), "leased": True},
                )

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
