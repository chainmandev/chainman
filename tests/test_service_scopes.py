import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import services


class ServiceScopeTests(unittest.TestCase):
    def test_shared_identity_and_inputs_survive_worktree_and_execution_mode_changes(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first, second = base / "first", base / "second"
            first.mkdir()
            subprocess.run(["git", "init", "-q", str(first)], check=True)
            (first / "migration").write_text("schema one")
            subprocess.run(["git", "-C", str(first), "add", "."], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(first),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(first),
                    "worktree",
                    "add",
                    "-q",
                    "--detach",
                    str(second),
                ],
                check=True,
            )
            declaration = {
                "database": {
                    "scope": "repository",
                    "container": {
                        "image": "example.invalid/db@sha256:" + "a" * 64,
                        "volumes": [
                            {
                                "name": "data",
                                "target": "/data",
                                "format": "one",
                                "inputs": ["migration"],
                            }
                        ],
                    },
                }
            }
            a = services.repository_scope(first, base / "user-cache", declaration)
            b = services.repository_scope(second, base / "user-cache", declaration)
            self.assertEqual(a, b)
            self.assertNotEqual(
                a[1],
                services.repository_scope(
                    first, base / "another-user-cache", declaration
                )[1],
            )
            (second / "migration").write_text("schema two")
            c = services.repository_scope(second, base / "user-cache", declaration)
            self.assertEqual(a[:2], c[:2])
            self.assertNotEqual(a[2], c[2])
            self.assertNotEqual(
                services.scope_key(base, first, "host-nix"),
                services.scope_key(base, first, "container-nix"),
            )

    def test_repository_service_rejects_worktree_bindings_and_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = {
                "scope": "repository",
                "container": {"image": "example.invalid/db@sha256:" + "a" * 64},
            }
            for changes in (
                {"environment": {"CUSTOM": "value"}},
                {"setup": ["install"]},
                {
                    "container": dict(
                        original["container"],
                        environment={"PATH_IN_PROJECT": "{root}/data"},
                    )
                },
            ):
                spec = dict(original, **changes)
                with self.assertRaises(ValueError):
                    services.declarations(root, {"services": {"db": spec}})
            local = json.loads(json.dumps(original))
            local.pop("scope")
            with self.assertRaisesRegex(ValueError, "cannot depend"):
                services.declarations(
                    root,
                    {
                        "services": {
                            "db": dict(original, depends_on=["local"]),
                            "local": local,
                        }
                    },
                )


if __name__ == "__main__":
    unittest.main()
