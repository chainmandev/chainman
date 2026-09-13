from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services
import workflows


class VolumeCompatibilityTests(unittest.TestCase):
    def test_setup_can_produce_a_volume_identity_before_planning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "seed-input").write_text("canonical seed")
            (root / "prepare.py").write_text(
                "from pathlib import Path\n"
                "Path('seed-identity').write_text(Path('seed-input').read_text())\n"
            )
            (root / "chainman.toml").write_text("""schema=2
[project]
default_profile="host"
[setup.identity]
inputs=["seed-input", "prepare.py"]
artifacts=[{path="seed-identity",digest=true}]
commands=[["python3","prepare.py"]]
[services.database.container]
image="example.invalid/database@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
volumes=[{name="data",target="/data",format="v1",inputs=["seed-identity"]}]
[tasks.database]
setup=["identity"]
services=["database"]
commands=[["true"]]
""")
            cfg = workflows.configuration(root)
            with self.assertRaisesRegex(ValueError, "matched no files"):
                services.config_fingerprint(root, cfg)
            self.assertEqual(services.prepare_requested(root, ["database"]), 0)
            expected = services.config_fingerprint(root, cfg)
            self.assertEqual((root / "seed-identity").read_text(), "canonical seed")
            services.execute_internal(root, "_workflow-prepare", ["database", expected])
            (root / "seed-identity").write_text("concurrent alteration")
            with self.assertRaisesRegex(ValueError, "changed after planning"):
                services.execute_internal(
                    root, "_workflow-prepare", ["database", expected]
                )

    def test_content_identity_is_independent_of_worktree_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
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
            root = Path(temporary).resolve() / "root"
            root.mkdir()
            (Path(temporary).resolve() / "outside").write_text("private")
            (root / "linked").symlink_to(Path(temporary).resolve() / "outside")
            for inputs in (["missing"], ["linked"]):
                with self.assertRaises(ValueError):
                    services.volume_compatibility(
                        root, {"format": "v1", "inputs": inputs}
                    )
            with self.assertRaisesRegex(ValueError, "data format"):
                services.volume_compatibility(root, {"inputs": []})
