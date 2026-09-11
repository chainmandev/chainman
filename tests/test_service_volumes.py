from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services


class VolumeCompatibilityTests(unittest.TestCase):
    def test_content_identity_is_independent_of_worktree_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trees = [root / "one", root / "another worktree"]
            for tree in trees:
                (tree / "migrations/nested").mkdir(parents=True)
                (tree / "migrations/nested/one.sql").write_text("schema one")
            spec = {"format": "postgres-18", "inputs": ["migrations"]}
            before = services.volume_compatibility(trees[0], spec)
            self.assertEqual(before, services.volume_compatibility(trees[1], spec))
            (trees[1] / "migrations/nested/one.sql").write_text("schema two")
            self.assertNotEqual(before, services.volume_compatibility(trees[1], spec))

    def test_missing_inputs_and_symlink_escapes_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            (Path(temporary) / "outside").write_text("private")
            (root / "linked").symlink_to(Path(temporary) / "outside")
            for inputs in (["missing"], ["linked"]):
                with self.assertRaises(ValueError):
                    services.volume_compatibility(
                        root, {"format": "v1", "inputs": inputs}
                    )
            with self.assertRaisesRegex(ValueError, "data format"):
                services.volume_compatibility(root, {"inputs": []})
