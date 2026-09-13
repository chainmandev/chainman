"""Release and behavior checks fail on consumer drift without executing hooks."""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman
import consumer_contract


class ConsumerContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = {
            "version": "0.1.0",
            "revision": "a" * 40,
            "url": "https://example.com/release",
            "narHash": "fixture",
            "archive_sha256": hashlib.sha256(b"fixture").hexdigest(),
        }
        (self.root / "chainman.toml").write_text(
            'schema=3\n[project]\ndefault_profile="host"\n[templates.tasks.base]\ncommands=[["false"]]\n[tasks.test]\nextends="base"\n[runtime]\ncopies=["copy"]\n'
        )
        (self.root / "chainman.lock").write_text(
            json.dumps(dict(self.release, bundled_archive="bundle.tar.gz"))
        )
        (self.root / "bundle.tar.gz").write_bytes(b"fixture")
        (self.root / "scripts").mkdir()
        for source, target in (
            ("chainman.sh", "chainman.sh"),
            ("fetch.nix", "chainman-fetch.nix"),
        ):
            shutil.copy2(
                chainman.RUNTIME / "bootstrap" / source, self.root / "scripts" / target
            )
        (self.root / "copy").mkdir()
        for name in ("chainman.lock", "bundle.tar.gz", "scripts"):
            source, target = self.root / name, self.root / "copy" / name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)

    def test_matching_release_and_expanded_baseline(self):
        baseline = {
            "schema": 2,
            "project": {"default_profile": "host"},
            "tasks": {"test": {"commands": [["false"]]}},
            "runtime": {"copies": ["copy"]},
        }
        result = consumer_contract.check(self.root, self.release, baseline)
        self.assertTrue(result["baseline_equal"])
        baseline["tasks"]["test"]["commands"] = [["true"]]
        with self.assertRaisesRegex(ValueError, "baseline"):
            consumer_contract.check(self.root, self.release, baseline)

    def test_corrupt_bundle_rejected(self):
        (self.root / "bundle.tar.gz").write_bytes(b"different")
        with self.assertRaisesRegex(ValueError, "archive"):
            consumer_contract.check(self.root, self.release)

    def test_copy_mode_drift_rejected(self):
        (self.root / "copy/scripts/chainman.sh").chmod(0o644)
        with self.assertRaisesRegex(ValueError, "copy differs"):
            consumer_contract.check(self.root, self.release)

    def test_checkout_and_generated_permissions_preserve_copy_identity(self):
        for root_modes, copy_modes in (
            ((0o775, 0o664), (0o755, 0o644)),
            ((0o700, 0o600), (0o555, 0o444)),
        ):
            with self.subTest(root=root_modes, copy=copy_modes):
                for directory, modes in (
                    (self.root, root_modes),
                    (self.root / "copy", copy_modes),
                ):
                    for name in (
                        "chainman.lock",
                        "bundle.tar.gz",
                        "scripts/chainman.sh",
                        "scripts/chainman-fetch.nix",
                    ):
                        (directory / name).chmod(
                            modes[0] if name.endswith(".sh") else modes[1]
                        )
                self.assertTrue(
                    consumer_contract.check(self.root, self.release)["valid"]
                )

    def test_bootstrap_drift_rejected(self):
        (self.root / "scripts/chainman.sh").write_text("exit 0\n")
        with self.assertRaisesRegex(ValueError, "bootstrap"):
            consumer_contract.check(self.root, self.release)

    def test_symlink_copy_is_rejected(self):
        target = self.root / "copy/chainman.lock"
        target.unlink()
        target.symlink_to(self.root / "chainman.lock")
        with self.assertRaises(ValueError):
            consumer_contract.check(self.root, self.release)


if __name__ == "__main__":
    unittest.main()
