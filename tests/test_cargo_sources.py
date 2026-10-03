"""Candidate source identity and project path admission, without registry waivers."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cargo_sources as sources


class CargoSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="candidate source spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "owned crate").mkdir()
        self.manifest = self.root / "owned crate/Cargo.toml"
        self.manifest.write_text('[package]\nname="local_pkg"\nversion="1.2.3"\n')
        self.spec = {
            "adapter": "rust",
            "cargo_sources": {"local_pkg": "owned crate/Cargo.toml"},
        }

    def test_version_and_native_config_keep_paths_literal(self):
        bindings = sources.read(self.root, self.spec)
        self.assertEqual(bindings["local_pkg"].version, "1.2.3")
        flags = sources.arguments(self.root, bindings)
        self.assertEqual(flags[0], "--config")
        self.assertEqual(
            json.loads(flags[1].partition("=")[2]), str(self.manifest.parent)
        )

    def test_inherited_version_retains_workspace_owner(self):
        (self.root / "Cargo.toml").write_text(
            '[workspace]\nmembers=["owned crate"]\n[workspace.package]\nversion="2.3.4"\n'
        )
        self.manifest.write_text(
            '[package]\nname="local_pkg"\nversion.workspace=true\n'
        )
        binding = sources.read(self.root, self.spec)["local_pkg"]
        self.assertEqual(
            (binding.version, binding.version_manifest), ("2.3.4", "Cargo.toml")
        )

    def test_bad_adapters_resolvers_names_and_paths_are_rejected(self):
        for spec in (
            {**self.spec, "adapter": "python"},
            {**self.spec, "resolve": [["cargo", "update", "--workspace"]]},
            {
                **self.spec,
                "cargo_sources": {"private?token=secret": "owned crate/Cargo.toml"},
            },
            {**self.spec, "cargo_sources": {"local_pkg": str(self.manifest)}},
            {**self.spec, "cargo_sources": {"local_pkg": "../Cargo.toml"}},
            {**self.spec, "cargo_sources": {"local_pkg": ".git/Cargo.toml"}},
            {**self.spec, "cargo_sources": {"local_pkg": "owned crate"}},
            {**self.spec, "cargo_sources": {"another_pkg": "owned crate/Cargo.toml"}},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                sources.read(self.root, spec)
        (self.root / "alias").symlink_to(self.manifest.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            sources.read(
                self.root,
                {**self.spec, "cargo_sources": {"local_pkg": "alias/Cargo.toml"}},
            )

    def test_native_identity_cannot_fall_back_to_registry_or_another_source(self):
        bindings = sources.read(self.root, self.spec)
        package = {
            "name": "local_pkg",
            "version": "1.2.3",
            "source": None,
            "manifest_path": str(self.manifest),
        }
        self.assertEqual(
            sources.materialized(
                self.root, bindings, {"packages": [package]}, {"local_pkg"}
            ),
            [
                {
                    "name": "local_pkg",
                    "version": "1.2.3",
                    "manifest": "owned crate/Cargo.toml",
                }
            ],
        )
        for packages in (
            [],
            [package, package],
            [
                {
                    **package,
                    "source": "registry+https://github.com/rust-lang/crates.io-index",
                }
            ],
            [{**package, "version": "1.2.4"}],
            [{**package, "manifest_path": str(self.root / "other/Cargo.toml")}],
        ):
            with self.subTest(packages=packages), self.assertRaises(ValueError):
                sources.materialized(
                    self.root, bindings, {"packages": packages}, {"local_pkg"}
                )
        self.assertEqual(
            sources.materialized(self.root, bindings, {"packages": []}, set()), []
        )


class CargoSourceIntegrationTests(CargoSourceTests):
    def setUp(self):
        super().setUp()
        (self.root / "chainman.toml").write_text("schema=1\n")
        (self.root / "consumer/src").mkdir(parents=True)
        (self.root / "owned crate/src").mkdir()
        self.public = self.root / "consumer/Cargo.toml"
        self.public.write_text(
            '[package]\nname="consumer"\nversion="0.1.0"\n[dependencies]\nlocal_pkg="1.2.3"\n'
        )
        (self.root / "consumer/src/lib.rs").write_text("// consumer\n")
        self.source = self.root / "owned crate/src/lib.rs"
        self.source.write_text("// bound source\n")
        self.spec = {**self.spec, "directories": ["consumer"]}

    def test_source_selection_never_requests_registry_age_or_publication(self):
        from datetime import datetime, timezone
        from unittest.mock import patch
        import ecosystem_updates as native

        specs = native.specifications(self.root, self.spec)
        pin = native.pins(self.root, self.spec, specs)[0]
        original = self.public.read_bytes()
        with patch.object(native.registry, "releases") as releases:
            self.assertIsNone(
                native.choose(self.root, pin, self.spec, {}, datetime.now(timezone.utc))
            )
        releases.assert_not_called()
        self.assertEqual(self.public.read_bytes(), original)
        self.public.write_text(
            original.decode().replace('local_pkg="1.2.3"', 'local_pkg="2.0.0"')
        )
        with (
            patch.object(native.registry, "releases") as releases,
            self.assertRaisesRegex(ValueError, "requirement or policy"),
        ):
            native.choose(self.root, pin, self.spec, {}, datetime.now(timezone.utc))
        releases.assert_not_called()

    def test_bound_sources_are_guarded_in_non_git_projects(self):
        import ecosystem_updates as native

        specs = native.specifications(self.root, self.spec)
        before = native.cargo_input_state(self.root, specs, set())
        self.source.write_text("// changed source\n")
        self.assertNotEqual(native.cargo_input_state(self.root, specs, set()), before)

    def test_failed_native_inspection_preserves_status_and_changed_source(self):
        import subprocess
        from unittest.mock import patch
        import ecosystem_updates as native

        specs = native.specifications(self.root, self.spec)
        failure = subprocess.CalledProcessError(
            23, ["cargo", "metadata"], output="original native diagnostic"
        )

        def execute(*args, **kwargs):
            self.source.write_text("// retained unexpected write\n")
            raise failure

        with (
            patch.object(native.chainman, "execute", side_effect=execute),
            self.assertRaises(subprocess.CalledProcessError) as caught,
        ):
            native.cargo_candidate_identities(self.root, self.spec, specs)
        self.assertIs(caught.exception, failure)
        self.assertEqual(caught.exception.returncode, 23)
        self.assertIn("changes preserved", " ".join(failure.__notes__))
        self.assertEqual(self.source.read_text(), "// retained unexpected write\n")

    def test_successful_native_inspection_cannot_write_inputs_or_locks(self):
        import subprocess
        from unittest.mock import patch
        import ecosystem_updates as native

        specs = native.specifications(self.root, self.spec)

        def execute(*args, **kwargs):
            (self.root / "consumer/Cargo.lock").write_text(
                "unexpected resolver lock write\n"
            )
            return subprocess.CompletedProcess(["cargo", "metadata"], 0, stdout="{}")

        with (
            patch.object(native.chainman, "execute", side_effect=execute),
            self.assertRaisesRegex(ValueError, "changes preserved"),
        ):
            native.cargo_candidate_identities(self.root, self.spec, specs)
        self.assertEqual(
            (self.root / "consumer/Cargo.lock").read_text(),
            "unexpected resolver lock write\n",
        )

    def test_registry_fallback_and_unused_bindings_fail_native_admission(self):
        import subprocess
        from unittest.mock import patch
        import ecosystem_updates as native

        specs = native.specifications(self.root, self.spec)
        package = {
            "name": "local_pkg",
            "version": "1.2.3",
            "manifest_path": str(self.manifest),
            "source": "registry+https://github.com/rust-lang/crates.io-index",
        }
        for document in ({"packages": [package]}, {"packages": []}):
            with (
                patch.object(
                    native.chainman,
                    "execute",
                    return_value=subprocess.CompletedProcess(
                        ["cargo", "metadata"], 0, stdout=json.dumps(document)
                    ),
                ),
                self.assertRaises(ValueError),
            ):
                native.cargo_candidate_identities(self.root, self.spec, specs)

    def test_registry_transitives_still_require_artifact_identity_and_age_repair(self):
        from datetime import datetime, timedelta, timezone
        import hashlib
        import subprocess
        from unittest.mock import patch
        import ecosystem_updates as native
        import registry

        now = datetime(2026, 10, 2, tzinfo=timezone.utc)
        releases = []
        for version, days in [("1.0.0", 60), ("1.1.0", 1)]:
            date = now - timedelta(days=days)
            digest = hashlib.sha256(version.encode()).hexdigest()
            releases.append(
                registry.Release(
                    version,
                    date,
                    artifacts=(
                        registry.Artifact(
                            "https://crates.io/api/v1/crates/leaf/"
                            + version
                            + "/download",
                            "sha256:" + digest,
                            date,
                        ),
                    ),
                )
            )
        metadata = {
            "packages": [
                {
                    "name": "local_pkg",
                    "version": "1.2.3",
                    "source": None,
                    "manifest_path": str(self.manifest),
                }
            ]
        }
        self.manifest.write_text(
            self.manifest.read_text() + '[dependencies]\nleaf="1"\n'
        )
        calls = []
        wrong_digest = False

        def execute(root, profile, argv, **kwargs):
            self.assertIn("--config", argv)
            self.assertIn(
                "patch.crates-io.local_pkg.path="
                + json.dumps(str(self.manifest.parent)),
                argv,
            )
            if argv[:2] == ["cargo", "metadata"]:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(metadata))
            self.assertEqual(argv[:2], ["cargo", "update"])
            value = (
                argv[argv.index("--precise") + 1] if "--precise" in argv else "1.1.0"
            )
            calls.append(value)
            digest = (
                "0" * 64
                if wrong_digest
                else next(
                    r.artifacts[0].digest.split(":")[1]
                    for r in releases
                    if r.version == value
                )
            )
            (self.root / "consumer/Cargo.lock").write_text(
                'version=4\n[[package]]\nname="leaf"\nversion="'
                + value
                + '"\nsource="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="'
                + digest
                + '"\n[[package]]\nname="local_pkg"\nversion="1.2.3"\ndependencies=["leaf"]\n'
            )
            return subprocess.CompletedProcess(
                argv, 0, stdout="native resolver fixture\n"
            )

        def registry_releases(provider, name):
            self.assertEqual((provider, name), ("crates", "leaf"))
            return releases

        with (
            patch.object(native.chainman, "execute", side_effect=execute),
            patch.object(registry, "releases", side_effect=registry_releases),
        ):
            result = native.resolve(self.root, self.spec, {}, now)
            self.assertEqual(calls, ["1.1.0", "1.0.0"])
            self.assertEqual(
                result["cargo_identities"]["rust-0"][0][1:3], ["leaf", "1.0.0"]
            )
            (self.root / "consumer/Cargo.lock").unlink()
            wrong_digest = True
            with self.assertRaisesRegex(ValueError, "absent from registry evidence"):
                native.resolve(self.root, self.spec, {}, now)
            self.assertFalse((self.root / "consumer/Cargo.lock").exists())

    def test_failed_inspection_with_missing_manifest_preserves_native_status(self):
        import subprocess
        from unittest.mock import patch
        import ecosystem_updates as native

        specs = native.specifications(self.root, self.spec)
        failure = subprocess.CalledProcessError(
            23, ["cargo", "metadata"], output="native missing-manifest diagnostic"
        )

        def execute(*args, **kwargs):
            self.public.unlink()
            raise failure

        with (
            patch.object(native.chainman, "execute", side_effect=execute),
            self.assertRaises(subprocess.CalledProcessError) as caught,
        ):
            native.cargo_candidate_identities(self.root, self.spec, specs)
        self.assertIs(caught.exception, failure)
        self.assertIn("could not recheck", " ".join(failure.__notes__))
        self.assertFalse(self.public.exists())

    def test_workspace_version_inheritance_requires_literal_boolean_true(self):
        (self.root / "Cargo.toml").write_text(
            '[workspace]\n[workspace.package]\nversion="1.2.3"\n'
        )
        for raw in ("1", "1.0", "false", '"true"'):
            self.manifest.write_text(
                '[package]\nname="local_pkg"\nversion.workspace=' + raw + "\n"
            )
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                sources.read(self.root, self.spec)

    def test_explicit_pin_does_not_require_another_workspaces_source(self):
        import subprocess
        from unittest.mock import patch
        import ecosystem_updates as native

        (self.root / "other/src").mkdir(parents=True)
        (self.root / "other/src/lib.rs").write_text("// independent workspace\n")
        (self.root / "other/Cargo.toml").write_text(
            '[package]\nname="other"\nversion="0.1.0"\n'
        )
        (self.root / "config.toml").write_text('dependency="1.2.3"\n')
        spec = {
            **self.spec,
            "directories": ["consumer", "other"],
            "pins": [
                {
                    "provider": "crates",
                    "name": "local_pkg",
                    "file": "config.toml",
                    "pointer": ["dependency"],
                }
            ],
        }
        specs = native.specifications(self.root, spec)

        def execute(*args, **kwargs):
            packages = (
                [
                    {
                        "name": "local_pkg",
                        "version": "1.2.3",
                        "source": None,
                        "manifest_path": str(self.manifest),
                    }
                ]
                if kwargs["cwd"] == self.public.parent
                else []
            )
            return subprocess.CompletedProcess(
                ["cargo", "metadata"], 0, stdout=json.dumps({"packages": packages})
            )

        with patch.object(native.chainman, "execute", side_effect=execute):
            result = native.cargo_candidate_identities(self.root, spec, specs)
        self.assertEqual(result["rust-1"], [])
        self.assertEqual(len(result["rust-0"]), 1)
