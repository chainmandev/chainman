"""Recursive input inventories agree across supported Python interpreters."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman


class InputInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman recursive inputs ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data/nested").mkdir(parents=True)
        for name in ("top.txt", "nested/input.txt", "nested/excluded.txt"):
            (self.root / "data" / name).write_text("before")

    def test_recursive_patterns_include_root_and_nested_files(self):
        for pattern in ("data/**", "data/**/*", "data/**/**"):
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    set(
                        chainman.input_digests(
                            self.root, [pattern], ["**/excluded.txt"]
                        )
                    ),
                    {"data/top.txt", "data/nested/input.txt"},
                )
        self.assertEqual(chainman.input_digests(self.root, ["data/**/"]), {})

    def test_recursive_inventory_rejects_external_symlinks(self):
        (self.root / "data/escape").symlink_to(self.root.parent / "outside")
        with self.assertRaises(ValueError):
            chainman.input_digests(self.root, ["data/**"])

    def test_setup_invalidation_on_current_and_minimum_python(self):
        interpreters = [sys.executable]
        if minimum := os.environ.get("CHAINMAN_TEST_MINIMUM_PYTHON"):
            interpreters.append(minimum)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        env.update(CHAINMAN_MODE="host", CHAINMAN_SETUP="auto")
        (self.root / "chainman.toml").write_text("""schema=3
[setup.data]
inputs=["data/**"]
exclude_inputs=["**/excluded.txt"]
artifacts=["ready"]
commands=[["sh","-c","printf ready > ready"]]
""")
        for interpreter in interpreters:
            with self.subTest(interpreter=interpreter):

                def run(*args):
                    return subprocess.run(
                        [
                            interpreter,
                            "-B",
                            str(chainman.RUNTIME / "scripts/chainman.py"),
                            "--root",
                            str(self.root),
                            *args,
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )

                for action in ("changed", "added", "missing"):
                    result = run("setup")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    excluded = self.root / "data/nested/excluded.txt"
                    excluded.write_text(excluded.read_text() + "x")
                    self.assertEqual(run("setup-status").returncode, 0)
                    target = self.root / "data/nested/input.txt"
                    if action == "changed":
                        target.write_text(target.read_text() + "x")
                    elif action == "added":
                        target = self.root / "data/new.txt"
                        target.write_text("new")
                    else:
                        target = self.root / "data/new.txt"
                        target.unlink()
                    result = run("setup-status")
                    self.assertEqual(result.returncode, 1, result.stderr)
                    detail = json.loads(result.stdout)["details"]["data"]
                    self.assertEqual(detail["reason"], "inputs-changed")
                    self.assertEqual(
                        detail["changed_inputs"][action],
                        ["setup:" + target.relative_to(self.root).as_posix()],
                    )


if __name__ == "__main__":
    unittest.main()
