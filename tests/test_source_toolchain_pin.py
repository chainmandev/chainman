"""Source selection precedes SDK refresh and cannot waive changed artifact age."""

import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import source_toolchain as sdk
import source_toolchain_pin as source

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=60)
YOUNG = NOW - timedelta(days=1)


class SourcePinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="SDK source fixture ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.tool = {
            "provider": "npm",
            "name": "sample-sdk",
            "command": ["sample-sdk", "--version"],
            "version_pattern": r"^(?P<version>\d+\.\d+\.\d+)$",
            "source_pin": {"file": "sources.json", "pointer": ["sdk"]},
            "pins": [
                {"file": "package.json", "pointer": ["sdk"], "value": "{version}"}
            ],
        }
        self.spec = {"adapter": "toolchain", "profile": "native", "tools": [self.tool]}
        self.releases = [self.release("1.0.0", YOUNG)]
        self.write(
            "sources.json",
            {"sdk": source.record(self.tool, self.releases[0]), "other": 1},
        )
        self.write("package.json", {"sdk": "1.0.0"})
        inventory = patch.object(
            registry, "releases", side_effect=lambda *_: self.releases
        )
        inventory.start()
        self.addCleanup(inventory.stop)
        probe = patch.object(
            sdk,
            "probe",
            side_effect=lambda root, spec, tool: source.read(root, tool)["version"],
        )
        probe.start()
        self.addCleanup(probe.stop)

    def write(self, file, value):
        (self.root / file).write_text(json.dumps(value, indent=2) + "\n")

    def release(self, value, published=OLD, digest="a"):
        return registry.Release(
            value,
            OLD,
            artifacts=(
                registry.Artifact(
                    f"https://registry.npmjs.org/sample-sdk/-/sample-sdk-{value}.tgz",
                    "sha512:" + digest * 128,
                    published,
                ),
            ),
        )

    def resolve(self, before=None, policy=None):
        before = sdk.snapshot(self.root, self.spec) if before is None else before
        before["resolution"] = sdk.resolve(
            self.root, self.spec, policy or {}, NOW, before=before
        )
        sdk.audit(self.root, self.spec, before, policy or {}, NOW)
        return before["resolution"]

    def test_unchanged_young_source_is_retained_without_downgrade(self):
        self.releases.append(self.release("0.9.0"))
        self.assertEqual(self.resolve()["changed"], [])

    def test_source_projection_owns_its_pointer_and_write_requires_a_declaration(self):
        pin = source.declaration(self.tool)
        self.assertEqual(pin, {"file": "sources.json", "pointer": ["sdk"]})
        pin["pointer"].append("changed")
        self.assertEqual(self.tool["source_pin"]["pointer"], ["sdk"])
        undeclared = {
            key: value for key, value in self.tool.items() if key != "source_pin"
        }
        before = (self.root / "sources.json").read_bytes()
        record = source.record(self.tool, self.releases[0])
        with self.assertRaisesRegex(ValueError, "requires a declared source pin"):
            source.write(self.root, undeclared, record, record)
        self.assertEqual((self.root / "sources.json").read_bytes(), before)

    def test_mature_major_updates_source_then_refreshes_and_renders(self):
        self.releases.extend([self.release("2.0.0"), self.release("3.0.0", YOUNG)])
        before = sdk.snapshot(self.root, self.spec)
        calls = []

        def refreshed(root, spec, tool):
            calls.append(
                (
                    source.read(root, tool)["version"],
                    sdk.pin_value(root, tool["pins"][0]),
                )
            )
            return calls[-1][0]

        (self.root / "sources.json").chmod(0o640)
        with patch.object(sdk, "probe", side_effect=refreshed):
            result = self.resolve(before)
        self.assertEqual(calls, [("2.0.0", "1.0.0"), ("2.0.0", "2.0.0")])
        self.assertEqual(result["changed"], ["package.json", "sources.json"])
        self.assertEqual(
            stat.S_IMODE((self.root / "sources.json").stat().st_mode), 0o640
        )
        self.assertEqual(
            json.loads((self.root / "sources.json").read_text())["other"], 1
        )

    def test_incomplete_baseline_is_rejected_before_any_source_update(self):
        second = copy.deepcopy(self.tool)
        second["source_pin"]["pointer"] = ["second"]
        second["pins"][0]["pointer"] = ["second"]
        self.spec["tools"].append(second)
        current = source.record(self.tool, self.releases[0])
        self.write("sources.json", {"sdk": current, "second": current})
        self.write("package.json", {"sdk": "1.0.0", "second": "1.0.0"})
        before = sdk.snapshot(self.root, self.spec)
        before["sources"].pop()
        self.releases.append(self.release("2.0.0"))
        original = {
            file: (self.root / file).read_bytes()
            for file in ("sources.json", "package.json")
        }
        with (
            patch.object(source, "write", wraps=source.write) as write,
            self.assertRaisesRegex(ValueError, "source inventory changed"),
        ):
            sdk.resolve(self.root, self.spec, {}, NOW, before=before)
        write.assert_not_called()
        self.assertEqual(
            original, {file: (self.root / file).read_bytes() for file in original}
        )

    def test_changed_young_digest_cannot_retain_baseline_age(self):
        before = sdk.snapshot(self.root, self.spec)
        self.releases = [self.release("1.0.0", YOUNG, "b")]
        with self.assertRaisesRegex(ValueError, "eligible dated"):
            self.resolve(before)

    def test_changed_mature_digest_requires_and_records_new_source(self):
        before = sdk.snapshot(self.root, self.spec)
        self.releases = [self.release("1.0.0", OLD, "b")]
        result = self.resolve(before)
        self.assertEqual(result["changed"], ["sources.json"])
        self.assertEqual(
            result["tools"][0]["source"], source.record(self.tool, self.releases[0])
        )

    def test_actual_binary_must_match_selected_source(self):
        before = sdk.snapshot(self.root, self.spec)
        self.releases.append(self.release("2.0.0"))
        with (
            patch.object(sdk, "probe", return_value="1.0.0"),
            self.assertRaisesRegex(ValueError, "differs"),
        ):
            self.resolve(before)

    def test_compatible_mode_bounds_selection_and_exception_retirement(self):
        self.spec["mode"] = "compatible"
        self.releases.extend([self.release("1.1.0"), self.release("2.0.0")])
        self.assertEqual(self.resolve()["tools"][0]["version"], "1.1.0")
        self.releases[1] = self.release("1.1.0", YOUNG)
        policy = {
            "exceptions": [
                {
                    "package": "npm:sample-sdk",
                    "version": "1.1.0",
                    "minimum_safe": "1.1.0",
                    "reason": "Required fix",
                    "advisory": "https://example.invalid/fix",
                    "expires": NOW.isoformat(),
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.resolve(policy=policy)

    def test_post_hook_source_mutation_and_unrecorded_selection_fail(self):
        self.releases.extend([self.release("2.0.0"), self.release("3.0.0")])
        before = sdk.snapshot(self.root, self.spec)
        before["resolution"] = sdk.resolve(self.root, self.spec, {}, NOW, before=before)
        self.write("sources.json", {"sdk": source.record(self.tool, self.releases[1])})
        self.write("package.json", {"sdk": "2.0.0"})
        with self.assertRaisesRegex(ValueError, "changed after resolution"):
            sdk.audit(self.root, self.spec, before, {}, NOW)
        del before["resolution"]
        with self.assertRaisesRegex(ValueError, "without a recorded selection"):
            sdk.audit(self.root, self.spec, before, {}, NOW)

    def test_concurrent_source_document_change_fails_before_write(self):
        before = sdk.snapshot(self.root, self.spec)
        value = json.loads((self.root / "sources.json").read_text())
        value["other"] = 2
        self.write("sources.json", value)
        with self.assertRaisesRegex(ValueError, "concurrently"):
            self.resolve(before)

    def test_concurrent_change_during_render_fails(self):
        old = source.read(self.root, self.tool)
        original = source.document

        def document(root, pin):
            body, value, render = original(root, pin)

            def changed():
                (root / pin["file"]).write_text("changed externally")
                return render()

            return body, value, changed

        with (
            patch.object(source, "document", side_effect=document),
            self.assertRaisesRegex(ValueError, "concurrently"),
        ):
            source.write(
                self.root,
                self.tool,
                old,
                source.record(self.tool, self.release("2.0.0")),
            )
        self.assertEqual((self.root / "sources.json").read_text(), "changed externally")

    def test_strong_canonical_single_artifact_required(self):
        release = self.releases[0]
        for artifacts in (
            (),
            release.artifacts * 2,
            (
                registry.Artifact(
                    "https://example.invalid/tool.tgz", "sha512:" + "a" * 128, OLD
                ),
            ),
            (registry.Artifact(release.artifacts[0].url, "sha1:" + "a" * 40, OLD),),
        ):
            with (
                self.subTest(artifacts=artifacts),
                self.assertRaisesRegex(ValueError, "canonical"),
            ):
                source.record(
                    self.tool, registry.Release("1.0.0", OLD, artifacts=artifacts)
                )

    def test_baseline_source_must_match_observed_artifact(self):
        value = source.read(self.root, self.tool)
        value["url"] = "https://example.invalid/injected.tgz"
        self.write("sources.json", {"sdk": value})
        with self.assertRaisesRegex(ValueError, "differs"):
            sdk.snapshot(self.root, self.spec)

    def test_constraints_and_expired_exception_still_apply(self):
        self.releases.append(self.release("2.0.0"))
        policy = {
            "constraints": {
                "npm:sample-sdk": {
                    "range": "^1",
                    "reason": "API compatibility",
                }
            }
        }
        self.assertEqual(self.resolve(policy=policy)["changed"], [])
        policy["exceptions"] = [
            {
                "package": "npm:sample-sdk",
                "version": "1.0.0",
                "minimum_safe": "1.0.0",
                "reason": "Fix",
                "advisory": "https://example.invalid/advisory",
                "expires": NOW.isoformat(),
            }
        ]
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.resolve(policy=policy)

    def test_parsed_formats_and_symlink_escape(self):
        record = source.read(self.root, self.tool)
        for suffix in ("toml", "yaml"):
            file = f"sources.{suffix}"
            body = (
                "[sdk]\n"
                + "\n".join(f"{k} = {json.dumps(v)}" for k, v in record.items())
                if suffix == "toml"
                else "sdk:\n"
                + "\n".join(f"  {k}: {json.dumps(v)}" for k, v in record.items())
            )
            (self.root / file).write_text(body + "\n")
            self.tool["source_pin"]["file"] = file
            self.assertEqual(source.read(self.root, self.tool), record)
            self.releases.append(self.release("2.0.0"))
            self.assertTrue(
                source.write(
                    self.root,
                    self.tool,
                    record,
                    source.record(self.tool, self.releases[-1]),
                )
            )
        (self.root / "escape").symlink_to(self.root, target_is_directory=True)
        self.tool["source_pin"]["file"] = "escape/sources.json"
        with self.assertRaisesRegex(ValueError, "symlink"):
            source.read(self.root, self.tool)

    def test_duplicate_json_and_unparsed_source_rejected(self):
        (self.root / "sources.json").write_text('{"sdk": {}, "sdk": {}}')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            source.read(self.root, self.tool)
        for change in (
            {"provider": "crates"},
            {"source_pin": {"file": "flake.nix", "pointer": ["sdk"]}},
        ):
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(ValueError, "parsed"),
            ):
                source.declaration({**self.tool, **change})

    def test_overlapping_output_and_source_ownership_rejected(self):
        tool = copy.deepcopy(self.tool)
        tool["pins"][0].update(file="sources.json", pointer=["sdk", "version"])
        with self.assertRaisesRegex(ValueError, "overlaps"):
            sdk.tools({"tools": [tool]})


if __name__ == "__main__":
    unittest.main()
