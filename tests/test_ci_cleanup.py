"""Disposable hosted SDK pruning never becomes general host cleanup."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ci_cleanup


class CleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hosted SDK fixture ")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root, self.fs = self.base / "project", self.base / "filesystem"
        (self.root / "modules").mkdir(parents=True)
        (self.root / "toolchain.toml").write_text(
            'schema=1\nmodules=["core"]\n[ci]\ndisposable_sdks=["android","dotnet","haskell"]\n'
        )
        for name in ("core", "flutter"):
            (self.root / f"modules/{name}.toml").write_text(
                f'name="{name}"\nprofile="{name}"\ndirectory="."\n'
            )
        for relative in ci_cleanup.SDK_PATHS.values():
            path = self.fs / relative
            path.mkdir(parents=True)
            (path / "sdk").write_bytes(b"disposable")
        self.env = dict(
            CI="true",
            GITHUB_ACTIONS="true",
            RUNNER_ENVIRONMENT="github-hosted",
            RUNNER_OS="Linux",
        )

    def test_required_android_is_preserved_and_preview_does_not_delete(self):
        paths = ci_cleanup.plan(self.root, self.fs, self.env, ["flutter"])
        self.assertEqual(
            {p.relative_to(self.fs).as_posix() for p in paths},
            {ci_cleanup.SDK_PATHS[s] for s in ("dotnet", "haskell")},
        )
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual(ci_cleanup.remove(paths), [str(p) for p in paths])
        self.assertEqual(
            (self.fs / ci_cleanup.SDK_PATHS["android"] / "sdk").read_bytes(),
            b"disposable",
        )

    def test_every_hosted_guard_and_container_refusal(self):
        for key in self.env:
            env = self.env | {key: "self-hosted"}
            with self.assertRaisesRegex(ValueError, "disposable"):
                ci_cleanup.plan(self.root, self.fs, env, [])
        with self.assertRaisesRegex(ValueError, "disposable"):
            ci_cleanup.plan(
                self.root, self.fs, self.env | {"TOOLCHAIN_CONTAINER": "1"}, []
            )

    def test_symlink_escape_rejects_whole_plan(self):
        target = self.fs / ci_cleanup.SDK_PATHS["haskell"] / "escape"
        target.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escape"):
            ci_cleanup.plan(self.root, self.fs, self.env, [])
        self.assertTrue((self.fs / ci_cleanup.SDK_PATHS["android"]).is_dir())

    def test_actual_deletion_failure_propagates(self):
        paths = ci_cleanup.plan(self.root, self.fs, self.env, [])
        with patch.object(
            ci_cleanup.shutil, "rmtree", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(PermissionError):
                ci_cleanup.remove(paths)
        self.assertTrue(all(p.exists() for p in paths))
