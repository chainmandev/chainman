"""Project environment data must never become host launcher authority."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services


@unittest.skipUnless(shutil.which("nix"), "requires a rooted Nix fixture")
class ServiceExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="chainman export ")
        cls.addClassCleanup(cls.temporary.cleanup)
        base = Path(cls.temporary.name)
        package = base / "package"
        (package / "bin").mkdir(parents=True)
        for name in ("chainman-control", "process-compose", "watchexec"):
            (package / "bin" / name).write_text("#!/bin/sh\nexit 0\n")
        cls.package = subprocess.check_output(
            [
                services.tc.nix_command(),
                "--extra-experimental-features",
                "nix-command flakes",
                "build",
                "--impure",
                "--expr",
                'builtins.path { path = builtins.toPath (builtins.getEnv "CHAINMAN_TEST_PACKAGE"); name = "chainman-export-fixture"; }',
                "--out-link",
                str(base / "root"),
                "--print-out-paths",
            ],
            env=dict(os.environ, CHAINMAN_TEST_PACKAGE=str(package)),
            text=True,
        ).strip()

    def test_project_values_stay_out_of_all_host_command_environments(self):
        with tempfile.TemporaryDirectory(prefix="chainman candidate ") as temporary:
            base = Path(temporary)
            root, output = base / "project", base / "private-output"
            root.mkdir()
            output.mkdir()
            (root / "scripts").mkdir()
            launcher = root / "scripts/chainman.sh"
            shutil.copy2(services.chainman.RUNTIME / "bootstrap/chainman.sh", launcher)
            payload = root / ".cache/payload"
            payload.mkdir(parents=True)
            marker = base / "host-code-executed"
            (payload / "dirname").write_text(
                "#!/bin/sh\ntouch "
                + shlex.quote(str(marker))
                + '\nexec /usr/bin/dirname "$@"\n'
            )
            (payload / "dirname").chmod(0o755)
            (root / "project.env").write_text(
                f"PATH={payload}:/usr/bin:/bin\n"
                "LD_PRELOAD=/candidate/library\nBASH_ENV=/candidate/bash\n"
                "PYTHONPATH=/candidate/python\nNODE_OPTIONS=--require=/candidate/js\n"
                "DOCKER_HOST=unix:///candidate/engine\nHOST_SEED=project\n"
            )
            (root / "chainman.toml").write_text(
                """schema=2
[project]
default_profile="host"
[environment]
files=[{path="project.env",override=true}]
pass=["HOST_SEED"]
[tasks.main]
commands=[["true"]]
services=["worker","database"]
context_environment={APP_CONTEXT="{env:HOST_SEED}"}
[tasks.build]
commands=[["true"]]
[tasks.other]
commands=[["true"]]
services=["worker","database"]
context_environment={APP_CONTEXT="{env:HOST_SEED}"}
[tasks.changed]
commands=[["true"]]
services=["worker","database"]
context_environment={APP_CONTEXT="different"}
[services.worker]
command=["true"]
environment={PYTHONPATH="{root}/service-python"}
watch={task="build",paths=["chainman.toml"]}
readiness={command=["true"]}
[services.database]
container={image="example.invalid/database@sha256:AAAAAAAA",environment={DATA_CONTEXT="{env:APP_CONTEXT}"}}
readiness={command=["true"]}
""".replace("AAAAAAAA", "a" * 64)
            )
            (output / "host-environment").write_bytes(b"HOST_SEED=caller\0")
            real_run = subprocess.run

            def run(argv, **kwargs):
                if "--out-link" in argv:
                    return subprocess.CompletedProcess(argv, 0, self.package + "\n")
                return real_run(argv, **kwargs)

            def commands(value):
                if isinstance(value, dict):
                    if "argv" in value:
                        yield value
                    for item in value.values():
                        yield from commands(item)
                elif isinstance(value, list):
                    for item in value:
                        yield from commands(item)

            for mode in ("host-nix", "container-nix"):
                with (
                    self.subTest(mode=mode),
                    patch.dict(os.environ, CHAINMAN_MODE=mode),
                    patch.object(services.subprocess, "run", side_effect=run),
                ):
                    plans = {}
                    for task in ("main", "other", "changed"):
                        services.export(
                            root,
                            [
                                str(output),
                                "linux-arm64",
                                str(base / "state"),
                                "/fixture/docker",
                                str(launcher),
                                "run",
                                task,
                            ],
                        )
                        plans[task] = json.loads((output / "plan.json").read_text())
                plan = plans["main"]
                self.assertEqual(plan["fingerprint"], plans["other"]["fingerprint"])
                self.assertNotEqual(
                    plan["fingerprint"], plans["changed"]["fingerprint"]
                )
                for command in commands(plan):
                    environment = command["environment"]
                    self.assertFalse(
                        set(environment)
                        & {
                            "PATH",
                            "LD_PRELOAD",
                            "BASH_ENV",
                            "PYTHONPATH",
                            "NODE_OPTIONS",
                            "DOCKER_HOST",
                            "APP_CONTEXT",
                        }
                    )
                    if "HOST_SEED" in environment:
                        self.assertEqual(environment["HOST_SEED"], "caller")
                self.assertEqual(
                    plan["prepare"]["environment"]["CHAINMAN_CONTEXT_TASK"], "main"
                )
                self.assertIn(
                    "DATA_CONTEXT=project",
                    plan["services"]["database"]["command"]["argv"],
                )
                # Exercise the actual launcher's early host commands. No pin is
                # present, so it exits before Nix or any engine can execute.
                result = real_run(
                    plan["prepare"]["argv"],
                    cwd=root,
                    env=dict(os.environ, **plan["prepare"]["environment"]),
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists(), result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
