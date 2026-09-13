from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services
import service_endpoints
import project_environment


class ServiceNetworkTests(unittest.TestCase):
    def test_addresses_use_published_host_ports_and_stable_container_dns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = {
                "services": {
                    "database": {
                        "scope": "repository",
                        "container": {"ports": ["127.0.0.1:15432:5432"]},
                    },
                    "api": {"command": ["true"]},
                    "frontend": {"network_service": "api"},
                }
            }
            with patch.object(service_endpoints.tc, "config", return_value=cfg):
                value = {
                    "DATABASE_URL": "postgresql://test@{service:database:5432}/test"
                }
                host = project_environment.expand(value, root, {})
                container = project_environment.expand(
                    value, root, {"CHAINMAN_MODE": "container-nix"}
                )
                self.assertEqual(
                    host["DATABASE_URL"], "postgresql://test@127.0.0.1:15432/test"
                )
                alias = service_endpoints.alias(root, "database")
                self.assertEqual(
                    container["DATABASE_URL"], f"postgresql://test@{alias}:5432/test"
                )
                self.assertEqual(
                    alias, service_endpoints.alias(root / "other-worktree", "database")
                )
                self.assertNotEqual(
                    service_endpoints.alias(root, "api"),
                    service_endpoints.alias(root / "other-worktree", "api"),
                )
                self.assertEqual(
                    service_endpoints.alias(root, "frontend"),
                    service_endpoints.alias(root, "api"),
                )
                self.assertEqual(
                    service_endpoints.address(root, "api", "8080", False),
                    "127.0.0.1:8080",
                )
                self.assertEqual(
                    project_environment.expand(
                        value,
                        root,
                        {
                            "CHAINMAN_MODE": "container-nix",
                            "CHAINMAN_CONTAINER_NETWORK_MODE": "host",
                        },
                    ),
                    host,
                )

    def test_invalid_endpoints_fail_without_guessing_addresses(self):
        root = Path("/fixture")
        cfg = {
            "services": {
                "db": {"container": {"ports": ["0.0.0.0:15432:5432"]}},
                "cycle": {"network_service": "cycle"},
            }
        }
        with patch.object(service_endpoints.tc, "config", return_value=cfg):
            for name, port in (
                ("db", "0"),
                ("db", "65536"),
                ("db", "5432"),
                ("missing", "5432"),
                ("cycle", "80"),
            ):
                with self.subTest(name=name, port=port), self.assertRaises(ValueError):
                    service_endpoints.address(root, name, port, False)
            cfg["services"]["db"]["container"]["ports"] = [
                "127.0.0.1:15432:5432",
                "127.0.0.1:25432:5432/tcp",
            ]
            with self.assertRaises(ValueError):
                service_endpoints.address(root, "db", "5432", False)
            for value in ("{service:db}", "{service:db:tcp}", "{service:db:0}"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    project_environment.expand({"ENDPOINT": value}, root, {})

    def test_borrowing_requires_an_acquired_stable_owner_and_one_port_publisher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
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
