"""Published consumer examples must match the actual entrypoint and local guides."""

from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configuration

ROOT = Path(__file__).resolve().parents[1]


class DocumentationTests(unittest.TestCase):
    def test_readme_contains_the_complete_current_bootstrap(self):
        readme = (ROOT / "README.md").read_text()
        blocks = re.findall(r"```just\n(.*?)```", readme, re.S)
        self.assertIn((ROOT / "bootstrap/chainman.just").read_text(), blocks)
        self.assertIn("Git", readme)
        self.assertIn("git ls-remote --exit-code", readme)

    def test_readme_configuration_is_schema_three_and_routes_existing_workflow(self):
        readme = (ROOT / "README.md").read_text()
        blocks = re.findall(r"```toml\n(.*?)```", readme, re.S)
        self.assertEqual(len(blocks), 1)
        config = tomllib.loads(blocks[0])
        compiled, _ = configuration.compile(config)
        self.assertEqual(config["schema"], 3)
        self.assertEqual(compiled["profiles"]["default"]["flake"], ".#default")
        self.assertEqual(compiled["tasks"]["check"]["commands"], [["just", "check"]])

    def test_local_guide_links_and_anchors_resolve(self):
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]:
            body = path.read_text()
            for target in re.findall(r"\]\(([^\s)]+)\)", body):
                if "://" in target or target.startswith("mailto:"):
                    continue
                name, _, anchor = unquote(target).partition("#")
                resolved = (path.parent / name).resolve() if name else path
                with self.subTest(document=str(path.relative_to(ROOT)), target=target):
                    self.assertTrue(resolved.exists(), target)
                    if anchor and resolved.is_file() and resolved.suffix == ".md":
                        headings = re.findall(
                            r"^#+\s+(.+)$", resolved.read_text(), re.M
                        )
                        slugs = [
                            re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                            for heading in headings
                        ]
                        self.assertIn(anchor, slugs)


if __name__ == "__main__":
    unittest.main()
