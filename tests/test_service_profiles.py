"""Service provisioning roots a real Nix profile without running its shell hook."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services


@unittest.skipUnless(shutil.which("nix"), "requires a real Nix profile")
class ServiceProfileTests(unittest.TestCase):
    def test_prepared_profile_is_rooted_until_owner_releases_it_without_shell_hook(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="chainman service profile "
        ) as temporary:
            root = Path(temporary).resolve()
            environment = root / "environment"
            environment.mkdir()
            (environment / "flake.nix").write_text(
                "{ inputs.base.url = "
                + json.dumps(
                    services.tc.nix_path_reference(
                        services.chainman.RUNTIME / "nix", ""
                    ).removesuffix("#")
                )
                + "; outputs = { base, ... }: { devShells = builtins.mapAttrs "
                + "(system: shells: { fixture = shells.core.overrideAttrs (old: { "
                + 'shellHook = (old.shellHook or "") + '
                + json.dumps("\ntouch " + shlex.quote(str(root / "shell-hook-ran")))
                + "; }); }) base.devShells; }; }"
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
            }
            env.update(
                CHAINMAN_MODE="host-nix",
                CHAINMAN_SETUP="auto",
                TOOLCHAIN_DOWNLOAD_CACHE=str(root / "downloads"),
            )
            subprocess.run(
                [
                    services.tc.nix_command(),
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "flake",
                    "lock",
                ],
                cwd=environment,
                env=env,
                check=True,
                capture_output=True,
                timeout=120,
            )
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[profiles.fixture]
flake="environment#fixture"
[services.worker]
profile="fixture"
command=["false"]
""")
            channel = root / "channel"
            (channel / "incoming").mkdir(parents=True, mode=0o700)
            (channel / "outgoing").mkdir(mode=0o700)
            alive = (channel / "outgoing/alive").open("w")
            fcntl.flock(alive, fcntl.LOCK_EX)
            env["CHAINMAN_PROFILE_CHANNEL"] = str(channel)
            # Resolve the fingerprint in the same clean environment as the child.
            from unittest.mock import patch

            with patch.dict(os.environ, env, clear=True):
                cfg = services.workflows.configuration(root)
                expected = services.config_fingerprint(
                    root, cfg, env=services.tc.environment(root, create=False)
                )
            pool = Path(tempfile.gettempdir()) / f"chainman-gc-roots-{os.getuid()}"
            previous = set(pool.glob("chainman-service-profile-*/profile"))
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(services.chainman.RUNTIME / "scripts/chainman.py"),
                    "--root",
                    str(root),
                    "_workflow-profile",
                    "worker",
                    expected,
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 120
                while not (channel / "incoming/ready.json").exists():
                    if process.poll() is not None:
                        output, error = process.communicate()
                        self.fail(
                            f"Profile provision exited {process.returncode}: {output!r} {error!r}"
                        )
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.05)
                self.assertFalse((root / "shell-hook-ran").exists())
                roots = [
                    path
                    for path in pool.glob("chainman-service-profile-*/profile")
                    if path not in previous and path.is_symlink()
                ]
                self.assertEqual(
                    len(roots), 1, "Expected the prepared profile's retained root"
                )
                profile = roots[0]
                registered = subprocess.check_output(
                    ["nix-store", "--query", "--roots", str(profile.resolve())],
                    text=True,
                )
                self.assertIn(str(profile), registered)
                alive.close()
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, f"{output!r} {error!r}")
                self.assertFalse(profile.parent.exists())
                self.assertFalse((root / "shell-hook-ran").exists())
            finally:
                alive.close()
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=15)
