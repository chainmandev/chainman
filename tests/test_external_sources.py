"""External source trees share consumer ownership without widening host access."""

from contextlib import ExitStack, nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap_plan
import chainman
import services
import source_roots
import storage
import workflows


class ExternalSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman external source ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.consumer = self.base / "consumer"
        self.source = self.base / "website"
        self.other = self.base / "other-website"
        for root in (self.consumer, self.source, self.other):
            root.mkdir()
            (root / "input.lock").write_text(root.name)
            (root / "artifact").write_text(root.name + " installed")
        self.env = {"FIXTURE_SOURCE": str(self.source), "FIXTURE_GENERATION": "one"}
        self.cfg = {
            "schema": 3,
            "project": {"default_profile": "host"},
            "setup": {
                "website": {
                    "source_root_env": "FIXTURE_SOURCE",
                    "inputs": ["input.lock"],
                    "artifacts": [{"path": "artifact", "digest": True}],
                    "commands": [["installer"]],
                }
            },
            "services": {
                "web": {
                    "source_root_env": "FIXTURE_SOURCE",
                    "setup": ["website"],
                    "command": ["serve"],
                    "readiness": {"command": ["ready"]},
                    "shutdown_command": ["checkpoint"],
                    "environment_inputs": ["FIXTURE_GENERATION"],
                }
            },
            "tasks": {"work": {"commands": [["true"]], "services": ["web"]}},
        }
        stack = ExitStack()
        self.addCleanup(stack.close)
        inherited = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        stack.enter_context(patch.dict(os.environ, inherited, clear=True))
        stack.enter_context(
            patch.object(chainman, "profile", return_value=("fixture#host", {}))
        )
        stack.enter_context(
            patch.object(chainman, "profile_fingerprint", return_value="admitted-tools")
        )
        stack.enter_context(
            patch.object(
                chainman, "profile_inputs", return_value={"flake.nix": "profile"}
            )
        )
        stack.enter_context(
            patch.object(services.tc, "context_id", return_value="fixture-context")
        )
        stack.enter_context(
            patch.object(storage, "download_epoch", return_value="fixture-epoch")
        )
        stack.enter_context(
            patch.object(
                services.project_environment, "file_fingerprint", return_value={}
            )
        )

    def test_explicit_selection_is_canonical_and_commands_remain_contained(self):
        link = self.base / "source-link"
        link.symlink_to(self.source, target_is_directory=True)
        spec = {"source_root_env": "FIXTURE_SOURCE", "directory": "nested"}
        (self.source / "nested").mkdir()
        with patch.dict(os.environ, FIXTURE_SOURCE=str(self.other)):
            self.assertEqual(
                source_roots.root(self.consumer, spec, {"FIXTURE_SOURCE": str(link)}),
                self.source,
            )
            self.assertEqual(
                source_roots.directory(self.consumer, spec, self.env),
                self.source / "nested",
            )
        self.assertEqual(source_roots.root(self.consumer, {}, {}), self.consumer)
        (self.source / "escape").symlink_to(self.other, target_is_directory=True)
        for directory in ("../other-website", str(self.other), "escape"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                source_roots.directory(
                    self.consumer, {**spec, "directory": directory}, self.env
                )

    def test_external_working_directory_does_not_resolve_consumer_links(self):
        (self.consumer / "nested").symlink_to(self.base, target_is_directory=True)
        (self.source / "nested").mkdir()
        cfg = deepcopy(self.cfg)
        cfg["services"]["web"]["directory"] = "nested"
        declared = services.declarations(self.consumer, cfg)
        self.assertEqual(
            source_roots.directory(self.consumer, declared["web"], self.env),
            self.source / "nested",
        )
        for directory in ("../outside", "/absolute", ".git/config"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                source_roots.declaration(
                    {"source_root_env": "FIXTURE_SOURCE", "directory": directory}
                )

    def test_broad_missing_and_unsafe_selectors_fail_before_mounting(self):
        spec = {"source_root_env": "FIXTURE_SOURCE"}
        for value in (
            "",
            "relative",
            "/",
            str(Path.home()),
            str(self.base),
            str(self.consumer),
            str(self.source / "missing"),
            str(self.source) + ",extra",
            str(self.source) + "\n",
            str(self.source / "input.lock"),
        ):
            with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                source_roots.root(self.consumer, spec, {"FIXTURE_SOURCE": value})
        for key in ("BAD KEY", "OPTION=VALUE", "", 7):
            with self.subTest(key=key), self.assertRaises(ValueError):
                source_roots.declaration({"source_root_env": key})

    def test_mounts_project_only_selected_canonical_trees(self):
        link = self.base / "alias"
        link.symlink_to(self.source, target_is_directory=True)
        result = source_roots.mounts(
            self.consumer,
            [
                {"source_root_env": "FIXTURE_SOURCE"},
                {"source_root_env": "FIXTURE_ALIAS"},
                {"directory": "."},
            ],
            {**self.env, "FIXTURE_ALIAS": str(link)},
        )
        self.assertEqual(
            result,
            [
                {
                    "source": str(self.source),
                    "target": str(self.source),
                    "read_only": False,
                }
            ],
        )
        self.assertNotIn(str(self.base), [row["source"] for row in result])
        self.assertNotIn(str(self.consumer), [row["source"] for row in result])

    def test_setup_inputs_artifacts_and_identity_use_passed_environment(self):
        spec = workflows.group_spec(self.cfg, "website")
        with patch.dict(os.environ, FIXTURE_SOURCE=str(self.other)):
            inputs = workflows.setup_inputs(self.consumer, spec, self.env)
            artifacts = workflows.artifact_digests(self.consumer, spec, self.env)
            initial = workflows.fingerprint(self.consumer, spec, self.env)
        self.assertEqual(
            inputs["setup:input.lock"], hashlib.sha256(b"website").hexdigest()
        )
        self.assertEqual(inputs["profile:flake.nix"], "profile")
        self.assertEqual(
            artifacts["artifact"], hashlib.sha256(b"website installed").hexdigest()
        )
        (self.other / "input.lock").write_text("unselected edit")
        self.assertEqual(workflows.fingerprint(self.consumer, spec, self.env), initial)
        (self.source / "input.lock").write_text("selected edit")
        self.assertNotEqual(
            workflows.fingerprint(self.consumer, spec, self.env), initial
        )
        (self.other / "input.lock").write_text("selected edit")
        self.assertNotEqual(
            workflows.fingerprint(self.consumer, spec, self.env),
            workflows.fingerprint(
                self.consumer, spec, {**self.env, "FIXTURE_SOURCE": str(self.other)}
            ),
        )
        self.assertEqual(
            workflows.stamp_path(self.consumer, "website").parent.parent.parent.parent,
            self.consumer,
        )
        self.assertFalse((self.source / ".cache").exists())

    def test_symlink_retarget_and_setup_dependency_change_invalidate_selection(self):
        spec = workflows.group_spec(self.cfg, "website")
        (self.other / "input.lock").write_text((self.source / "input.lock").read_text())
        link = self.base / "selected-source"
        link.symlink_to(self.source, target_is_directory=True)
        env = {**self.env, "FIXTURE_SOURCE": str(link)}
        initial = workflows.fingerprint(self.consumer, spec, env)
        lifecycle = services.config_fingerprint(
            self.consumer, self.cfg, env=env, include_setup_inputs=False
        )
        link.unlink()
        link.symlink_to(self.other, target_is_directory=True)
        self.assertNotEqual(workflows.fingerprint(self.consumer, spec, env), initial)
        self.assertNotEqual(
            services.config_fingerprint(
                self.consumer, self.cfg, env=env, include_setup_inputs=False
            ),
            lifecycle,
        )
        cfg = deepcopy(self.cfg)
        cfg["setup"]["common"] = {
            **cfg["setup"]["website"],
            "source_root_env": "FIXTURE_COMMON",
        }
        cfg["setup"]["website"]["depends_on"] = ["common"]
        env = {**self.env, "FIXTURE_COMMON": str(self.other)}
        before = workflows.group_specs(self.consumer, cfg, ["website"], env)
        fingerprint = workflows.fingerprint(self.consumer, before["website"], env)
        (self.other / "input.lock").write_text("shared dependency changed")
        after = workflows.group_specs(self.consumer, cfg, ["website"], env)
        self.assertNotEqual(
            workflows.fingerprint(self.consumer, after["website"], env), fingerprint
        )

    def test_running_lifecycle_keeps_admitted_tools_across_hmr_edits(self):
        admitted = services.config_fingerprint(self.consumer, self.cfg, env=self.env)
        lifecycle = services.config_fingerprint(
            self.consumer, self.cfg, env=self.env, include_setup_inputs=False
        )
        (self.source / "input.lock").write_text("HMR changed setup input")
        self.assertNotEqual(
            services.config_fingerprint(self.consumer, self.cfg, env=self.env), admitted
        )
        self.assertEqual(
            services.config_fingerprint(
                self.consumer, self.cfg, env=self.env, include_setup_inputs=False
            ),
            lifecycle,
        )
        with (
            patch.object(workflows, "configuration", return_value=self.cfg),
            patch.object(
                services.tc,
                "environment",
                side_effect=lambda *args, **kwargs: dict(self.env),
            ),
            patch.object(services.tc, "operation", return_value=nullcontext()),
            patch.object(
                workflows,
                "setup_use",
                side_effect=AssertionError("lifecycle reacquired changed setup"),
            ),
            patch.object(
                chainman, "execute", return_value=subprocess.CompletedProcess([], 0)
            ) as execute,
        ):
            for action, argv in (
                ("_workflow-probe", ["ready"]),
                ("_workflow-shutdown", ["checkpoint"]),
            ):
                self.assertEqual(
                    services.execute_internal(
                        self.consumer, action, ["web", lifecycle]
                    ),
                    0,
                )
                self.assertEqual(
                    execute.call_args.args[:3], (self.consumer, "host", argv)
                )
                self.assertEqual(execute.call_args.kwargs["cwd"], self.source)
                self.assertEqual(execute.call_args.kwargs["pass_fds"], ())
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                services.execute_internal(
                    self.consumer, "_workflow-service", ["web", admitted]
                )

    def test_lifecycle_rejects_changed_source_generation_and_setup_only_selector(self):
        for setup_only in (False, True):
            cfg = deepcopy(self.cfg)
            if setup_only:
                del cfg["services"]["web"]["source_root_env"]
            expected = services.config_fingerprint(
                self.consumer, cfg, env=self.env, include_setup_inputs=False
            )
            for env in (
                {**self.env, "FIXTURE_SOURCE": str(self.other)},
                {**self.env, "FIXTURE_GENERATION": "two"},
            ):
                with self.subTest(setup_only=setup_only, env=env):
                    self.assertNotEqual(
                        services.config_fingerprint(
                            self.consumer, cfg, env=env, include_setup_inputs=False
                        ),
                        expected,
                    )
        cfg = deepcopy(self.cfg)
        cfg["services"]["web"]["shutdown_command"] = ["different-checkpoint"]
        self.assertNotEqual(
            services.config_fingerprint(
                self.consumer, cfg, env=self.env, include_setup_inputs=False
            ),
            services.config_fingerprint(
                self.consumer, self.cfg, env=self.env, include_setup_inputs=False
            ),
        )

    def test_task_bootstrap_mounts_service_and_setup_closure_without_parent_trees(self):
        common = self.base / "common-tools"
        preparation = self.base / "preparation"
        common.mkdir()
        preparation.mkdir()
        cfg = deepcopy(self.cfg)
        cfg["setup"]["common"] = {
            **cfg["setup"]["website"],
            "source_root_env": "FIXTURE_COMMON",
        }
        cfg["setup"]["website"]["depends_on"] = ["common"]
        cfg["tasks"]["prepare"] = {
            "commands": [["true"]],
            "source_root_env": "FIXTURE_PREPARE",
        }
        cfg["tasks"]["work"]["depends_on"] = ["prepare"]
        env = {
            **self.env,
            "FIXTURE_COMMON": str(common),
            "FIXTURE_PREPARE": str(preparation),
            "CHAINMAN_MODE": "container-nix",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                bootstrap_plan.config_inspection, "validated", return_value=cfg
            ),
            patch.object(
                workflows,
                "context_environment",
                side_effect=lambda root, cfg, task, selected: dict(selected),
            ),
            patch.object(
                chainman,
                "profile_environment",
                side_effect=lambda root, profile, selected, *args, **kwargs: dict(
                    selected
                ),
            ),
        ):
            controller, options = bootstrap_plan.plan(
                self.consumer, "_workflow-task", "work"
            )
        self.assertFalse(controller)
        mounts = [
            options[index + 1]
            for index, option in enumerate(options)
            if option == "--mount"
        ]
        self.assertEqual(
            set(mounts),
            {
                f"type=bind,src={path},dst={path}"
                for path in (self.source, common, preparation)
            },
        )
        declarations = [
            json.loads(options[index + 1])
            for index, option in enumerate(options)
            if option == "--transport-declaration"
        ]
        self.assertEqual(
            {row["source"] for value in declarations for row in value["mounts"]},
            {str(self.source), str(common), str(preparation)},
        )

    def test_external_payload_enters_consumer_profile_before_changing_directory(self):
        with (
            patch.object(chainman, "configuration", return_value=self.cfg),
            patch.object(
                chainman,
                "profile_environment",
                side_effect=lambda root, profile, env, *args, **kwargs: dict(env),
            ),
            patch.object(chainman.tc, "host_mode", return_value=True),
            patch.object(chainman.tc, "nix_command", return_value="/fixture/nix"),
            patch.object(
                chainman.tc,
                "managed_run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run,
        ):
            chainman.execute(
                self.consumer,
                "host",
                ["serve", "--flag"],
                env={},
                cwd=self.source,
                gc_root=self.consumer / "profile",
            )
        self.assertEqual(run.call_args.kwargs["cwd"], self.consumer)
        argv = run.call_args.args[0]
        self.assertIn("fixture#host", argv)
        self.assertEqual(argv[-3:], [str(self.source), "serve", "--flag"])
        self.assertEqual(
            run.call_args.kwargs["env"]["CHAINMAN_PROJECT_ROOT"], str(self.consumer)
        )


if __name__ == "__main__":
    unittest.main()
