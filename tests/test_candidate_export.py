"""Public candidates survive failed verification and disposable cache cleanup."""

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import candidate_export as export
import source_workflow
import toolchain as tc
import update_staging as staging
import updates
import test_source_workflow
import test_update_staging


class ManifestReadTests(unittest.TestCase):
    def test_check_rejects_oversized_snapshot_before_hashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "result.json").write_bytes(
                export.encoded(
                    {
                        "schema": 1,
                        "kind": "chainman.update-result",
                        "operation": "12345678-1234-1234-1234-123456789abc",
                        "outcome": "interrupted_or_unknown",
                        "stage": "verification",
                        "exit_code": None,
                        "snapshot_sha256": "0" * 64,
                    }
                )
            )
            with (root / "snapshot.json").open("wb") as stream:
                stream.truncate(export.MAX_MANIFEST + 1)
            with (
                patch.object(
                    export,
                    "digest",
                    side_effect=AssertionError("Oversized snapshot reached hashing"),
                ),
                self.assertRaisesRegex(ValueError, "exceeds 16 MiB"),
            ):
                export.check(root)

    def test_manifest_read_remains_bounded_after_file_growth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "snapshot.json"
            path.touch()
            before_growth = path.stat()
            path.write_bytes(b"x" * 64)
            with path.open("rb") as stream:
                reader = Mock(wraps=stream)
                with (
                    patch.object(export, "MAX_MANIFEST", 32),
                    patch.object(export.os, "fstat", return_value=before_growth),
                    patch.object(
                        Path, "open", return_value=contextlib.nullcontext(reader)
                    ),
                    self.assertRaisesRegex(ValueError, "exceeds 16 MiB"),
                ):
                    export.manifest_bytes(root, "snapshot.json")
                reader.read.assert_called_once_with(33)

    def test_json_manifest_at_exact_limit_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "result.json").write_bytes(b"{}" + b" " * 30)
            with patch.object(export, "MAX_MANIFEST", 32):
                self.assertEqual(export.read_json(root, "result.json"), {})


class CandidateExports(unittest.TestCase):
    setUp = test_source_workflow.SourceWorkflows.setUp
    resolve = test_source_workflow.SourceWorkflows.resolve

    def run_export(self, *, resolve=None, verify=None):
        self.output = self.base / "export"
        with (
            patch.object(updates, "perform", side_effect=resolve or self.resolve),
            patch.object(updates, "verify", side_effect=verify),
            contextlib.redirect_stdout(io.StringIO()) as stream,
        ):
            source_workflow.run(
                self.root, "deps-update", ["--export-candidate", str(self.output)]
            )
        return json.loads(stream.getvalue())

    def transaction(self):
        return self.base / "cache/chainman/updates/v1/candidate.fixture"

    def test_success_exports_without_applying_and_survives_pruning(self):
        result = self.run_export()
        self.assertEqual(result["outcome"], "verified_success")
        self.assertEqual(updates.snapshot(self.root), self.before)
        self.assertEqual(updates.git(self.root, "status", "--porcelain"), "")
        snapshot = export.validate_snapshot(self.output)
        self.assertEqual(snapshot["outputs"][0]["path"], "dependency.lock")
        self.assertEqual(snapshot["runtimes"]["entry"], snapshot["base"])
        with self.assertRaisesRegex(ValueError, "cannot apply"):
            staging.finalize(self.root, self.transaction())
        with self.assertRaisesRegex(ValueError, "cannot resume"):
            staging.resume(self.root, self.transaction())
        shutil.rmtree(self.transaction())
        self.assertEqual(export.check(self.output), result)

    def test_failed_verifier_cannot_rewrite_accepted_bytes(self):
        def verifier(root, selected):
            (root / "dependency.lock").write_text("tampered\n")
            raise ValueError("test rejected")

        with self.assertRaisesRegex(ValueError, "test rejected"):
            self.run_export(verify=verifier)
        result = export.check(self.output)
        self.assertEqual(result["outcome"], "accepted_verification_failed")
        row = export.validate_snapshot(self.output)["outputs"][0]
        self.assertEqual((self.output / "blobs" / row["sha256"]).read_bytes(), b"new\n")
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_passing_verifier_mutation_is_failure(self):
        def verifier(root, selected):
            (root / "dependency.lock").write_text("tampered\n")

        with self.assertRaises(SystemExit) as stopped:
            self.run_export(verify=verifier)
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(
            export.check(self.output)["outcome"], "accepted_verification_failed"
        )

    def test_failure_before_selection_has_no_snapshot(self):
        def fail(*args):
            raise ValueError("registry unavailable")

        with self.assertRaisesRegex(ValueError, "registry unavailable"):
            self.run_export(resolve=fail)
        result = export.check(self.output)
        self.assertEqual(result["outcome"], "failure_before_acceptance")
        self.assertIsNone(result["snapshot_sha256"])
        self.assertFalse((self.output / "snapshot.json").exists())

    def test_changed_base_cannot_use_a_different_source_runtime(self):
        with patch.dict(os.environ, CHAINMAN_SOURCE_EXPORT_REVISION="a" * 40):
            with self.assertRaisesRegex(ValueError, "Source base changed"):
                self.run_export(
                    resolve=lambda *args: self.fail(
                        "mismatched base reached resolution"
                    )
                )
        self.assertEqual(
            export.check(self.output)["outcome"], "failure_before_acceptance"
        )
        self.assertFalse((self.output / "snapshot.json").exists())

    def test_no_change_is_success_only_after_resolution(self):
        result = self.run_export(resolve=lambda *args: None)
        self.assertEqual(result["outcome"], "complete_no_change")
        self.assertEqual(export.validate_snapshot(self.output)["outputs"], [])

    def test_interruption_keeps_accepted_snapshot_without_success(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_export(
                verify=lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
            )
        result = export.check(self.output)
        self.assertEqual(result["outcome"], "interrupted_or_unknown")
        self.assertIsNotNone(result["snapshot_sha256"])

    def test_addition_deletion_and_executable_mode_round_trip(self):
        # Reuse the existing allowed path, first as a deletion.
        self.run_export(resolve=lambda root, *args: (root / "dependency.lock").unlink())
        row = export.validate_snapshot(self.output)["outputs"][0]
        self.assertEqual(
            row, {"path": "dependency.lock", "sha256": None, "mode": None, "size": 0}
        )
        shutil.rmtree(self.output)
        shutil.rmtree(self.transaction())

        def executable(root, *args):
            self.resolve(root)
            (root / "dependency.lock").chmod(0o755)

        self.run_export(resolve=executable)
        self.assertEqual(
            export.validate_snapshot(self.output)["outputs"][0]["mode"], "100755"
        )

    def test_addition_is_exported_without_applying(self):
        (self.root / "dependency.lock").unlink()
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-qm", "No baseline lock")
        self.before = updates.snapshot(self.root)
        self.run_export()
        self.assertFalse((self.root / "dependency.lock").exists())
        self.assertEqual(
            export.validate_snapshot(self.output)["outputs"][0]["sha256"],
            export.digest(b"new\n"),
        )

    def test_corrupt_blob_and_schema_are_rejected(self):
        self.run_export()
        row = export.validate_snapshot(self.output)["outputs"][0]
        blob = self.output / "blobs" / row["sha256"]
        blob.chmod(0o600)
        blob.write_bytes(b"bad\n")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            export.check(self.output)
        blob.write_bytes(b"new\n")
        result = self.output / "result.json"
        original = result.read_bytes()
        result.write_text(original.decode().replace('"schema":1', '"schema":2'))
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            export.check(self.output)
        result.write_bytes(original[:-2] + b',"schema":1}\n')
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            export.check(self.output)

    def test_missing_final_result_never_becomes_success(self):
        self.run_export()
        (self.output / "result.json").unlink()
        with self.assertRaises(FileNotFoundError):
            export.check(self.output)

    def test_interrupted_capture_does_not_publish_acceptance(self):
        original = export.tc.atomic_bytes

        def interrupted(path, body, *args):
            if path.name == "snapshot.json":
                raise OSError("interrupted capture")
            return original(path, body, *args)

        with patch.object(export.tc, "atomic_bytes", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted capture"):
                self.run_export()
        result = export.check(self.output)
        self.assertIsNone(result["snapshot_sha256"])
        self.assertEqual(result["outcome"], "failure_before_acceptance")

    def test_rebound_invalid_manifest_is_still_rejected(self):
        self.run_export()
        snapshot = self.output / "snapshot.json"
        snapshot.chmod(0o600)
        original = json.loads(snapshot.read_text())
        result = export.check(self.output)
        for change in (
            lambda d: d.update(extra=True),
            lambda d: d["outputs"][0].update(path="../escape"),
            lambda d: d["outputs"][0].update(mode="120000"),
        ):
            data = json.loads(json.dumps(original))
            change(data)
            body = export.encoded(data)
            snapshot.write_bytes(body)
            result["snapshot_sha256"] = export.digest(body)
            (self.output / "result.json").write_bytes(export.encoded(result))
            with self.assertRaises(ValueError):
                export.check(self.output)

    def test_destination_overlap_alias_and_overwrite_are_rejected(self):
        transaction = self.base / "transaction"
        for path in (
            self.root / "export",
            self.base,
            self.base / "cache/chainman/updates/export",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                export.destination(self.root, str(path), transaction)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            export.destination(self.root, str(alias / "export"), transaction)
        self.run_export()
        original = (self.output / "snapshot.json").read_bytes()
        with self.assertRaises(FileExistsError):
            export.start(self.root, self.transaction(), str(self.output), source=True)
        self.assertEqual((self.output / "snapshot.json").read_bytes(), original)

    def test_options_reject_resume_and_apply_controls(self):
        for args in (
            ["resume=/tmp/x"],
            ["mode=dry-run"],
            ["commit=off"],
            ["commit=on"],
            ["--format"],
            ["--export-candidate", "/tmp/other"],
        ):
            with self.subTest(args=args), self.assertRaises((ValueError, SystemExit)):
                export.split_arguments(["--export-candidate", "/tmp/export", *args])

    def test_unsupported_policy_is_not_an_accepted_candidate(self):
        with patch.object(
            export, "capture", side_effect=export.Unsupported("opaque resolver")
        ):
            with self.assertRaises(export.Unsupported):
                self.run_export()
        self.assertEqual(export.check(self.output)["outcome"], "unsupported")
        self.assertFalse((self.output / "snapshot.json").exists())

    def test_reconstruct_then_repair_and_independently_verify(self):
        with self.assertRaisesRegex(ValueError, "repair needed"):
            self.run_export(
                verify=lambda *args: (_ for _ in ()).throw(ValueError("repair needed"))
            )
        manifest = export.validate_snapshot(self.output)
        repaired = self.base / "repaired"
        updates.copy_submodule(self.root, repaired, manifest["base"]["commit"])
        for row in manifest["outputs"]:
            path = repaired / row["path"]
            if row["sha256"] is None:
                path.unlink()
            else:
                path.write_bytes((self.output / "blobs" / row["sha256"]).read_bytes())
                path.chmod(0o755 if row["mode"] == "100755" else 0o644)
        (repaired / "application.py").write_text("EXPECTED = 'new'")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from application import EXPECTED; from pathlib import Path; assert Path('dependency.lock').read_text().strip() == EXPECTED",
            ],
            cwd=repaired,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(updates.snapshot(self.root), self.before)
        self.assertEqual(
            export.check(self.output)["outcome"], "accepted_verification_failed"
        )


class ConsumerRuntimeExportTests(unittest.TestCase):
    def setUp(self):
        test_update_staging.StagingTests.setUp(self)
        (self.root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[updates]
outputs=["dependency.lock"]
verify_task="verify"
[updates.adapters.actions]
adapter="actions"
files=["dependency.lock"]
[[updates.steps]]
resolve="actions"
[tasks.verify]
commands=[["true"]]
""")
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-qm", "Adapter fixture")
        self.before = updates.snapshot(self.root)

    def prepare_export(self):
        target = self.base / "export"
        export.start(self.root, self.stage, str(target), source=False)
        staging.prepare(self.root, self.stage, ["--skip-chainman"])
        (self.candidate / "dependency.lock").write_text("new\n")
        staging.inspect(self.root, self.stage)
        export.capture(self.root, self.stage)
        export.stage(self.stage, "verification")
        return target

    def assert_finished_failure(self, target):
        accepted = (target / "snapshot.json").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = staging.run(
                self.root, "_update-export-finish", [str(self.stage), "0"]
            )
        self.assertEqual(code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result, export.check(target))
        self.assertEqual(result["outcome"], "accepted_verification_failed")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["stage"], "verification")
        self.assertEqual((target / "snapshot.json").read_bytes(), accepted)

    def test_passing_verifier_git_mutation_produces_a_final_failure(self):
        target = self.prepare_export()
        with (self.candidate / ".git/info/exclude").open("a") as stream:
            stream.write("verifier-ignore\n")
        self.assert_finished_failure(target)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_original_changes_produce_a_final_failure_and_remain_untouched(self):
        target = self.prepare_export()
        (self.root / "source.txt").write_text("concurrent user edit\n")
        self.assert_finished_failure(target)
        self.assertEqual(
            (self.root / "source.txt").read_text(), "concurrent user edit\n"
        )

    def test_selected_runtime_is_reported_without_changing_original_pin(self):
        import chainman
        import chainman_updates

        target = self.base / "export"
        export.start(self.root, self.stage, str(target), source=False)
        staging.prepare(self.root, self.stage, [])

        def choose(root, *args, **kwargs):
            (root / "chainman.lock").write_text("b" * 40 + "\n")
            return chainman.RUNTIME

        with patch.object(chainman_updates, "runtime_candidate", side_effect=choose):
            staging.prepare_runtime(self.root, self.stage)
        (self.candidate / "dependency.lock").write_text("new\n")
        staging.inspect(self.root, self.stage)
        export.capture(self.root, self.stage)
        export.stage(self.stage, "verification")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(export.finish(self.root, self.stage, 0), 0)
        manifest = export.validate_snapshot(target)
        self.assertEqual(manifest["runtimes"]["entry"]["commit"], "a" * 40)
        self.assertEqual(manifest["runtimes"]["resolution"]["commit"], "b" * 40)
        self.assertEqual(manifest["runtimes"]["verification"]["commit"], "b" * 40)
        self.assertEqual((self.root / "chainman.lock").read_text(), "a" * 40 + "\n")


class IsolatedRuntimeTests(unittest.TestCase):
    def test_runtime_tree_identity_matches_git_and_ignores_checkout_filters(self):
        import git_runtime

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, materialized = root / "repo", root / "runtime"
            repo.mkdir()
            (repo / "a").mkdir()
            (repo / "a.txt").write_bytes(b"binary\x00bytes")
            (repo / "a/file").write_bytes(b"executable")
            (repo / "a/file").chmod(0o755)
            updates.git(repo, "init", "-q")
            updates.git(repo, "add", ".")
            updates.git(
                repo,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-qm",
                "Fixture",
            )
            revision = updates.git(repo, "rev-parse", "HEAD")
            git_runtime.materialize(revision, materialized, repository=repo)
            self.assertEqual(
                git_runtime.tree_identity(materialized),
                updates.git(repo, "rev-parse", "HEAD^{tree}"),
            )
            import zlib

            oid = updates.git(repo, "rev-parse", "HEAD:a.txt")
            damaged = repo / ".git/objects" / oid[:2] / oid[2:]
            damaged.chmod(0o600)
            damaged.write_bytes(zlib.compress(b"blob 7\0damaged"))
            with self.assertRaisesRegex(ValueError, "tree bytes"):
                git_runtime.materialize(revision, root / "corrupt", repository=repo)

    def test_candidate_environment_cannot_supply_control_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime, candidate = base / "runtime", base / "candidate"
            runtime.mkdir()
            candidate.mkdir()
            shutil.copyfile(tc.RUNTIME / "scripts/isolated.py", runtime / "isolated.py")
            (runtime / "chainman.py").write_text("import control; print(control.VALUE)")
            (runtime / "control.py").write_text("VALUE='trusted'")
            (candidate / "control.py").write_text("VALUE='candidate'")
            marker = base / "injected"
            (candidate / "sitecustomize.py").write_text(
                f"open({str(marker)!r}, 'w').close()"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(runtime / "isolated.py"),
                    "chainman.py",
                ],
                cwd=candidate,
                env=dict(os.environ, PYTHONPATH=str(candidate)),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), "trusted")
            self.assertFalse(marker.exists())
            with patch.dict(os.environ, CHAINMAN_SOURCE_EXPORT_RUNTIME=str(tc.RUNTIME)):
                self.assertEqual(
                    tc.entry_command(candidate, "core")[:4],
                    [
                        str(tc.RUNTIME / "scripts/enter.sh"),
                        "core",
                        "--project-root",
                        str(candidate),
                    ],
                )


if __name__ == "__main__":
    unittest.main()
