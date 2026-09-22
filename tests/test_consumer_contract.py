"""Pin and configuration checks are static and do not execute project hooks."""

import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configuration_files
import consumer_contract


class ConsumerContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = {"revision": "a" * 40}
        (self.root / "chainman.toml").write_text(
            'schema=3\n[project]\ndefault_profile="host"\n[templates.tasks.base]\ncommands=[["false"]]\n[tasks.test]\nextends="base"\n[runtime]\ncopies=["copy"]\n'
        )
        (self.root / "chainman.lock").write_text("a" * 40 + "\n")
        (self.root / "copy").mkdir()
        shutil.copyfile(self.root / "chainman.lock", self.root / "copy/chainman.lock")

    def test_matching_revision_and_expanded_baseline(self):
        baseline = {
            "schema": 2,
            "project": {"default_profile": "host"},
            "tasks": {"test": {"commands": [["false"]]}},
            "runtime": {"copies": ["copy"]},
        }
        result = consumer_contract.check(self.root, self.release, baseline)
        self.assertTrue(result["baseline_equal"])
        self.assertEqual(result["runtime_files"], 2)
        baseline["tasks"]["test"]["commands"] = [["true"]]
        with self.assertRaisesRegex(ValueError, "baseline"):
            consumer_contract.check(self.root, self.release, baseline)

    def test_wrong_pin_and_copy_drift_fail(self):
        (self.root / "copy/chainman.lock").write_text("b" * 40 + "\n")
        with self.assertRaisesRegex(ValueError, "copy differs"):
            consumer_contract.check(self.root, self.release)
        (self.root / "chainman.lock").write_text("b" * 40 + "\n")
        with self.assertRaisesRegex(ValueError, "revision differs"):
            consumer_contract.check(self.root, self.release)

    def test_pnpm_baseline_expansion_preserves_input_and_detects_changes(self):
        with (self.root / "chainman.toml").open("a") as stream:
            stream.write(
                '\n[setup.javascript]\npnpm=true\ninputs=["package.json"]\nartifacts=["node_modules/.modules.yaml"]\n'
            )
        baseline = configuration_files.read(self.root).data
        original = deepcopy(baseline)
        result = consumer_contract.check(self.root, self.release, baseline)
        self.assertTrue(result["baseline_equal"])
        self.assertEqual(baseline, original)
        baseline["setup"]["javascript"]["inputs"].append("pnpm-lock.yaml")
        with self.assertRaisesRegex(ValueError, "baseline"):
            consumer_contract.check(self.root, self.release, baseline)

    def test_pnpm_baseline_does_not_hide_conflicting_installer(self):
        baseline = configuration_files.read(self.root).data
        baseline["setup"] = {"javascript": {"pnpm": True, "commands": [["unexpected"]]}}
        with self.assertRaisesRegex(ValueError, "supplies commands and readiness"):
            consumer_contract.check(self.root, self.release, baseline)

    def test_copy_permissions_and_symlink_boundary(self):
        (self.root / "chainman.lock").chmod(0o664)
        target = self.root / "copy/chainman.lock"
        target.chmod(0o444)
        self.assertTrue(consumer_contract.check(self.root, self.release)["valid"])
        target.unlink()
        target.symlink_to(self.root / "chainman.lock")
        with self.assertRaises(ValueError):
            consumer_contract.check(self.root, self.release)

    def test_old_archive_lock_fails(self):
        (self.root / "chainman.lock").write_text('{"schema":1}\n')
        with self.assertRaisesRegex(ValueError, "full lowercase Git SHA"):
            consumer_contract.check(self.root, self.release)


if __name__ == "__main__":
    unittest.main()
