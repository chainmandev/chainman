"""Optional timing preserves real command behavior and omits sensitive payloads."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman


class TimingTests(unittest.TestCase):
    def test_opt_in_records_phases_without_argv_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chainman.toml").write_text(
                'schema=2\n[project]\ndefault_profile="host"\n'
            )
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("CHAINMAN_", "TOOLCHAIN_"))
            }
            env.update(
                TOOLCHAIN_DOWNLOAD_CACHE=str(root / "downloads"),
                PRIVATE_TOKEN="private-env-token",
            )
            command = [
                sys.executable,
                str(chainman.RUNTIME / "scripts/chainman.py"),
                "--root",
                str(root),
                "exec",
                "--profile",
                "host",
                "--",
                sys.executable,
                "-c",
                "import sys; print('private-argv-token'); sys.exit(7)",
            ]
            plain = subprocess.run(command, env=env, text=True, capture_output=True)
            timed = subprocess.run(
                command,
                env=dict(env, CHAINMAN_TIMING="1"),
                text=True,
                capture_output=True,
            )
            self.assertEqual((plain.returncode, timed.returncode), (7, 7))
            self.assertEqual(plain.stdout, timed.stdout)
            self.assertNotIn("CHAINMAN_TIMING ", plain.stderr)
            self.assertNotIn("private-argv-token", timed.stderr)
            self.assertNotIn("private-env-token", timed.stderr)
            records = [
                json.loads(line.removeprefix("CHAINMAN_TIMING "))
                for line in timed.stderr.splitlines()
                if line.startswith("CHAINMAN_TIMING ")
            ]
            self.assertEqual(
                [(r["phase"], r["event"]) for r in records],
                [
                    ("profile_entry", "start"),
                    ("profile_entry", "end"),
                    ("command", "start"),
                    ("command", "end"),
                ],
            )
            self.assertEqual(len({r["operation"] for r in records}), 1)
            self.assertEqual(len({r["parent"] for r in records}), 1)
            self.assertNotEqual(records[0]["operation"], records[0]["parent"])
            self.assertEqual(
                sorted(r["monotonic_ns"] for r in records),
                [r["monotonic_ns"] for r in records],
            )


if __name__ == "__main__":
    unittest.main()
