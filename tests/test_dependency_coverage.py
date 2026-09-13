"""Dependency coverage follows declared inputs without running their tools."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dependency_reports
import updates


class CoverageTests(unittest.TestCase):
    def test_sdk_source_and_output_pins_are_both_reported_as_managed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "sdk.json").write_text("{}")
            (root / "package.json").write_text("{}")
            (root / "chainman.toml").write_text("""schema=3
[updates.adapters.sdk]
adapter="toolchain"
[[updates.adapters.sdk.tools]]
provider="npm"
name="example-sdk"
source_pin={file="sdk.json",pointer=["sdk"]}
pins=[{file="package.json",pointer=["engines","example"]}]
""")
            updates.git(root, "init")
            updates.git(root, "add", ".")
            before = {
                name: (root / name).read_bytes()
                for name in ("sdk.json", "package.json")
            }
            with patch("source_toolchain.probe") as probe:
                result = dependency_reports.coverage(root)
                probe.assert_not_called()
            self.assertTrue(result["complete"])
            self.assertEqual(
                {row["path"]: row["adapters"] for row in result["inputs"]},
                {"sdk.json": ["sdk"], "package.json": ["sdk"]},
            )
            self.assertEqual(
                before, {name: (root / name).read_bytes() for name in before}
            )
            self.assertEqual(json.loads(json.dumps(result)), result)

    def test_workspace_discovery_preserves_exclusions_and_explicit_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for directory in ("packages/a", "packages/fixture", "extra"):
                (root / directory).mkdir(parents=True)
                (root / directory / "package.json").write_text("{}")
            (root / "package.json").write_text(
                json.dumps(
                    {"workspaces": {"packages": ["packages/*", "!packages/fixture"]}}
                )
            )
            self.assertEqual(
                dependency_reports.inputs(
                    root, {"adapter": "javascript", "manager": "npm"}
                ),
                {"package.json", "packages/a/package.json"},
            )
            self.assertEqual(
                dependency_reports.inputs(
                    root,
                    {
                        "adapter": "javascript",
                        "manager": "npm",
                        "manifests": ["extra/package.json"],
                    },
                ),
                {"package.json", "extra/package.json"},
            )


if __name__ == "__main__":
    unittest.main()
