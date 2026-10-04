"""Candidate-only Pub authority, restoration and native import checks."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import pub_sources as sources
import registry


class PubSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Pub candidate spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.put("chainman.toml", "schema=1\n")
        self.consumer = self.put(
            "consumer/pubspec.yaml",
            "name: consumer\nenvironment: {sdk: '>=3.3.0 <4.0.0'}\n"
            "dependencies:\n  candidate_library: ^1.0.0\n",
        )
        self.source = self.put(
            "owned library/pubspec.yaml",
            "name: candidate_library\nversion: 1.2.3\n"
            "environment: {sdk: '>=3.3.0 <4.0.0'}\n",
        )
        self.spec = {
            "adapter": "flutter",
            "profile": "host",
            "resolve": [["dart", "pub", "get"]],
            "directories": ["consumer"],
            "pub_sources": {"candidate_library": "owned library/pubspec.yaml"},
        }
        self.override = self.root / "consumer/pubspec_overrides.yaml"

    def put(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def transitive(self):
        self.source.write_text(
            self.source.read_text() + "dependencies:\n  owned_codec: ^0.1.0\n"
        )
        codec = self.put(
            "owned codec/pubspec.yaml",
            "name: owned_codec\nversion: 0.1.2\nenvironment: {sdk: '>=3.3.0 <4.0.0'}\n",
        )
        self.spec["pub_sources"]["owned_codec"] = "owned codec/pubspec.yaml"
        return codec

    def test_transitive_binding_requires_reachable_runtime_hosted_declaration(self):
        self.transitive()
        before = self.consumer.read_bytes()
        with sources.bind(self.root, self.spec) as scope:
            self.assertIn("owned_codec", self.override.read_text())
            codec = next(row for row in scope.records() if row["name"] == "owned_codec")
            self.assertEqual(
                codec["declarations"],
                [
                    {
                        "name": "owned_codec",
                        "file": "owned library/pubspec.yaml",
                        "range": "^0.1.0",
                    }
                ],
            )
        self.assertEqual(self.consumer.read_bytes(), before)
        self.assertFalse(self.override.exists())
        self.source.write_text(
            self.source.read_text().replace("dependencies:", "dev_dependencies:")
        )
        with self.assertRaisesRegex(ValueError, "unused"):
            sources.admission(self.root, self.spec, {})

    def test_exact_transitive_override_preserves_authority_bytes_and_mode(self):
        self.transitive()
        original = b"# Existing owned codec authority\ndependency_overrides:\n  owned_codec: {path: ../owned codec}\n"
        self.override.write_bytes(original)
        self.override.chmod(0o600)
        with sources.bind(self.root, self.spec) as scope:
            self.assertIsNotNone(scope)
            self.assertIn("candidate_library", self.override.read_text())
            self.assertEqual(self.override.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.override.read_bytes(), original)
        self.assertEqual(self.override.stat().st_mode & 0o777, 0o600)

    def test_transitive_ranges_policy_and_source_authority_remain_required(self):
        self.transitive()
        for requirement in (
            "^0.2.0",
            "{path: ../owned codec}",
            "{git: https://example.invalid/codec}",
        ):
            with self.subTest(requirement=requirement):
                self.source.write_text(
                    "name: candidate_library\nversion: 1.2.3\ndependencies:\n  owned_codec: "
                    + requirement
                    + "\n"
                )
                with self.assertRaises(ValueError):
                    sources.admission(self.root, self.spec, {})
        self.source.write_text(
            "name: candidate_library\nversion: 1.2.3\ndependencies:\n  owned_codec: ^0.1.0\n"
        )
        for policy in (
            {
                "constraints": {
                    "pub:owned_codec": {
                        "range": "^0.2.0",
                        "reason": "Preserve codec compatibility",
                    }
                }
            },
            {
                "exceptions": [
                    {
                        "package": "pub:owned_codec",
                        "version": "0.2.0",
                        "minimum_safe": "0.2.0",
                        "reason": "Retain safe floor",
                        "advisory": "https://example.invalid/advisory",
                        "expires": "2027-01-01T00:00:00Z",
                    }
                ]
            },
        ):
            with (
                self.subTest(policy=policy),
                self.assertRaisesRegex(ValueError, "requirement or policy"),
            ):
                sources.admission(self.root, self.spec, policy)
        self.override.write_text("dependency_overrides:\n  owned_codec: ^0.1.0\n")
        with self.assertRaisesRegex(ValueError, "declared override"):
            sources.admission(self.root, self.spec, {})

    def test_transitive_groups_do_not_bind_unrelated_consumers(self):
        self.transitive()
        self.put("other/pubspec.yaml", "name: other\n")
        self.spec["directories"].append("other")
        with sources.bind(self.root, self.spec) as scope:
            self.assertEqual(scope.records()[0]["groups"], ["flutter-0"])
            self.assertFalse((self.root / "other/pubspec_overrides.yaml").exists())

    def observed(
        self,
        *,
        version="1.2.3",
        path="../owned library",
        uri="../../owned%20library/",
        source="path",
    ):
        self.put(
            "consumer/pubspec.lock",
            f"packages:\n  candidate_library:\n    source: {source}\n    version: {version}\n"
            f"    description:\n      path: '{path}'\n      relative: true\n",
        )
        self.put(
            "consumer/.dart_tool/package_config.json",
            json.dumps(
                {
                    "configVersion": 2,
                    "packages": [
                        {
                            "name": "candidate_library",
                            "rootUri": uri,
                            "packageUri": "lib/",
                        }
                    ],
                }
            ),
        )

    def test_read_rejects_foreign_authority_and_unsupported_resolvers(self):
        for spec in (
            {**self.spec, "adapter": "rust"},
            {**self.spec, "resolve": [["dart", "pub", "upgrade"]]},
            {**self.spec, "pub_sources": {"wrong": "owned library/pubspec.yaml"}},
            {**self.spec, "pub_sources": {"candidate_library": "../pubspec.yaml"}},
            {**self.spec, "pub_sources": {"candidate_library": str(self.source)}},
            {**self.spec, "pub_sources": {"candidate_library": "owned library"}},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                sources.read(self.root, spec)
        (self.root / "alias").symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            sources.read(
                self.root,
                {
                    **self.spec,
                    "pub_sources": {"candidate_library": "alias/pubspec.yaml"},
                },
            )

    def test_requirement_policy_floor_unused_and_existing_override_reject(self):
        for policy in (
            {"constraints": {"pub:candidate_library": "^2.0.0"}},
            {
                "exceptions": [
                    {
                        "package": "pub:candidate_library",
                        "version": "2.0.0",
                        "minimum_safe": "2.0.0",
                        "reason": "Retain the required safe floor.",
                        "advisory": "https://example.invalid/security/1",
                        "expires": "2027-01-01T00:00:00Z",
                    }
                ]
            },
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                with sources.bind(self.root, self.spec, policy):
                    pass
        self.consumer.write_text(
            "name: consumer\ndependencies:\n  candidate_library: ^2.0.0\n"
        )
        with self.assertRaisesRegex(ValueError, "requirement or policy"):
            with sources.bind(self.root, self.spec):
                pass
        self.consumer.write_text("name: consumer\n")
        with self.assertRaisesRegex(ValueError, "unused"):
            with sources.bind(self.root, self.spec):
                pass
        self.consumer.write_text(
            "name: consumer\ndependencies:\n  candidate_library: ^1.0.0\n"
        )
        self.override.write_text("dependency_overrides:\n  candidate_library: ^1.0.0\n")
        with self.assertRaisesRegex(ValueError, "declared override"):
            with sources.bind(self.root, self.spec):
                pass

    def test_scope_keeps_public_manifest_and_restores_existing_bytes_mode(self):
        original = self.consumer.read_bytes()
        body = b"# retained user data\nresolution: null\n"
        self.override.write_bytes(body)
        self.override.chmod(0o600)
        with sources.bind(self.root, self.spec) as scope:
            self.assertEqual(self.consumer.read_bytes(), original)
            self.assertIn("../owned library", self.override.read_text())
            with sources.bind(self.root, self.spec) as nested:
                self.assertIs(nested, scope)
            self.assertIsNotNone(scope)
        self.assertEqual(self.override.read_bytes(), body)
        self.assertEqual(self.override.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.consumer.read_bytes(), original)

    def test_error_exit_removes_only_unchanged_temporary_override(self):
        with self.assertRaisesRegex(RuntimeError, "original failure"):
            with sources.bind(self.root, self.spec):
                raise RuntimeError("original failure")
        self.assertFalse(self.override.exists())
        with self.assertRaisesRegex(ValueError, "changes preserved"):
            with sources.bind(self.root, self.spec):
                self.override.write_text("# concurrent change\n")
        self.assertEqual(self.override.read_text(), "# concurrent change\n")

    def test_materialization_rejects_version_path_registry_and_import_substitution(
        self,
    ):
        for changes in (
            {},
            {"version": "1.2.4"},
            {"path": "../other"},
            {"source": "hosted"},
            {"uri": "../../other/"},
            {"uri": "https://example.invalid/source"},
        ):
            with (
                self.subTest(changes=changes),
                sources.bind(self.root, self.spec) as scope,
            ):
                self.observed(**changes)
                self.assertIsNotNone(scope)
                if changes:
                    with self.assertRaises(ValueError):
                        sources.materialized(self.root, self.spec, scope)
                else:
                    self.assertEqual(
                        sources.materialized(self.root, self.spec, scope),
                        scope.records(),
                    )
        with sources.bind(self.root, self.spec) as scope:
            self.observed()
            self.source.write_text(self.source.read_text() + "# mutation\n")
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                sources.materialized(self.root, self.spec, scope)

    def test_resolver_and_post_hook_audit_keep_candidate_identity_separate(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        original = self.consumer.read_bytes()
        before = native.snapshot(self.root, self.spec)

        def resolve(root, profile, argv, **kwargs):
            self.observed()
            return subprocess.CompletedProcess(argv, 0, stdout="native observation\n")

        with (
            patch.object(registry, "releases") as queried,
            patch.object(native.chainman, "execute", side_effect=resolve),
        ):
            resolution = native.resolve(self.root, self.spec, {}, now)
            self.assertIn("pub_candidates", resolution)
            native.audit(
                self.root, self.spec, {**before, "resolution": resolution}, {}, now
            )
            queried.assert_not_called()
            self.source.write_text(self.source.read_text() + "# post hook\n")
            with self.assertRaisesRegex(ValueError, "selected Pub candidate sources"):
                native.audit(
                    self.root, self.spec, {**before, "resolution": resolution}, {}, now
                )
        self.assertEqual(self.consumer.read_bytes(), original)
        self.assertFalse(self.override.exists())

    def test_prior_source_adapter_changes_are_frozen_at_resolution(self):
        now = datetime(2026, 9, 7, tzinfo=timezone.utc)
        before = native.snapshot(self.root, self.spec)
        self.source.write_text(
            self.source.read_text() + "description: Updated by the owning adapter.\n"
        )

        def execute(root, profile, argv, **kwargs):
            self.observed()
            return subprocess.CompletedProcess(argv, 0, stdout="native observation\n")

        with patch.object(native.chainman, "execute", side_effect=execute):
            resolution = native.resolve(self.root, self.spec, {}, now)
        self.assertNotEqual(
            before["pub_sources"][0]["manifest_sha256"],
            resolution["pub_candidates"][0]["manifest_sha256"],
        )
        native.audit(
            self.root, self.spec, {**before, "resolution": resolution}, {}, now
        )
        # Resume audits re-observe current sources and the complete native graph.
        native.audit(self.root, self.spec, before, {}, now)
        self.source.write_text(
            self.source.read_text().replace("version: 1.2.3", "version: 1.2.4")
        )
        with self.assertRaisesRegex(ValueError, "authority changed"):
            native.audit(self.root, self.spec, before, {}, now)

    def test_missing_duplicate_or_unknown_native_configuration_rejects(self):
        with sources.bind(self.root, self.spec) as scope:
            for change in (
                "missing",
                "duplicate",
                "version",
                "absolute_lock",
                "package_uri",
            ):
                with self.subTest(change=change):
                    self.observed()
                    config = self.root / "consumer/.dart_tool/package_config.json"
                    value = json.loads(config.read_text())
                    if change == "missing":
                        value["packages"] = []
                    elif change == "duplicate":
                        value["packages"] *= 2
                    elif change == "version":
                        value["configVersion"] = 1
                    elif change == "package_uri":
                        value["packages"][0]["packageUri"] = "../other/lib/"
                    else:
                        lock = self.root / "consumer/pubspec.lock"
                        lock.write_text(
                            lock.read_text().replace(
                                "relative: true", "relative: false"
                            )
                        )
                    config.write_text(json.dumps(value))
                    with self.assertRaises(ValueError):
                        sources.materialized(self.root, self.spec, scope)

    def test_all_consumers_are_bound_and_restored_after_partial_failure(self):
        self.put(
            "second/pubspec.yaml",
            self.consumer.read_text().replace("name: consumer", "name: second"),
        )
        self.spec["directories"].append("second")
        second = self.root / "second/pubspec_overrides.yaml"
        with sources.bind(self.root, self.spec) as scope:
            self.assertEqual(len(scope.records()[0]["groups"]), 2)
            self.assertTrue(self.override.exists())
            self.assertTrue(second.exists())
        self.assertFalse(self.override.exists())
        self.assertFalse(second.exists())
        second.write_text(
            "dependency_overrides:\n  candidate_library: {path: ../owned library}\n"
        )
        original = second.read_bytes()
        with sources.bind(self.root, self.spec):
            self.assertTrue(self.override.exists())
            self.assertEqual(second.read_bytes(), original)
        self.assertFalse(self.override.exists())
        self.put("other/pubspec.yaml", self.source.read_text())
        second.write_text(
            "dependency_overrides:\n  candidate_library: {path: ../other}\n"
        )
        original = second.read_bytes()
        with self.assertRaises(ValueError):
            with sources.bind(self.root, self.spec):
                pass
        self.assertFalse(self.override.exists())
        self.assertEqual(second.read_bytes(), original)

    def test_invalid_binding_shape_and_nested_authority_reject(self):
        for value in (None, [], "owned library/pubspec.yaml"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native.specifications(self.root, {**self.spec, "pub_sources": value})
        with sources.bind(self.root, self.spec):
            with self.assertRaisesRegex(ValueError, "changed its authority"):
                with sources.bind(self.root, {**self.spec, "profile": "other"}):
                    pass
        self.assertFalse(self.override.exists())

    @unittest.skipUnless(
        os.environ.get("CHAINMAN_TEST_PUB") == "1", "explicit native Pub profile lane"
    )
    def test_real_dart_resolves_imports_and_frozen_replay_candidate(self):
        self.assertIsNotNone(shutil.which("dart"))
        self.consumer.write_text(
            self.consumer.read_text().replace(">=3.3.0", ">=3.6.0")
            + "workspace: [member]\n"
        )
        member = self.put(
            "consumer/member/pubspec.yaml",
            "name: member\n"
            "environment: {sdk: '>=3.6.0 <4.0.0'}\nresolution: workspace\n"
            "dependencies:\n  candidate_library: ^1.0.0\n",
        )
        original_member = member.read_bytes()
        self.transitive()
        codec = self.root / "owned codec/pubspec.yaml"
        codec.write_text(codec.read_text() + "dependencies:\n  collection: 1.19.1\n")
        self.put(
            "owned codec/lib/owned_codec.dart",
            "const codecIdentity = 'owned-codec-0.1.2';\n",
        )
        self.put(
            "owned library/lib/candidate_library.dart",
            "import 'package:owned_codec/owned_codec.dart';\nconst identity = 'candidate-library-1.2.3/' + codecIdentity;\n",
        )
        main = self.put(
            "consumer/bin/main.dart",
            "import 'package:candidate_library/candidate_library.dart';\nvoid main() { print(identity); }\n",
        )
        original = self.consumer.read_bytes()

        def execute(root, profile, argv, **kwargs):
            return subprocess.run(
                argv,
                cwd=kwargs["cwd"],
                text=True,
                check=True,
                capture_output=True,
                timeout=120,
            )

        root_only = {
            **self.spec,
            "pub_sources": {"candidate_library": "owned library/pubspec.yaml"},
        }
        with sources.bind(self.root, root_only):
            missing = subprocess.run(
                ["dart", "pub", "get"],
                cwd=self.consumer.parent,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("owned_codec", missing.stdout + missing.stderr)
        existing_override = b"# Preserve codec source\ndependency_overrides:\n  owned_codec: {path: ../owned codec}\n"
        self.override.write_bytes(existing_override)
        self.override.chmod(0o600)
        before = native.snapshot(self.root, self.spec)
        with (
            patch.object(native.chainman, "execute", side_effect=execute),
            patch.object(registry, "releases", wraps=registry.releases) as queried,
        ):
            resolution = native.resolve(
                self.root, self.spec, {}, datetime.now(timezone.utc)
            )
            native.audit(
                self.root,
                self.spec,
                {**before, "resolution": resolution},
                {},
                datetime.now(timezone.utc),
            )
            names = {
                call.args[1] for call in queried.call_args_list if call.args[0] == "pub"
            }
            self.assertIn("collection", names)
            self.assertNotIn("candidate_library", names)
            self.assertNotIn("owned_codec", names)
        with sources.bind(self.root, self.spec) as scope:
            original_codec = codec.read_bytes()
            codec.write_bytes(original_codec + b"# post-resolution mutation\n")
            with self.assertRaisesRegex(ValueError, "manifest changed"):
                sources.materialized(self.root, self.spec, scope)
            codec.write_bytes(original_codec)
            frozen = subprocess.run(
                ["dart", "pub", "get", "--offline", "--enforce-lockfile"],
                cwd=self.consumer.parent,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(frozen.returncode, 0, frozen.stdout + frozen.stderr)
            run = subprocess.run(
                ["dart", "run", str(main)],
                cwd=self.consumer.parent,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(
                run.stdout.strip(), "candidate-library-1.2.3/owned-codec-0.1.2"
            )
        self.assertEqual(self.consumer.read_bytes(), original)
        self.assertEqual(member.read_bytes(), original_member)
        self.assertEqual(self.override.read_bytes(), existing_override)
        self.assertEqual(self.override.stat().st_mode & 0o777, 0o600)
