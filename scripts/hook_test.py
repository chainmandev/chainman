"""Focused native-hook integration tests; no application builds or pushes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="chainman-hook-tests-") as temporary:
    binary = str(Path(temporary) / "chainman-control")
    subprocess.run(
        ["go", "build", "-o", binary, "."], cwd=root / "nix/control", check=True
    )
    raise SystemExit(
        subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_native_hooks.py",
                "-v",
            ],
            cwd=root,
            env=dict(os.environ, CHAINMAN_TEST_HOOK_CONTROL=binary),
        ).returncode
    )
