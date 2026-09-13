"""Explicit SDK ownership, fresh-profile ordering, and locked build bootstrap."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from packaging.version import Version

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import manifests
import sdk_versions
import updates
from toolchain import module


class ManifestPointerTests(unittest.TestCase):
    def test_nested_sequence_edits_preserve_serializer_structure(self):
        bodies = {
            "json": json.dumps(
                {"items": [{"version": "1.0.0", "name": "keep"}]}, indent=2
            )
            + "\n",
            "toml": 'items = [{ version = "1.0.0", name = "keep" }] # keep comment\n',
            "yaml": 'items:\n  - version: "1.0.0" # keep comment\n    name: keep\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            for suffix, body in bodies.items():
                with self.subTest(format=suffix):
                    path = Path(temporary).resolve() / ("manifest." + suffix)
                    value, render = manifests.document(path, body=body)
                    self.assertEqual(
                        manifests.lookup(value, ["items", 0, "version"]), "1.0.0"
                    )
                    manifests.assign(value, ["items", 0, "version"], "2.0.0")
                    updated = render()
                    reparsed, _ = manifests.document(path, body=updated)
                    self.assertEqual(
                        manifests.lookup(reparsed, ["items", 0, "version"]), "2.0.0"
                    )
                    self.assertEqual(
                        manifests.lookup(reparsed, ["items", 0, "name"]), "keep"
                    )
                    if suffix != "yaml":
                        self.assertEqual(updated, body.replace("1.0.0", "2.0.0"))
                    else:
                        self.assertRegex(updated, r'"2\.0\.0" +# keep comment')


class SDKTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sdk-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.targets = []
        self.specs = {}
        self.versions = {}

    def write(self, file, body):
        path = self.root / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def target(self, kind, file, body, sdk, **extra):
        profile = sdk_versions.KINDS[kind]
        self.specs.setdefault(profile, {"profile": profile, "update_outputs": []})[
            "update_outputs"
        ].append(file)
        self.versions.setdefault(profile, {})["python" if kind == "ruff" else kind] = (
            Version(sdk)
        )
        self.targets.append({"module": profile, "kind": kind, "files": [file], **extra})
        return self.write(file, body)

    def synchronize(self, *, check=False, selected=None):
        body = "schema=1\n"
        for target in self.targets:
            body += (
                "\n[[targets]]\n"
                + "\n".join(k + "=" + json.dumps(v) for k, v in target.items())
                + "\n"
            )
        self.write("sdk-versions.toml", body)
        with (
            patch.object(
                sdk_versions, "module", side_effect=lambda n, r: self.specs[n]
            ),
            patch.object(
                sdk_versions,
                "probe",
                side_effect=lambda r, spec: self.versions[spec["profile"]],
            ),
        ):
            return sdk_versions.synchronize(
                self.root,
                list(self.specs) if selected is None else selected,
                check=check,
            )

    def test_major_refresh_coordinates_package_manager_and_node_engine(self):
        body = '{"packageManager":"pnpm@11.25.0","engines":{"node":">=26 <27"},"private":true}\n'
        path = self.target("pnpm", "package.json", body, "12.1.0")
        self.target("node", "package.json", body, "27.0.1")
        self.synchronize()
        self.assertEqual(
            json.loads(path.read_text()),
            {
                "packageManager": "pnpm@12.1.0",
                "engines": {"node": ">=27 <28"},
                "private": True,
            },
        )

    def test_explicit_empty_or_malformed_target_files_fail_before_probes(self):
        self.target("go", "go.mod", "module example.invalid/demo\ngo 1.27\n", "1.27.1")
        for files in ([], "go.mod", [""], [3]):
            with self.subTest(files=files):
                self.targets[0]["files"] = files
                with self.assertRaisesRegex(ValueError, "nonempty list"):
                    self.synchronize()

    def test_selected_builtin_requires_targets_or_reasoned_unmanaged_declaration(self):
        self.write("sdk-versions.toml", "schema=1\n")
        with patch.object(sdk_versions, "probe") as probe:
            with self.assertRaisesRegex(ValueError, "no targets"):
                sdk_versions.synchronize(self.root, ["go"])
            self.write(
                "sdk-versions.toml",
                'schema=1\n[unmanaged_modules]\ngo="External compatibility contract owns this SDK declaration"\n',
            )
            self.assertEqual(sdk_versions.synchronize(self.root, ["go"]), {})
            probe.assert_not_called()

    def test_unmanaged_declaration_requires_reason_and_cannot_overlap_targets(self):
        for config in (
            'schema=1\n[unmanaged_modules]\ngo=""\n',
            'schema=1\n[unmanaged_modules]\ngo="External owner"\n[[targets]]\nmodule="go"\nkind="go"\nfiles=["go.mod"]\n',
        ):
            self.write("sdk-versions.toml", config)
            with self.assertRaisesRegex(ValueError, "reasons|both targeted"):
                sdk_versions.synchronize(self.root, ["go"])

    def test_python_runtime_and_ruff_language_target_are_coordinated(self):
        body = '# retain this contract comment\n[project]\nrequires-python=">=3.14"\n[tool.ruff]\ntarget-version="py314"\n'
        path = self.target("python", "pyproject.toml", body, "3.15.2")
        self.target("ruff", "pyproject.toml", body, "3.15.2")
        self.synchronize()
        parsed = tomllib.loads(path.read_text())
        self.assertEqual(parsed["project"]["requires-python"], ">=3.15")
        self.assertEqual(parsed["tool"]["ruff"]["target-version"], "py315")
        self.assertIn("# retain this contract comment", path.read_text())

    def test_go_swift_dart_and_jdk_targets_preserve_unrelated_contracts(self):
        cases = [
            (
                "go",
                "go.mod",
                "module example.test/demo\n\ngo 1.26.2\n",
                "1.27.1",
                "go 1.27.1",
            ),
            (
                "swift",
                "Package.swift",
                "// swift-tools-version: 5.10\nlet floor = .macOS(.v13)\n",
                "6.2.1",
                "swift-tools-version: 6.2",
            ),
            (
                "dart",
                "pubspec.yaml",
                'environment:\n  sdk: ">=3.12.0 <4.0.0"\nname: demo\n',
                "3.13.0",
                ">=3.13.0 <4.0.0",
            ),
            (
                "jdk",
                "build.gradle.kts",
                "kotlin { jvmToolchain(21) }\n",
                "25.0.1",
                "jvmToolchain(25)",
            ),
        ]
        paths = [(self.target(k, f, b, v), expected) for k, f, b, v, expected in cases]
        self.synchronize()
        for path, expected in paths:
            self.assertIn(expected, path.read_text())
        self.assertIn(".macOS(.v13)", (self.root / "Package.swift").read_text())

    def test_identical_targets_preserve_bytes_including_json_spacing(self):
        body = '{ "packageManager" : "pnpm@11.25.0" }\n'
        path = self.target("pnpm", "package.json", body, "11.25.0")
        self.synchronize()
        self.synchronize(check=True)
        self.assertEqual(path.read_text(), body)

    def test_explicit_compatible_hold_preserves_existing_support_range(self):
        body = '[project]\nrequires-python=">=3.10,<3.15"\n'
        path = self.target(
            "python",
            "pyproject.toml",
            body,
            "3.14.7",
            hold_value=">=3.10,<3.15",
            hold_reason="Supported user interpreter range",
        )
        self.synchronize()
        self.assertEqual(path.read_text(), body)
        self.versions["python"]["python"] = Version("3.15.0")
        with self.assertRaisesRegex(ValueError, "hold"):
            self.synchronize()
        self.assertEqual(path.read_text(), body)

    def test_hold_requires_reason_and_exact_manifest_agreement(self):
        self.target(
            "jdk", "build.gradle.kts", "jvmToolchain(21)\n", "21.0.12", hold_value="21"
        )
        with self.assertRaisesRegex(ValueError, "reason"):
            self.synchronize()
        self.targets[0].update(hold_reason="Qualified compiler", hold_value="17")
        with self.assertRaisesRegex(ValueError, "hold"):
            self.synchronize()

    def test_python_floor_hold_cannot_silently_raise_ruff_language_features(self):
        body = '[project]\nrequires-python=">=3.10,<3.15"\n[tool.ruff]\ntarget-version="py310"\n'
        path = self.target(
            "python",
            "pyproject.toml",
            body,
            "3.14.7",
            hold_value=">=3.10,<3.15",
            hold_reason="Published interpreter support",
        )
        self.target("ruff", "pyproject.toml", body, "3.14.7")
        with self.assertRaisesRegex(ValueError, "Ruff.*floor"):
            self.synchronize()
        self.assertEqual(path.read_text(), body)
        self.targets[1].update(
            hold_value="py310",
            hold_reason="Keep syntax within the supported Python floor",
        )
        self.synchronize()
        self.assertEqual(path.read_text(), body)

    def test_custom_range_without_hold_and_sdk_downgrade_fail(self):
        self.target(
            "node", "package.json", '{"engines":{"node":">=22 <27"}}\n', "26.8.1"
        )
        with self.assertRaisesRegex(ValueError, "hold"):
            self.synchronize()
        self.targets.clear()
        self.specs.clear()
        self.target("go", "go.mod", "go 1.28.0\n", "1.27.1")
        with self.assertRaisesRegex(ValueError, "older"):
            self.synchronize()

    def test_unknown_target_missing_probe_and_undeclared_outputs_fail_before_writes(
        self,
    ):
        path = self.target(
            "pnpm", "package.json", '{"packageManager":"pnpm@11.25.0"}\n', "12.0.0"
        )
        before = path.read_text()
        self.target("go", "go.mod", "go 1.27.1\n", "1.28.0")
        self.versions["go"].clear()
        with self.assertRaisesRegex(ValueError, "probe"):
            self.synchronize()
        self.assertEqual(path.read_text(), before)
        self.versions["go"]["go"] = Version("1.28.0")
        self.specs["go"]["update_outputs"] = []
        with self.assertRaisesRegex(ValueError, "output"):
            self.synchronize()
        self.targets[1]["kind"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.synchronize()
        self.assertEqual(path.read_text(), before)

    def test_check_refuses_stale_targets_without_editing(self):
        path = self.target("go", "go.work", "go 1.26.0\n", "1.27.1")
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.synchronize(check=True)
        self.assertEqual(path.read_text(), "go 1.26.0\n")

    def test_recursive_go_module_and_workspace_targets_update_every_declared_file(self):
        paths = [
            self.target("go", file, "go 1.26.0\n", "1.27.1")
            for file in (
                "go/go.work",
                "go/library/go.mod",
                "go/nested/go.work",
                "go/nested/member/go.mod",
            )
        ]
        self.targets = [
            {"module": "go", "kind": "go", "files": ["go/**/go.mod", "go/**/go.work"]}
        ]
        self.synchronize()
        self.assertTrue(all(path.read_text() == "go 1.27.1\n" for path in paths))

    def test_unselected_optional_profiles_are_not_probed(self):
        self.target("go", "go.mod", "go 1.27.1\n", "1.28.0")
        with patch.object(
            sdk_versions, "probe", side_effect=AssertionError("optional SDK loaded")
        ):
            self.assertEqual(self.synchronize(selected=["core"]), {})

    def test_rust_edition_and_inherited_msrv_are_checked_without_rewriting(self):
        self.write(
            "Cargo.toml", '[workspace.package]\nedition="2024"\nrust-version="1.90"\n'
        )
        body = '[package]\nname="demo"\nedition.workspace=true\nrust-version.workspace=true\n'
        path = self.target(
            "rust", "member/Cargo.toml", body, "1.97.1", workspace="Cargo.toml"
        )
        self.synchronize()
        self.assertEqual(path.read_text(), body)
        for version in ("1.84.0", "1.89.0"):
            self.versions["rust"]["rust"] = Version(version)
            with self.assertRaisesRegex(ValueError, "edition|MSRV"):
                self.synchronize()

    def test_fresh_profile_probe_uses_tool_output_away_from_project_pins(self):
        results = [
            type("Result", (), {"stdout": "v27.0.1\n", "stderr": ""})(),
            type("Result", (), {"stdout": "12.1.0\n", "stderr": ""})(),
        ]
        with (
            patch.object(sdk_versions, "environment", return_value={}),
            patch.object(sdk_versions, "managed_run", side_effect=results) as run,
        ):
            actual = sdk_versions.probe(self.root, {"profile": "javascript"})
        self.assertEqual(actual, {"node": Version("27.0.1"), "pnpm": Version("12.1.0")})
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1], "javascript")
            self.assertEqual(call.kwargs["env"]["TOOLCHAIN_FRESH"], "1")
            self.assertNotEqual(Path(call.args[0][6]), self.root)

    def test_missing_prerelease_or_ambiguous_probe_is_not_coerced(self):
        for output in ("", "v27.0.0-rc.1\n", "v27.0.0\nv28.0.0\n"):
            with self.assertRaises(ValueError):
                sdk_versions.extracted(output, r"^v([0-9]+\.[0-9]+\.[0-9]+)$")

    def test_flutter_bundled_dart_must_match_actual_dart_cli(self):
        results = [
            type(
                "Result",
                (),
                {
                    "stdout": '{"dartSdkVersion":"3.13.0","channel":"stable"}',
                    "stderr": "Nix diagnostic\n",
                },
            )(),
            type(
                "Result",
                (),
                {
                    "stdout": "Dart SDK version: 3.12.0 (stable) on linux\n",
                    "stderr": "",
                },
            )(),
        ]
        with (
            patch.object(sdk_versions, "environment", return_value={}),
            patch.object(sdk_versions, "managed_run", side_effect=results),
            self.assertRaisesRegex(ValueError, "bundled"),
        ):
            sdk_versions.probe(self.root, {"profile": "flutter"})

    def test_refresh_precedes_sdk_alignment_and_package_resolution_and_verification(
        self,
    ):
        events = []
        import module_updates
        import source_updates
        from unittest.mock import Mock

        engine = Mock()
        engine.snapshot.side_effect = lambda *a: events.append("baseline") or {}
        engine.resolve.side_effect = lambda *a: events.append("resolve") or {}
        engine.audit.side_effect = lambda *a: events.append("audit")
        spec = {
            "name": "demo",
            "profile": "go",
            "commands": {"resolve": [["go", "mod", "tidy"]]},
        }

        def launch(*args, **kwargs):
            self.assertEqual(args[0][1], "core")
            self.assertEqual(kwargs["env"]["TOOLCHAIN_FRESH"], "1")
            events.append("fresh-core")
            updates.resolve(self.root, datetime.now(timezone.utc), ["demo"])

        with (
            patch.object(updates, "environment", return_value={}),
            patch.object(
                updates, "settings", return_value={"docker": {"enabled": False}}
            ),
            patch.object(source_updates, "snapshot", return_value={}),
            patch.object(
                source_updates, "resolve", side_effect=lambda *a: events.append("nix")
            ),
            patch.object(
                source_updates,
                "audit",
                side_effect=lambda *a: events.append("nix-audit"),
            ),
            patch.object(
                module_updates, "adapters", return_value={"demo": {"adapter": "go"}}
            ),
            patch.object(
                module_updates.dependency_api, "implementation", return_value=engine
            ),
            patch.object(updates, "managed_run", side_effect=launch),
            patch.object(updates, "module", return_value=spec),
            patch.object(updates, "lock_identities", return_value=set()),
            patch.object(
                sdk_versions,
                "synchronize",
                side_effect=lambda *a, **k: events.append(
                    "sdk-check" if k.get("check") else "sdk-update"
                ),
            ),
            patch.object(manifests, "configure_build_dependencies"),
            patch.object(
                manifests,
                "discover",
                side_effect=lambda *a: events.append("packages") or [],
            ),
            patch.object(
                updates,
                "run_commands",
                side_effect=lambda s, action, *a: events.append(action),
            ),
            patch.object(updates, "audit_locks"),
            patch.object(
                updates, "setup", side_effect=lambda *a: events.append("setup")
            ),
        ):
            updates.perform(self.root, datetime.now(timezone.utc), ["demo"])
            updates.verify(self.root, ["demo"])
        self.assertEqual(
            events,
            [
                "nix",
                "fresh-core",
                "baseline",
                "sdk-update",
                "resolve",
                "audit",
                "sdk-check",
                "nix-audit",
                "sdk-check",
                "setup",
                "verify",
            ],
        )


class BuildDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="build-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "modules").mkdir()
        (self.root / "python/member").mkdir(parents=True)
        (self.root / "modules/python.toml").write_text(
            'name="python"\ndirectory="python"\necosystem="pypi"\nbuild_dependency_group="toolchain-build"\ninputs=["python/**/pyproject.toml"]\nupdate_outputs=["python/pyproject.toml","python/member/pyproject.toml"]\n'
        )
        (self.root / "python/pyproject.toml").write_text(
            '[project]\nname="workspace"\nversion="0.1.0"\n'
        )
        (self.root / "python/member/pyproject.toml").write_text(
            '[project]\nname="member"\nversion="0.1.0"\n[build-system]\nrequires=["uv_build==0.12.5"]\nbuild-backend="uv_build"\n'
        )

    def test_build_requires_are_authoritative_pins_and_derived_group_is_not(self):
        manifests.configure_build_dependencies(self.root, ["python"])
        pins = manifests.discover(self.root, ["python"])
        self.assertEqual(
            [(p["name"], p["pointer"]) for p in pins],
            [("uv_build", ["build-system", "requires", 0])],
        )
        root = self.root / "python/pyproject.toml"
        first = root.read_text()
        self.assertEqual(
            tomllib.loads(first)["dependency-groups"]["toolchain-build"],
            ["uv-build==0.12.5"],
        )
        manifests.configure_build_dependencies(self.root, ["python"])
        manifests.configure_build_dependencies(self.root, ["python"], check=True)
        self.assertEqual(root.read_text(), first)

    def test_conflicting_build_requirements_fail_before_group_mutation(self):
        root = self.root / "python/pyproject.toml"
        root.write_text(
            root.read_text() + '[build-system]\nrequires=["uv_build==0.12.6"]\n'
        )
        before = root.read_text()
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            manifests.configure_build_dependencies(
                self.root, ["python"], validate_only=True
            )
        self.assertEqual(root.read_text(), before)

    def test_stale_derived_group_fails_readonly_check(self):
        with self.assertRaisesRegex(ValueError, "disagrees"):
            manifests.configure_build_dependencies(self.root, ["python"], check=True)

    def test_equivalent_build_names_share_one_group_entry(self):
        root = self.root / "python/pyproject.toml"
        root.write_text(
            root.read_text() + '[build-system]\nrequires=["UV-build==0.12.5"]\n'
        )
        manifests.configure_build_dependencies(self.root, ["python"])
        self.assertEqual(
            tomllib.loads(root.read_text())["dependency-groups"]["toolchain-build"],
            ["uv-build==0.12.5"],
        )

    def test_conditional_and_direct_build_sources_are_refused(self):
        path = self.root / "python/member/pyproject.toml"
        for requirement in (
            'uv_build==0.12.5; python_version>="3.14"',
            "uv_build @ https://example.test/backend.whl",
        ):
            path.write_text(
                "[build-system]\nrequires=[" + json.dumps(requirement) + "]\n"
            )
            with self.assertRaisesRegex(ValueError, "unconditional registry"):
                manifests.configure_build_dependencies(self.root, ["python"])

    def test_full_selected_verification_contains_real_build_and_frozen_bootstrap(self):
        root = Path(__file__).resolve().parents[1]
        for name in (
            "core",
            "javascript",
            "rust",
            "python",
            "go",
            "swift",
            "flutter",
            "compose",
        ):
            spec = module(name, root)
            for build in spec["commands"]["build"]:
                self.assertIn(build, spec["commands"]["verify"], name)
            if name != "core":
                self.assertIn("sdk-versions.toml", spec["inputs"], name)
        spec = module("python", root)
        bootstrap, workspace = spec["commands"]["setup"]
        for option in (
            "--only-group",
            "--no-install-workspace",
            "--no-build",
            "--frozen",
        ):
            self.assertIn(option, bootstrap)
        for option in ("--group", "--no-build-isolation", "--frozen"):
            self.assertIn(option, workspace)
        self.assertIn("--force-pep517", spec["commands"]["build"][0][-1])


if __name__ == "__main__":
    unittest.main()
