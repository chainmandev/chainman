"""Checked public and service commands retain their admitted setup boundaries."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import chainman
import pnpm_setup
import services
import toolchain as tc
import workflows


@unittest.skipUnless(shutil.which("pnpm"), "requires the pinned JavaScript profile")
class ActualPnpmEntryTests(unittest.TestCase):
    def test_managed_public_and_service_commands_preserve_the_frozen_lock(self):
        version = subprocess.check_output(
            ["pnpm", "--version"], cwd="/", text=True
        ).strip()
        with tempfile.TemporaryDirectory(prefix="pnpm actual entries ") as temporary:
            base = Path(temporary).resolve()
            root = base / "consumer"
            dependency = root / "dependency"
            dependency.mkdir(parents=True)
            (dependency / "package.json").write_text(
                '{"name":"local-fixture","version":"1.0.0"}'
            )
            (root / "package.json").write_text(
                json.dumps(
                    {
                        "name": "checked-entry-fixture",
                        "packageManager": "pnpm@" + version,
                        "dependencies": {"local-fixture": "file:./dependency"},
                        "scripts": {
                            "outer": "pnpm run inner",
                            "inner": "node -e \"require('fs').appendFileSync('ran','yes\\n')\"",
                        },
                    }
                )
            )
            (root / "pnpm-workspace.yaml").write_text("packages: []\npmOnFail: error\n")
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[cache]
automatic_prune=false
[profiles.host]
entry_setup=["js"]
[setup.js]
pnpm=true
inputs=["package.json","pnpm-lock.yaml","pnpm-workspace.yaml"]
artifacts=["node_modules/.modules.yaml"]
[services.worker]
setup=["js"]
command=["pnpm","run","outer"]
readiness={command=["pnpm","run","outer"],timeout_seconds=10}
shutdown_command=["pnpm","run","outer"]
""")
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
            }
            env.update(
                CHAINMAN_MODE="host-nix",
                CHAINMAN_SETUP="auto",
                TOOLCHAIN_DOWNLOAD_CACHE=str(base / "downloads"),
                XDG_CACHE_HOME=str(base / "cache"),
                XDG_CONFIG_HOME=str(base / "config"),
                XDG_DATA_HOME=str(base / "data"),
                XDG_STATE_HOME=str(base / "state"),
                PNPM_HOME=str(base / "manager-home"),
                PNPM_CONFIG_REGISTRY="http://127.0.0.1:1",
                npm_config_registry="http://127.0.0.1:1",
            )
            with patch.dict(os.environ, env, clear=True):
                # The same disposable store policy reaches lock creation, setup,
                # public entries and every nested package-manager process.
                selected = tc.environment(root)
                with patch.dict(os.environ, selected, clear=True):
                    subprocess.run(
                        [
                            "pnpm",
                            *pnpm_setup.options(version, version),
                            "install",
                            "--offline",
                            "--lockfile-only",
                        ],
                        cwd=root,
                        check=True,
                        capture_output=True,
                    )
                    lock = (root / "pnpm-lock.yaml").read_bytes()
                    for action in ("exec", "shell"):
                        self.assertEqual(
                            chainman.main(
                                [
                                    "--root",
                                    str(root),
                                    action,
                                    "--",
                                    "pnpm",
                                    "run",
                                    "outer",
                                ]
                            ),
                            0,
                        )
                    cfg = workflows.configuration(root)
                    for action in (
                        "_workflow-service",
                        "_workflow-probe",
                        "_workflow-shutdown",
                    ):
                        expected = services.config_fingerprint(
                            root,
                            cfg,
                            env=tc.environment(root, create=False),
                            include_setup_inputs=action == "_workflow-service",
                        )
                        self.assertEqual(
                            services.execute_internal(
                                root, action, ["worker", expected]
                            ),
                            0,
                        )
                    self.assertEqual(
                        (root / "ran").read_text().splitlines(), ["yes"] * 5
                    )
                    self.assertEqual((root / "pnpm-lock.yaml").read_bytes(), lock)
                    self.assertNotIn(b"packageManagerDependencies:", lock)
                    self.assertFalse((base / "manager-home/.tools").exists())
                    self.assertFalse((base / "data/pnpm/.tools").exists())


class CheckedEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman checked entries ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "consumer"
        self.root.mkdir()
        self.tools = self.base / "selected tools"
        self.tools.mkdir()
        self.probes = self.base / "probes"
        self.version = self.base / "version"
        self.version.write_text("12.1.0")
        pnpm = self.tools / "pnpm"
        pnpm.write_text(
            f"#!{sys.executable}\n"
            "import os,sys,time\nfrom pathlib import Path\n"
            "assert sys.argv[1:]==['--version']\n"
            "with Path(os.environ['FIXTURE_PROBES']).open('a') as out: out.write(str(Path.cwd())+'\\n')\n"
            "print(Path(os.environ['FIXTURE_VERSION']).read_text(),flush=True)\n"
            "time.sleep(float(os.environ.get('FIXTURE_PROBE_DELAY','0')))\n"
        )
        pnpm.chmod(0o700)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        env.update(
            CHAINMAN_MODE="host-nix",
            TOOLCHAIN_DOWNLOAD_CACHE=str(self.base / "downloads"),
            XDG_CACHE_HOME=str(self.base / "cache"),
            FIXTURE_PROBES=str(self.probes),
            FIXTURE_VERSION=str(self.version),
        )
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        # Setup installation/readiness already has independent coverage. Keep
        # actual operation and setup-use leases while admitting this fixture.
        details = patch.object(
            workflows, "setup_detail", return_value={"current": True, "reason": ""}
        )
        details.start()
        self.addCleanup(details.stop)
        (self.root / "package.json").write_text('{"packageManager":"pnpm@12.1.0"}')
        (self.root / "installed").touch()
        selected_path = str(self.tools) + os.pathsep + os.environ["PATH"]
        self.body = f"""schema=3
[project]
default_profile="host"
[cache]
automatic_prune=false
[profiles.host]
entry_setup=["bridge"]
[profiles.host.environment]
PATH={json.dumps(selected_path)}
FIXTURE_PROFILE="selected"
[setup.managed]
pnpm=true
inputs=["package.json"]
artifacts=["installed"]
[setup.bridge]
depends_on=["managed"]
commands=[["true"]]
inputs=["package.json"]
artifacts=["installed"]
[setup.unmanaged]
commands=[["true"]]
inputs=["package.json"]
artifacts=["installed"]
[services.worker]
setup=["bridge"]
command=[{json.dumps(sys.executable)},"payload.py","service"]
readiness={{command=[{json.dumps(sys.executable)},"payload.py","probe"],timeout_seconds=1}}
shutdown_command=[{json.dumps(sys.executable)},"payload.py","shutdown"]
[services.plain]
depends_on=["worker"]
setup=["unmanaged"]
command=[{json.dumps(sys.executable)},"payload.py","plain-service"]
readiness={{command=[{json.dumps(sys.executable)},"payload.py","plain-probe"],timeout_seconds=1}}
shutdown_command=[{json.dumps(sys.executable)},"payload.py","plain-shutdown"]
"""
        self.write_config()
        (self.root / "payload.py").write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
            "import toolchain as tc\n"
            "fd,gate,identity,compat,ancestors=tc.inherited_operation()\n"
            "lease=Path('.cache/toolchain/setup-use.lock')\n"
            "held=lease.exists() and any(os.path.samestat(os.fstat(item),lease.stat()) for item in ancestors)\n"
            "Path(sys.argv[1]+'.json').write_text(json.dumps({'pm':os.environ['PNPM_CONFIG_PM_ON_FAIL'],'verify':os.environ['PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN'],'profile':os.environ['FIXTURE_PROFILE'],'operation':fd is not None,'setup_lease':held}))\n"
        )

    def write_config(self):
        (self.root / "chainman.toml").write_text(self.body)

    def public(self, action, label="public", *, reuse=False):
        return chainman.main(
            [
                "--root",
                str(self.root),
                action,
                *(["--reuse-operation"] if reuse else []),
                "--",
                sys.executable,
                "payload.py",
                label,
            ]
        )

    def service(self, action, name="worker"):
        cfg = workflows.configuration(self.root)
        expected = services.config_fingerprint(
            self.root,
            cfg,
            env=tc.environment(self.root, create=False),
            include_setup_inputs=action == "_workflow-service",
        )
        return services.execute_internal(self.root, action, [name, expected])

    def assert_payload(self, label, *, checked, leased=True):
        self.assertEqual(
            json.loads((self.root / (label + ".json")).read_text()),
            {
                "pm": "ignore" if checked else "error",
                "verify": "error",
                "profile": "selected",
                "operation": True,
                "setup_lease": leased,
            },
        )

    def test_exec_and_shell_check_selected_profile_and_keep_setup_leases(self):
        with patch.object(workflows, "setup_use", wraps=workflows.setup_use) as setup:
            for action in ("exec", "shell"):
                with self.subTest(action=action):
                    self.assertEqual(self.public(action, action), 0)
                    self.assert_payload(action, checked=True)
            self.assertEqual(setup.call_count, 2)
        self.assertEqual(self.probes.read_text().splitlines(), ["/", "/"])

    def test_public_mismatch_stops_before_payload(self):
        self.version.write_text("11.0.0")
        for action in ("exec", "shell"):
            with self.subTest(action=action):
                self.assertNotEqual(self.public(action, action), 0)
                self.assertFalse((self.root / (action + ".json")).exists())

    def test_unmanaged_and_internal_entries_keep_existing_policy(self):
        self.body = self.body.replace(
            'entry_setup=["bridge"]', 'entry_setup=["unmanaged"]'
        )
        self.write_config()
        for action in ("exec", "shell"):
            with self.subTest(action=action):
                self.assertEqual(self.public(action, action), 0)
                self.assert_payload(action, checked=False)
        self.assertFalse(self.probes.exists())
        self.body = self.body.replace(
            'entry_setup=["unmanaged"]', 'entry_setup=["bridge"]'
        )
        self.write_config()
        with (
            tc.operation(self.root, exclusive=False, automatic_prune=False),
            patch.object(workflows, "setup_use", wraps=workflows.setup_use) as setup,
        ):
            self.assertEqual(self.public("exec", "internal", reuse=True), 0)
            self.assertEqual(setup.call_args.args[2], [])
        self.assert_payload("internal", checked=False, leased=False)
        self.assertFalse(self.probes.exists())

    def test_service_command_checks_only_its_setup_and_keeps_lease(self):
        self.assertEqual(self.service("_workflow-service"), 0)
        self.assert_payload("service", checked=True)
        self.assertEqual(self.service("_workflow-service", "plain"), 0)
        self.assert_payload("plain-service", checked=False)
        self.assertEqual(self.probes.read_text().splitlines(), ["/"])

    def test_lifecycle_checks_identity_without_reacquiring_setup(self):
        cfg = workflows.configuration(self.root)
        with (
            tc.operation(self.root, exclusive=False, automatic_prune=False),
            workflows.setup_use(
                self.root, cfg, ["bridge"], tc.environment(self.root)
            ) as descriptors,
        ):
            fd, gate, identity, compat, ancestors = tc.inherited_operation()
            with (
                tc.operation_state(
                    fd, gate, identity, compat, (*ancestors, *descriptors)
                ),
                patch.object(
                    workflows,
                    "setup_use",
                    side_effect=AssertionError("lifecycle reacquired setup"),
                ),
            ):
                for action, label in (
                    ("_workflow-probe", "probe"),
                    ("_workflow-shutdown", "shutdown"),
                ):
                    with self.subTest(action=action):
                        self.assertEqual(self.service(action), 0)
                        self.assert_payload(label, checked=True)
                        self.assertEqual(self.service(action, "plain"), 0)
                        self.assert_payload("plain-" + label, checked=False)
                self.version.write_text("11.0.0")
                for action, label in (
                    ("_workflow-probe", "probe"),
                    ("_workflow-shutdown", "shutdown"),
                ):
                    with self.subTest(mismatch=action):
                        (self.root / (label + ".json")).unlink()
                        self.assertNotEqual(self.service(action), 0)
                        self.assertFalse((self.root / (label + ".json")).exists())

    def test_service_profile_mismatch_stops_before_payload(self):
        self.version.write_text("11.0.0")
        self.assertNotEqual(self.service("_workflow-service"), 0)
        self.assertFalse((self.root / "service.json").exists())

    def test_readiness_deadline_includes_identity_probe(self):
        start = time.monotonic()
        with patch.dict(os.environ, FIXTURE_PROBE_DELAY="4"):
            self.assertEqual(self.service("_workflow-probe"), 124)
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse((self.root / "probe.json").exists())
        self.assertEqual(self.probes.read_text().splitlines(), ["/"])


if __name__ == "__main__":
    unittest.main()
