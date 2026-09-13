"""Real Cargo updates with an isolated crates.io source-replacement fixture."""

from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import registry
import toolchain as tc


@unittest.skipUnless(os.environ.get("CHAINMAN_TEST_CARGO") == "1", "run just rust-test")
class NativeCargoTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(shutil.which("cargo"), "rust-test requires pinned Cargo")
        temporary = tempfile.TemporaryDirectory(prefix="chainman cargo fixture ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.files, self.index, self.releases, self.calls = {}, {}, {}, []
        self.now = datetime(2026, 8, 1, tzinfo=timezone.utc)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = fixture.files.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"
        self.files["/config.json"] = json.dumps(
            {"dl": self.url + "/crates/{crate}/{version}/download"}
        ).encode()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CARGO_", "RUSTUP_"))
        }
        self.env.update(
            CARGO_HOME=str(self.base / "cargo-home"),
            CARGO_NET_RETRY="0",
            CARGO_HTTP_TIMEOUT="5",
        )
        self.put(
            ".cargo/config.toml",
            '[source.crates-io]\nreplace-with="fixture"\n[source.fixture]\nregistry="sparse+'
            + self.url
            + '/"\n',
        )
        self.put("chainman.toml", "schema=1\n")
        self.manifest = self.put(
            "Cargo.toml",
            '# public comment\n[package]\nname="neutral-app"\nversion="0.1.0"\nedition="2021"\n[dependencies]\nalias={package="neutral-parent",version="=1.0.0"}\n',
        )
        self.source = self.put("src/lib.rs", "// unchanged application source\n")
        self.lock = self.root / "Cargo.lock"
        self.spec = {"adapter": "rust", "mode": "compatible"}
        self.release("neutral-parent", "1.0.0", 90, {"neutral-leaf": "^1.0.0"})
        self.release("neutral-leaf", "1.0.0", 90)
        self.cargo("update")
        self.manifest.write_text(
            self.manifest.read_text().replace('"=1.0.0"', '"^1.0.0"')
        )
        self.manifest.chmod(0o640)
        self.lock.chmod(0o640)
        self.original_lock = native.cargo_file_state(self.root, "Cargo.lock")
        self.release("neutral-parent", "1.2.0", 60, {"neutral-leaf": "^1.0.0"})
        self.release("neutral-parent", "1.2.1", 1, {"neutral-leaf": "^1.0.0"})
        self.release("neutral-leaf", "1.5.0", 60)
        self.release("neutral-leaf", "1.6.0", 1)
        self.release("neutral-leaf", "2.0.0", 60)

    def put(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def release(self, name, value, days, dependencies=None):
        dependencies = dependencies or {}
        manifest = (
            f'[package]\nname="{name}"\nversion="{value}"\nedition="2021"\n[dependencies]\n'
            + "".join(f'{key}="{bound}"\n' for key, bound in dependencies.items())
        )
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for path, body in {
                "Cargo.toml": manifest.encode(),
                "src/lib.rs": b"// neutral fixture\n",
            }.items():
                member = tarfile.TarInfo(f"{name}-{value}/{path}")
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
        body = buffer.getvalue()
        digest = hashlib.sha256(body).hexdigest()
        self.files[f"/crates/{name}/{value}/download"] = body
        entry = {
            "name": name,
            "vers": value,
            "deps": [
                {
                    "name": key,
                    "req": bound,
                    "features": [],
                    "optional": False,
                    "default_features": True,
                    "target": None,
                    "kind": "normal",
                }
                for key, bound in dependencies.items()
            ],
            "cksum": digest,
            "features": {},
            "yanked": False,
        }
        self.index.setdefault(name, []).append(entry)
        self.files[f"/{name[:2]}/{name[2:4]}/{name}"] = "\n".join(
            json.dumps(item) for item in self.index[name]
        ).encode()
        published = self.now - timedelta(days=days)
        self.releases.setdefault(name, []).append(
            registry.Release(
                value,
                published,
                artifacts=(
                    registry.Artifact(
                        f"https://crates.io/api/v1/crates/{name}/{value}/download",
                        "sha256:" + digest,
                        published,
                    ),
                ),
            )
        )

    def cargo(self, *arguments):
        return tc.managed_run(
            ["cargo", *arguments],
            cwd=self.root,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
            timeout=45,
        )

    def execute(self, root, profile, argv, **kwargs):
        self.assertEqual((root, profile, kwargs["cwd"]), (self.root, "rust", self.root))
        self.calls.append((list(argv), self.manifest.read_bytes()))
        return self.cargo(*argv[1:])

    def resolve(self):
        # Substitute transport/evidence only; every solver command and resulting
        # lock goes through the pinned Cargo binary and production adapter.
        with (
            patch.object(native.chainman, "execute", side_effect=self.execute),
            patch.object(
                registry,
                "releases",
                side_effect=lambda provider, name: self.releases[name],
            ),
        ):
            return native.resolve(self.root, self.spec, {}, self.now)

    def assert_public_manifest(self, value="1.2.0"):
        self.assertIn(f'version="{value}"', self.manifest.read_text())
        self.assertNotIn(f'version="={value}"', self.manifest.read_text())
        self.assertTrue(self.manifest.read_text().startswith("# public comment\n"))
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.source.read_bytes(), b"// unchanged application source\n")

    def test_real_direct_selection_transitive_repair_and_restored_locked_graph(self):
        result = self.resolve()
        packages = {
            item["name"]: item["version"]
            for item in tomllib.loads(self.lock.read_text())["package"]
        }
        self.assertEqual(
            packages,
            {
                "neutral-app": "0.1.0",
                "neutral-parent": "1.2.0",
                "neutral-leaf": "1.5.0",
            },
        )
        self.assertEqual(
            [argv[-1] for argv, _ in self.calls], ["update", "2.0.0", "1.5.0"]
        )
        self.assertTrue(all(b'version="=1.2.0"' in body for _, body in self.calls))
        self.assert_public_manifest()
        before = native.cargo_file_state(self.root, "Cargo.lock")
        graph = json.loads(
            self.cargo(
                "metadata", "--quiet", "--locked", "--format-version", "1"
            ).stdout
        )
        self.assertEqual({p["name"]: p["version"] for p in graph["packages"]}, packages)
        self.assertEqual(native.cargo_file_state(self.root, "Cargo.lock"), before)
        self.assertEqual(
            {tuple(item[:3]) for item in result["cargo_identities"]["rust-0"]},
            {
                ("crates", "neutral-parent", "1.2.0"),
                ("crates", "neutral-leaf", "1.5.0"),
            },
        )

    def test_real_unsatisfiable_selection_restores_lock_and_public_manifest(self):
        # The registry evidence is valid, but the selected release cannot resolve.
        self.release("neutral-parent", "1.3.0", 60, {"neutral-leaf": "=9.0.0"})
        with self.assertRaises(subprocess.CalledProcessError):
            self.resolve()
        self.assertEqual(len(self.calls), 1)
        self.assert_public_manifest("1.3.0")
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

    def test_real_workspace_inherited_alias_is_selected_once_and_members_are_preserved(
        self,
    ):
        self.manifest.write_text(
            '# public comment\n[workspace]\nmembers=["member-a", "member-b"]\nresolver="2"\n'
            '[workspace.dependencies]\nalias={package="neutral-parent",version="^1.0.0"}\n'
        )
        members = {}
        for name, section in (
            ("member-a", "dependencies"),
            ("member-b", "dev-dependencies"),
        ):
            path = self.put(
                name + "/Cargo.toml",
                f'# inherited alias\n[package]\nname="{name}"\nversion="0.1.0"\nedition="2021"\n'
                f"[{section}]\nalias.workspace=true\n",
            )
            path.chmod(0o640)
            members[path] = (path.read_bytes(), path.stat().st_mode)
            self.put(name + "/src/lib.rs", "// unchanged member source\n")
        # Refresh only local package membership, retaining the old registry graph.
        self.cargo("update", "--workspace")
        self.assertEqual(
            {
                p["name"]: p["version"]
                for p in tomllib.loads(self.lock.read_text())["package"]
                if p.get("source")
            },
            {"neutral-parent": "1.0.0", "neutral-leaf": "1.0.0"},
        )
        result = self.resolve()
        self.assert_public_manifest()
        self.assertTrue(all(b'version="=1.2.0"' in body for _, body in self.calls))
        before = native.cargo_file_state(self.root, "Cargo.lock")
        graph = json.loads(
            self.cargo(
                "metadata", "--quiet", "--locked", "--format-version", "1"
            ).stdout
        )
        self.assertEqual(
            {p["name"]: p["version"] for p in graph["packages"]},
            {
                "member-a": "0.1.0",
                "member-b": "0.1.0",
                "neutral-parent": "1.2.0",
                "neutral-leaf": "1.5.0",
            },
        )
        by_id = {p["id"]: p["name"] for p in graph["packages"]}
        for node in graph["resolve"]["nodes"]:
            if by_id[node["id"]] in ("member-a", "member-b"):
                self.assertEqual(
                    [(d["name"], by_id[d["pkg"]]) for d in node["deps"]],
                    [("alias", "neutral-parent")],
                )
        self.assertEqual(native.cargo_file_state(self.root, "Cargo.lock"), before)
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mode) for path in members},
            members,
        )
        self.assertEqual(
            {tuple(item[:3]) for item in result["cargo_identities"]["rust-0"]},
            {
                ("crates", "neutral-parent", "1.2.0"),
                ("crates", "neutral-leaf", "1.5.0"),
            },
        )

    def test_real_lock_checksum_mismatch_is_rejected_and_restored(self):
        release = self.releases["neutral-parent"][1]
        artifact = release.artifacts[0]
        self.releases["neutral-parent"][1] = registry.Release(
            release.version,
            release.published,
            artifacts=(
                registry.Artifact(
                    artifact.url, "sha256:" + "0" * 64, artifact.published
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "absent from registry evidence"):
            self.resolve()
        self.assertEqual(len(self.calls), 1)
        self.assert_public_manifest()
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )
