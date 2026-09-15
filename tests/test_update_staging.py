"""A verifier can approve only a frozen candidate, never concurrent user edits."""

import contextlib
import io
import json
import shutil
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chainman
import dependency_api
import update_staging as subject
import updates


class StagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="update staging ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "original"
        self.stage = self.base / "transaction"
        self.root.mkdir()
        self.stage.mkdir()
        for name in ("control", "candidate"):
            (self.stage / name).mkdir()
        self.candidate = self.stage / "candidate"
        (self.root / "chainman.toml").write_text("""schema=2
[project]
default_profile="host"
[updates]
eligibility="resolver"
resolver=[["unused"]]
outputs=["dependency.lock"]
verify_task="verify"
[tasks.verify]
commands=[["true"]]
""")
        (self.root / ".gitignore").write_text(".cache/\n")
        (self.root / "chainman.lock").write_text("a" * 40 + "\n")
        runtime = self.base / "runtime"
        for name in ("bootstrap", "scripts", "nix"):
            shutil.copytree(
                chainman.RUNTIME / name,
                runtime / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        shutil.copy2(chainman.RUNTIME / "VERSION", runtime / "VERSION")
        selected_runtime = patch.object(chainman, "RUNTIME", runtime)
        selected_runtime.start()
        self.addCleanup(selected_runtime.stop)
        (self.root / "dependency.lock").write_text("old\n")
        (self.root / "source.txt").write_text("user source\n")
        updates.git(self.root, "init", "-b", "main")
        updates.git(self.root, "config", "user.name", "Test")
        updates.git(self.root, "config", "user.email", "test@example.invalid")
        updates.git(self.root, "config", "commit.gpgsign", "false")
        updates.git(self.root, "add", ".")
        updates.git(
            self.root, "-c", "core.hooksPath=/dev/null", "commit", "-m", "Initial"
        )
        self.before = updates.snapshot(self.root)
        # The Nix fetch has independent real-container qualification. These tests
        # exercise real Git/raw bytes/index/modes and transaction interleavings.
        self.real_verified_runtime = subject.verified_runtime
        mock = patch.object(subject, "verified_runtime", return_value=chainman.RUNTIME)
        mock.start()
        self.addCleanup(mock.stop)
        discovery = patch.object(
            subject.git_runtime, "default_revision", return_value="b" * 40
        )
        self.discovery = discovery.start()
        self.addCleanup(discovery.stop)

    def prepare(self, *args):
        # Most fixtures exercise project-only transaction behavior without a
        # release source. Full/runtime updates opt into their own staged fixture.
        if "--only-chainman" not in args and "--include-chainman" not in args:
            args = ("--skip-chainman", *args)
        subject.prepare(self.root, self.stage, list(args))

    def update(self, *args):
        self.prepare(*args)
        (self.candidate / "dependency.lock").write_text("new\n")
        subject.inspect(self.root, self.stage)

    def finish(self):
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            subject.finalize(self.root, self.stage)
        return json.loads(stream.getvalue())

    def test_finalize_requires_inspection_and_resume_discards_old_inspection(self):
        self.prepare("--no-commit")
        with self.assertRaisesRegex(ValueError, "not been inspected"):
            self.finish()
        (self.candidate / "dependency.lock").write_text("new\n")
        subject.inspect(self.root, self.stage)
        subject.resume(self.root, self.stage)
        with self.assertRaisesRegex(ValueError, "not been inspected"):
            self.finish()
        self.assertEqual(updates.snapshot(self.root), self.before)
        subject.inspect(self.root, self.stage)
        self.assertEqual(self.finish()["changed"], ["dependency.lock"])
        self.assertEqual((self.root / "dependency.lock").read_text(), "new\n")

    def test_corrupt_checkpoint_fails_before_git_or_publication(self):
        self.update("--no-commit")
        path = self.stage / "control/state.json"
        original = json.loads(path.read_text())
        for field, invalid in (
            ("before", []),
            ("paths", "dependency.lock"),
            ("options", {}),
            ("candidate_index", {"source.txt": ["100644", False]}),
        ):
            path.write_text(json.dumps({**original, field: invalid}))
            with self.subTest(field=field), patch.object(updates, "repository") as git:
                with self.assertRaisesRegex(ValueError, "Invalid update state"):
                    self.finish()
                git.assert_not_called()
            self.assertEqual(updates.snapshot(self.root), self.before)

    def test_secondary_policy_cannot_choose_reconciliation_before_inspection(self):
        config = self.root / "chainman.toml"
        config.write_text(
            config.read_text().replace(
                "[updates]", '[updates]\npolicy_file="policy.toml"'
            )
        )
        (self.root / "policy.toml").write_text('reconcile_tasks=["verify"]\n')
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-m", "Declare secondary policy")
        self.prepare()
        (self.candidate / "policy.toml").write_text('reconcile_tasks=["privileged"]\n')
        with patch.dict(
            os.environ, CHAINMAN_ENTRY_AUTHORITY=str(self.stage / "original-bootstrap")
        ):
            self.assertEqual(
                dependency_api.policy(self.candidate)["reconcile_tasks"], ["verify"]
            )
        with self.assertRaises(ValueError):
            subject.inspect(self.root, self.stage)

    def test_nested_administration_is_rejected_before_git_can_run_filters(self):
        nested = self.root / "nested input"
        nested.mkdir()
        updates.git(nested, "init", "-b", "main")
        updates.git(nested, "config", "user.name", "Test")
        updates.git(nested, "config", "user.email", "test@example.invalid")
        (nested / "value").write_text("old\n")
        updates.git(nested, "add", ".")
        updates.git(nested, "-c", "commit.gpgsign=false", "commit", "-m", "Input")
        updates.git(self.root, "add", "nested input")
        updates.git(self.root, "commit", "-m", "Add frozen input")
        self.prepare()
        state = json.loads((self.stage / "control/state.json").read_text())
        self.assertEqual(set(state["candidate_git"]), {".git", "nested input/.git"})
        self.assertEqual(
            (self.stage / "original-bootstrap/git-directories").read_text(),
            ".git\nnested input/.git\n",
        )
        metadata = self.candidate / "nested input/.git"
        with (metadata / "config").open("a") as stream:
            stream.write('\n[filter "probe"]\nclean = "touch filter-executed; cat"\n')
        (metadata / "info/attributes").write_text("value filter=probe\n")
        (self.candidate / "nested input/value").write_text("new\n")
        with self.assertRaisesRegex(ValueError, "Git administration"):
            subject.inspect(self.root, self.stage)
        self.assertFalse((self.candidate / "nested input/filter-executed").exists())

    def add_runtime_copy(self):
        config = self.root / "chainman.toml"
        config.write_text(
            config.read_text().replace(
                'outputs=["dependency.lock"]',
                'outputs=["dependency.lock", "templates/**"]',
            )
            + '\n[runtime]\ncopies=["templates/common"]\n'
        )
        path = self.root / "templates/common/chainman.lock"
        path.parent.mkdir(parents=True)
        path.write_text("a" * 40 + "\n")
        updates.git(self.root, "add", ".")
        updates.git(
            self.root,
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "Declare runtime copy",
        )
        self.before = updates.snapshot(self.root)

    def test_dependency_resolver_cannot_modify_declared_runtime_copy(self):
        self.add_runtime_copy()
        self.prepare()
        (self.candidate / "templates/common/chainman.lock").write_text("changed copy")
        with self.assertRaisesRegex(ValueError, "must not change the runtime"):
            subject.inspect(self.root, self.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_runtime_copy_is_in_the_original_transaction_output_boundary(self):
        self.add_runtime_copy()
        self.prepare("--only-chainman")
        relative = "templates/common/chainman.lock"

        def runtime_candidate(root, policy, now, *, gc_root, revision):
            (root / relative).write_text("verified copy")
            return chainman.RUNTIME

        with patch.object(
            subject.runtime_updates, "runtime_candidate", side_effect=runtime_candidate
        ):
            subject.prepare_runtime(self.root, self.stage)
        subject.inspect(self.root, self.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)
        result = self.finish()
        self.assertEqual(result["changed"], [relative])
        self.assertEqual((self.root / relative).read_text(), "verified copy")
        self.assertEqual(updates.git(self.root, "status", "--porcelain"), "")

    def test_full_update_stages_runtime_then_combines_verified_project_outputs(self):
        config = self.root / "chainman.toml"
        config.write_text(
            config.read_text().replace(
                "[updates]", '[updates]\nreconcile_outputs=["runtime-report.txt"]'
            )
        )
        updates.git(self.root, "add", ".")
        updates.git(self.root, "commit", "-m", "Declare runtime report")
        self.before = updates.snapshot(self.root)
        subject.prepare(self.root, self.stage, [])

        def runtime_candidate(root, policy, now, *, gc_root, revision):
            (root / "chainman.lock").write_text("b" * 40 + "\n")
            return chainman.RUNTIME

        with patch.object(
            subject.runtime_updates, "runtime_candidate", side_effect=runtime_candidate
        ):
            subject.prepare_runtime(self.root, self.stage)
        state, _ = subject.read_state(self.root, self.stage)
        self.assertEqual(state.options.runtime.value, "include")
        self.assertIn("chainman.lock", state.runtime_snapshot)
        self.assertEqual(
            (self.stage / "resolution-bootstrap/chainman.lock").read_text(),
            "b" * 40 + "\n",
        )
        (self.candidate / "dependency.lock").write_text("resolved with new runtime\n")
        (self.candidate / "runtime-report.txt").write_text(
            "reconciled runtime report\n"
        )
        subject.inspect(self.root, self.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)
        subject.resume(self.root, self.stage)
        resumed = (self.stage / "control/resume-arguments").read_text().splitlines()
        self.assertEqual(
            subject.runtime_updates.options(resumed).runtime.value, "include"
        )
        subject.inspect(self.root, self.stage)
        result = self.finish()
        self.assertEqual(
            set(result["changed"]),
            {"chainman.lock", "dependency.lock", "runtime-report.txt"},
        )
        self.assertEqual((self.root / "chainman.lock").read_text(), "b" * 40 + "\n")
        self.assertEqual(
            (self.root / "dependency.lock").read_text(), "resolved with new runtime\n"
        )
        self.assertEqual(updates.git(self.root, "status", "--porcelain"), "")

    def test_full_update_requires_runtime_preparation_before_inspection(self):
        subject.prepare(self.root, self.stage, [])
        with self.assertRaisesRegex(ValueError, "runtime has not been prepared"):
            subject.inspect(self.root, self.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_failed_runtime_preparation_resumes_before_project_resolution(self):
        subject.prepare(self.root, self.stage, [])
        with (
            patch.object(
                subject.runtime_updates,
                "runtime_candidate",
                side_effect=OSError("Git unavailable"),
            ),
            self.assertRaisesRegex(ValueError, "--skip-chainman"),
        ):
            subject.prepare_runtime(self.root, self.stage)
        state, _ = subject.read_state(self.root, self.stage)
        self.assertEqual(state.runtime_revision, "b" * 40)
        self.discovery.side_effect = AssertionError(
            "Resume must not resolve a moving branch"
        )
        self.assertEqual(updates.snapshot(self.candidate), self.before)
        subject.resume(self.root, self.stage)
        self.assertEqual((self.stage / "control/retry-runtime").read_text(), "yes\n")
        with patch.object(
            subject.runtime_updates, "runtime_candidate", return_value=chainman.RUNTIME
        ) as selected:
            subject.prepare_runtime(self.root, self.stage)
        self.assertEqual(selected.call_args.kwargs["revision"], "b" * 40)
        (self.candidate / "dependency.lock").write_text("resolved\n")
        subject.resume(self.root, self.stage)
        self.assertEqual((self.stage / "control/retry-runtime").read_text(), "no\n")
        subject.inspect(self.root, self.stage)
        self.assertEqual(self.finish()["changed"], ["dependency.lock"])
        self.assertEqual((self.root / "dependency.lock").read_text(), "resolved\n")

    def test_apply_only_after_verification_and_commit_exact_candidate(self):
        self.update()
        self.assertEqual(
            updates.repository(self.root),
            updates.repository(self.candidate, clean=False),
        )
        self.assertEqual(updates.git(self.candidate, "remote"), "")
        self.assertEqual(
            updates.git(self.candidate, "rev-list", "--count", "HEAD"), "1"
        )
        self.assertEqual(updates.snapshot(self.root), self.before)
        result = self.finish()
        self.assertEqual(result["changed"], ["dependency.lock"])
        self.assertEqual(result["verification"], "passed")
        self.assertEqual(updates.git(self.root, "rev-parse", "HEAD"), result["commit"])
        self.assertEqual(updates.git(self.root, "status", "--porcelain"), "")
        self.assertEqual((self.root / "dependency.lock").read_text(), "new\n")

    def test_no_commit_and_preview(self):
        for mode in ("--no-commit", "--preview"):
            with self.subTest(mode=mode):
                # Separate fixture per mode because a completed transaction is immutable.
                test = StagingTests()
                test.setUp()
                try:
                    test.update(mode)
                    result = test.finish()
                    self.assertIsNone(result["commit"])
                    expected = "old\n" if mode == "--preview" else "new\n"
                    self.assertEqual(
                        (test.root / "dependency.lock").read_text(), expected
                    )
                finally:
                    test.doCleanups()

    def test_verifier_source_changes_leave_original_untouched(self):
        self.update("--no-commit")
        (self.candidate / "source.txt").write_text("verifier changed source")
        with self.assertRaisesRegex(ValueError, "Verification changed"):
            self.finish()
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_original_concurrent_edit_is_preserved(self):
        self.update("--no-commit")
        (self.root / "source.txt").write_text("concurrent user edit")
        with self.assertRaisesRegex(ValueError, "Original checkout changed"):
            self.finish()
        self.assertEqual((self.root / "source.txt").read_text(), "concurrent user edit")
        self.assertEqual((self.root / "dependency.lock").read_text(), "old\n")

    def test_original_concurrent_index_change_is_preserved(self):
        self.update("--no-commit")
        (self.root / "source.txt").write_text("concurrent staged edit")
        updates.git(self.root, "add", "source.txt")
        (self.root / "source.txt").write_text("user source\n")
        with self.assertRaisesRegex(ValueError, "Original checkout changed"):
            self.finish()
        self.assertEqual(
            updates.git(self.root, "show", ":source.txt"), "concurrent staged edit"
        )

    def test_candidate_cannot_stage_or_commit_during_verification(self):
        self.update()
        updates.git(self.candidate, "add", "dependency.lock")
        with self.assertRaisesRegex(ValueError, "candidate Git"):
            self.finish()
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_resolver_cannot_expand_scope(self):
        self.prepare()
        (self.candidate / "source.txt").write_text("unauthorized resolver edit")
        with self.assertRaisesRegex(ValueError, "Unexpected update"):
            subject.inspect(self.root, self.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_resolver_cannot_replace_bootstrap(self):
        self.prepare()
        (self.candidate / "scripts").mkdir()
        (self.candidate / "justfile").write_text("arbitrary host code")
        with self.assertRaisesRegex(
            ValueError, "Unexpected update/verification output"
        ):
            subject.inspect(self.root, self.stage)
        self.assertFalse((self.stage / "candidate-bootstrap").exists())

    def test_symlink_output_is_refused_before_verification(self):
        self.prepare()
        (self.candidate / "dependency.lock").unlink()
        (self.candidate / "dependency.lock").symlink_to("source.txt")
        with self.assertRaises(ValueError):
            subject.inspect(self.root, self.stage)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_preview_accepts_dirty_source_without_applying_or_committing(self):
        (self.root / "source.txt").write_text("uncommitted input")
        updates.git(self.root, "add", "source.txt")
        before_index = subject.index(self.root)
        self.update("--preview")
        self.assertEqual(
            (self.candidate / "source.txt").read_text(), "uncommitted input"
        )
        self.assertTrue(self.finish()["preview"])
        self.assertEqual(subject.index(self.root), before_index)
        self.assertEqual((self.root / "dependency.lock").read_text(), "old\n")

    def test_no_changes_does_not_request_verification(self):
        self.prepare()
        subject.inspect(self.root, self.stage)
        self.assertEqual((self.stage / "control/changed").read_text(), "no\n")
        self.assertFalse((self.stage / "candidate-bootstrap").exists())
        self.assertEqual(self.finish()["verification"], "no changes")

    def test_dependency_reaudit_uses_committed_blobs_after_edit_and_deletion(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        self.prepare()
        (self.candidate / "dependency.lock").write_text("new\n")
        (self.candidate / "source.txt").unlink()
        seen = []

        def snapshot(root, spec):
            self.assertEqual((root / "dependency.lock").read_text(), "old\n")
            self.assertEqual((root / "source.txt").read_text(), "user source\n")
            return {"identities": "original identities"}

        def audit(root, spec, before, policy, now):
            self.assertEqual(before, {"identities": "original identities"})
            self.assertEqual((root / "dependency.lock").read_text(), "new\n")
            self.assertFalse((root / "source.txt").exists())
            seen.append(now)

        with (
            patch.object(
                subject.dependency_api,
                "policy",
                return_value={"steps": [{"resolve": "js"}]},
            ),
            patch.object(
                subject.dependency_api,
                "plan_steps",
                return_value=([], {}, {"js": ({}, {})}),
            ),
            patch.object(
                subject.dependency_api,
                "implementation",
                return_value=SimpleNamespace(snapshot=snapshot, audit=audit),
            ),
        ):
            now = datetime.now(timezone.utc).isoformat()
            subject.reaudit(self.candidate, now, [])
            subject.resume(self.root, self.stage)
            subject.reaudit(
                self.candidate, (self.stage / "control/at").read_text().strip(), []
            )
        self.assertEqual(len(seen), 2)
        self.assertEqual(updates.snapshot(self.root), self.before)

    def test_runtime_only_scope_does_not_include_dependency_outputs(self):
        self.prepare("--only-chainman")
        (self.candidate / "dependency.lock").write_text("unexpected")
        with self.assertRaisesRegex(
            ValueError, "Unexpected update/verification output"
        ):
            subject.inspect(self.root, self.stage)

    def test_invalid_runtime_identity_is_rejected_before_export(self):
        self.prepare()
        (self.candidate / "chainman.lock").write_text("main\n")
        with self.assertRaisesRegex(ValueError, "full lowercase Git SHA"):
            self.real_verified_runtime(
                self.candidate, gc_root=self.stage / "runtime-root"
            )

    def test_verified_runtime_accepts_generated_copy_permissions(self):
        self.prepare()
        runtime = self.base / "verified-runtime"
        for name in (
            "VERSION",
            "bootstrap/git-entry.sh",
            "bootstrap/chainman.sh",
            "bootstrap/fetch.nix",
            "nix/flake.nix",
            "nix/flake.lock",
            "scripts/chainman.py",
            "scripts/chainman_updates.py",
        ):
            destination = runtime / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(chainman.RUNTIME / name, destination)
        config = self.candidate / "chainman.toml"
        config.write_text(config.read_text() + '\n[runtime]\ncopies=["export"]\n')
        (self.candidate / "scripts").mkdir()
        for source, target in (
            ("chainman.sh", "chainman.sh"),
            ("fetch.nix", "chainman-fetch.nix"),
        ):
            shutil.copy2(
                chainman.RUNTIME / "bootstrap" / source,
                self.candidate / "scripts" / target,
            )
        for name in (
            "chainman.lock",
            "scripts/chainman.sh",
            "scripts/chainman-fetch.nix",
        ):
            original = self.candidate / name
            exported = self.candidate / "export" / name
            exported.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, exported)
            original.chmod(0o775 if name.endswith(".sh") else 0o664)
            exported.chmod(0o755 if name.endswith(".sh") else 0o644)
        nar = "unused"
        with (
            patch.object(subject.runtime_updates, "fetch_source", return_value=runtime),
            patch.object(
                subject.tc,
                "managed_run",
                return_value=subprocess.CompletedProcess([], 0, nar + "\n"),
            ),
        ):
            self.assertEqual(
                self.real_verified_runtime(
                    self.candidate, gc_root=self.stage / "runtime-root"
                ),
                runtime,
            )
            (self.candidate / "export/chainman.lock").write_text("b" * 40 + "\n")
            with self.assertRaisesRegex(ValueError, "runtime copy differs"):
                self.real_verified_runtime(
                    self.candidate, gc_root=self.stage / "runtime-root"
                )

    def test_update_verification_must_terminate(self):
        path = self.root / "chainman.toml"
        path.write_text(
            path.read_text()
            + 'wait_for_services=true\nservices=["endpoint"]\n[services.endpoint]\ncommand=["true"]\n'
        )
        with self.assertRaisesRegex(ValueError, "finite task"):
            subject.verification(self.root, {"verify_task": "verify"})

    def test_verification_sequence_preserves_separate_service_lifetimes(self):
        path = self.root / "chainman.toml"
        path.write_text(path.read_text() + '\n[tasks.second]\ncommands=[["true"]]\n')
        self.assertEqual(
            subject.verification(self.root, {"verify_tasks": ["verify", "second"]}),
            ["run", "verify", "run", "second"],
        )
        for policy in (
            {"verify_task": "verify", "verify_tasks": ["second"]},
            {"verify_tasks": ["verify"], "verify": [["true"]]},
            {"verify_tasks": []},
            {"verify_tasks": ["verify", "verify"]},
            {"verify_tasks": ["verify", "missing"]},
            {"verify_tasks": ["verify", "not\na\ntask"]},
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                subject.verification(self.root, policy)
        path.write_text(
            path.read_text()
            + 'wait_for_services=true\nservices=["endpoint"]\n[services.endpoint]\ncommand=["true"]\n'
        )
        with self.assertRaisesRegex(ValueError, "finite task"):
            subject.verification(self.root, {"verify_tasks": ["verify", "second"]})


if __name__ == "__main__":
    unittest.main()
