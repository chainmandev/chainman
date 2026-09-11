from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services


class ServiceNetworkTests(unittest.TestCase):
    def test_borrowing_requires_an_acquired_stable_owner_and_one_port_publisher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "chainman.toml").write_text(
                'schema=2\n[project]\ndefault_profile="host"\n'
            )
            cfg = {
                "project": {"default_profile": "host"},
                "services": {
                    "database": {"command": ["sleep", "60"]},
                    "api": {
                        "command": ["sleep", "60"],
                        "depends_on": ["database"],
                        "network_service": "database",
                    },
                },
                "tasks": {"test": {"services": ["api"], "network_service": "database"}},
            }
            services.declarations(root, cfg)
            for key, changes in (
                ("database", {"restart": "always"}),
                ("api", {"depends_on": []}),
                ("api", {"network_service": "missing"}),
                ("api", {"transport": {"ports": ["127.0.0.1:8000:8000"]}}),
                ("api", {"transport": {"host_access": True}}),
            ):
                changed = deepcopy(cfg)
                changed["services"][key].update(changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    services.declarations(root, changed)
            changed = deepcopy(cfg)
            changed["tasks"]["test"]["services"] = []
            with self.assertRaises(ValueError):
                services.declarations(root, changed)


if __name__ == "__main__":
    unittest.main()
