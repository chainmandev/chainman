"""Setup readiness and shared-use contracts exercised with real fixture commands."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workflows
import toolchain as tc


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman workflow ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CHAINMAN_", "TOOLCHAIN_"))
        }
        env["TOOLCHAIN_DOWNLOAD_CACHE"] = str(self.root / "downloads")
        env["CHAINMAN_SETUP"] = "auto"
        context = patch.dict(os.environ, env, clear=True)
        context.start()
        self.addCleanup(context.stop)
        (self.root / "input.lock").write_text("one")
        (self.root / "install.py").write_text("""from pathlib import Path
p=Path('install-count')
p.write_text(str(int(p.read_text())+1 if p.exists() else 1))
Path('installed').write_text(Path('input.lock').read_text())
""")
        (self.root / "task.py").write_text("""import json,sys
from pathlib import Path
assert Path('installed').read_text()==Path('input.lock').read_text()
Path('arguments.json').write_text(json.dumps(sys.argv[1:]))
""")
        self.body = """schema=2
[project]
default_profile="host"
[setup.dependencies]
inputs=["input.lock","install.py"]
artifacts=["installed"]
commands=[["python3","install.py"]]
[tasks.build]
setup=["dependencies"]
commands=[["python3","task.py"]]
"""
        self.write_config()

    def write_config(self):
        (self.root / "chainman.toml").write_text(self.body)

    def test_readiness_uses_service_profile_without_starting_compiler_cache(self):
        import services

        self.body += """
[profiles.host]
compiler_cache=true
[services.demo]
profile="host"
command=["false"]
environment={FIXTURE_SERVICE="declared"}
[services.demo.readiness]
command=["python3","probe.py"]
"""
        self.write_config()
        (self.root / "probe.py").write_text(
            "import os; from pathlib import Path; "
            "assert os.environ['FIXTURE_SERVICE']=='declared'; "
            "Path('probed').touch()"
        )
        cfg = workflows.configuration(self.root)
        fingerprint = services.config_fingerprint(self.root, cfg)
        with (
            patch.object(
                tc,
                "compiler_cache",
                side_effect=AssertionError("probe started compiler"),
            ),
            patch.object(
                services.chainman, "execute", wraps=services.chainman.execute
            ) as execute,
        ):
            self.assertEqual(
                services.execute_internal(
                    self.root, "_workflow-probe", ["demo", fingerprint]
                ),
                0,
            )
        self.assertEqual(execute.call_args.args[1], "host")
        self.assertTrue((self.root / "probed").is_file())

    def run_cli(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(Path(workflows.__file__).with_name("chainman.py")),
                "--root",
                str(self.root),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_http_readiness_rejects_ambiguous_or_unbounded_declarations(self):
        import services

        cfg = {"services": {"web": {"command": ["true"]}}}
        for probe in (
            {},
            {"command": ["true"], "http_get": {"port": 8080}},
            {"http_get": {"port": True}},
            {"http_get": {"port": 65536}},
            {"http_get": {"port": 80, "host": "remote.example"}},
            {"http_get": {"port": 80, "path": "//remote.example/"}},
            {"http_get": {"port": 80, "path": "/health#fragment"}},
            {"http_get": {"port": 80, "status_code": 404}},
            {"http_get": {"port": 80}, "timeout_seconds": 601},
        ):
            with self.subTest(probe=probe), self.assertRaises(ValueError):
                cfg["services"]["web"]["readiness"] = probe
                services.declarations(self.root, cfg)
        cfg["services"]["web"]["readiness"] = {"http_get": {"port": 8080}}
        services.declarations(self.root, cfg)
        self.assertEqual(
            services.http_readiness({"port": 8080}),
            {"port": 8080, "path": "/", "status_code": 200},
        )

    def test_changed_inputs_and_missing_outputs_reinstall_before_task(self):
        for iteration, change in enumerate((None, "input", "output"), 1):
            if change == "input":
                (self.root / "input.lock").write_text("two")
            if change == "output":
                (self.root / "installed").unlink()
            workflows.run(self.root, "build", ["two words", "", "$(literal)"])
            self.assertEqual((self.root / "install-count").read_text(), str(iteration))
            workflows.run(self.root, "build", [])
            self.assertEqual((self.root / "install-count").read_text(), str(iteration))

    def test_declared_environment_inputs_invalidate_setup_and_dependents(self):
        self.body = self.body.replace(
            "[setup.dependencies]",
            '[setup.dependencies]\nenvironment_inputs=["FIXTURE_SEED"]',
        )
        self.body += '\n[setup.downstream]\ndepends_on=["dependencies"]\ninputs=["input.lock"]\nartifacts=["dependent"]\ncommands=[["python3","-c","from pathlib import Path; p=Path(\\"dependent\\"); p.write_text(str(int(p.read_text())+1 if p.exists() else 1))"]]\n'
        self.write_config()
        for index, value in enumerate((None, "", "literal $(seed) secret"), 1):
            if value is None:
                os.environ.pop("FIXTURE_SEED", None)
            else:
                os.environ["FIXTURE_SEED"] = value
            result = self.run_cli("setup", "downstream")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.root / "install-count").read_text(), str(index))
            self.assertEqual((self.root / "dependent").read_text(), str(index))
            self.assertEqual(self.run_cli("setup-status", "downstream").returncode, 0)
            os.environ["UNDECLARED_SEED"] = str(index)
            self.assertEqual(self.run_cli("setup", "downstream").returncode, 0)
            self.assertEqual((self.root / "install-count").read_text(), str(index))
        for stamp in (self.root / ".cache/toolchain/setup-groups").glob("*.json"):
            self.assertNotIn("literal $(seed) secret", stamp.read_text())

    def test_environment_fingerprint_uses_effective_values_and_unset(self):
        self.body = self.body.replace(
            "[setup.dependencies]",
            '[setup.dependencies]\nenvironment_inputs=["FIXTURE_SEED", "FIXTURE_UNSET"]',
        )
        self.body += '\n[environment]\nfiles=[{path="project.env",override=true}]\nunset=["FIXTURE_UNSET"]\n[environment.defaults]\nFIXTURE_DEFAULT="default"\n[environment.values]\nFIXTURE_SEED="{env:FIXTURE_DEFAULT}"\n'
        (self.root / "project.env").write_text("FIXTURE_DEFAULT=from file\n")
        with (self.root / "install.py").open("a") as script:
            script.write(
                "import os\nPath('effective').write_text(os.environ['FIXTURE_SEED'])\nassert 'FIXTURE_UNSET' not in os.environ\n"
            )
        self.write_config()
        for value in ("one", "two"):
            os.environ.update(FIXTURE_SEED=value, FIXTURE_UNSET=value)
            result = self.run_cli("setup")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((self.root / "install-count").read_text(), "1")
            self.assertEqual((self.root / "effective").read_text(), "from file")
        (self.root / "project.env").write_text("FIXTURE_DEFAULT=changed\n")
        self.assertEqual(self.run_cli("setup-status").returncode, 1)
        self.assertEqual(self.run_cli("setup").returncode, 0)
        self.assertEqual((self.root / "effective").read_text(), "changed")

    def test_setup_fingerprint_and_execution_share_profile_environment_resolution(self):
        self.body = self.body.replace(
            "[setup.dependencies]",
            '[setup.dependencies]\nenvironment_inputs=["FIXTURE_SEED"]',
        )
        self.write_config()
        with (self.root / "install.py").open("a") as script:
            script.write(
                "import os\nPath('effective').write_text(os.environ['FIXTURE_SEED'])\n"
            )
        for index, value in enumerate(("profile one", "profile two"), 1):
            with patch(
                "chainman.profile",
                return_value=(None, {"environment": {"FIXTURE_SEED": value}}),
            ):
                workflows.run(self.root, "setup", [])
                self.assertEqual((self.root / "effective").read_text(), value)
                self.assertEqual((self.root / "install-count").read_text(), str(index))
                workflows.run(self.root, "setup", [])
                self.assertEqual((self.root / "install-count").read_text(), str(index))

    def test_context_reaches_setup_and_dependencies_without_process_mutation(self):
        self.body = self.body.replace(
            "[setup.dependencies]",
            '[setup.dependencies]\nenvironment_inputs=["FIXTURE_SEED"]',
        )
        with (self.root / "install.py").open("a") as script:
            script.write(
                "import os\nPath('effective').write_text(os.environ['FIXTURE_SEED'])\n"
            )
        self.body += '\n[tasks.test]\ndepends_on=["build"]\ncontext_environment={FIXTURE_SEED="literal $(not-a-shell)"}\ncommands=[["python3","-c","import os; from pathlib import Path; Path(\\"task-context\\").write_text(os.environ[\\"FIXTURE_SEED\\"])"]]\n'
        self.write_config()
        os.environ["FIXTURE_SEED"] = "caller"
        workflows.run(self.root, "test", [])
        self.assertEqual(
            (self.root / "effective").read_text(), "literal $(not-a-shell)"
        )
        self.assertEqual(
            (self.root / "task-context").read_text(), "literal $(not-a-shell)"
        )
        self.assertEqual(os.environ["FIXTURE_SEED"], "caller")
        workflows.run(self.root, "test", [])
        self.assertEqual((self.root / "install-count").read_text(), "1")
        workflows.run(self.root, "build", [])
        self.assertEqual((self.root / "effective").read_text(), "caller")
        self.assertEqual((self.root / "install-count").read_text(), "2")

    def test_literal_task_mode_selects_files_before_caller_provider_inputs(self):
        self.body += '\n[environment]\nfiles=[{path="provider.env",required=true,when={AUTH_MODE="provider"}}]\n'
        self.body = self.body.replace(
            "[tasks.build]", '[tasks.build]\ncontext_environment={AUTH_MODE="local"}'
        )
        self.write_config()
        cfg = workflows.configuration(self.root)
        selected = workflows.context_environment(
            self.root, cfg, "build", {"AUTH_MODE": "provider"}
        )
        self.assertEqual(selected["AUTH_MODE"], "local")
        self.assertNotIn("PROVIDER_INPUT", selected)
        (self.root / "provider.env").write_text("PROVIDER_INPUT=must-not-leak\n")
        self.assertEqual(
            workflows.context_environment(
                self.root, cfg, "build", {"AUTH_MODE": "provider"}
            ),
            selected,
        )

    def test_internal_workflow_uses_planned_provider_file_fingerprint(self):
        import services

        self.body += '\n[environment]\nfiles=[{path="provider.env",required=true,when={AUTH_MODE="provider"}}]\n'
        self.write_config()
        provider = self.root / "provider.env"
        provider.write_text("PROVIDER_INPUT=original\n")
        os.environ["AUTH_MODE"] = "provider"
        cfg = workflows.configuration(self.root)
        expected = services.config_fingerprint(self.root, cfg, env=dict(os.environ))
        self.assertEqual(
            services.execute_internal(self.root, "_workflow-task", ["build", expected]),
            0,
        )
        self.assertTrue((self.root / "arguments.json").is_file())
        provider.write_text("PROVIDER_INPUT=changed\n")
        with self.assertRaisesRegex(
            ValueError, "Service inputs changed after planning"
        ):
            services.execute_internal(self.root, "_workflow-task", ["build", expected])

    def test_internal_services_reconstruct_requesting_task_context_in_target_lane(self):
        import services

        self.body += """
[environment]
files=[{path="local.env",when={AUTH_MODE="local"}},{path="provider.env",required=true,when={AUTH_MODE="provider"}}]
[tasks.main]
depends_on=["build"]
services=["demo"]
commands=[["true"]]
context_environment={AUTH_MODE="local",FIXTURE_SEED="{env:HOST_SEED}"}
[services.demo]
command=["true"]
environment={FIXTURE_SERVICE="{env:FIXTURE_SEED}"}
readiness={command=["python3","probe.py"]}
"""
        (self.root / "local.env").write_text("HOST_SEED=from file\n")
        (self.root / "probe.py").write_text(
            "import os; from pathlib import Path; "
            "Path('probed').write_text(os.environ['FIXTURE_SERVICE'])"
        )
        with (self.root / "install.py").open("a") as script:
            script.write(
                "import os\nPath('effective').write_text(os.environ['FIXTURE_SEED'])\n"
            )
        self.write_config()
        os.environ.update(
            AUTH_MODE="provider",
            CHAINMAN_CONTEXT_TASK="main",
            CHAINMAN_COMPILER_OWNER="inherited-owner",
        )
        cfg = workflows.configuration(self.root)
        selected = workflows.context_environment(
            self.root, cfg, "main", tc.environment(self.root)
        )
        expected = services.config_fingerprint(self.root, cfg, env=selected)
        endpoints = []
        real_setup_use = workflows.setup_use

        def setup_use(root, cfg, requested, env, **kwargs):
            self.assertTrue(tc._operation_id)
            self.assertTrue(env["SCCACHE_SERVER_UDS"].endswith(tc._operation_id))
            self.assertNotIn("CHAINMAN_COMPILER_OWNER", env)
            self.assertEqual(env["FIXTURE_SEED"], "from file")
            endpoints.append(env["SCCACHE_SERVER_UDS"])
            return real_setup_use(root, cfg, requested, env, **kwargs)

        with patch.object(workflows, "setup_use", side_effect=setup_use):
            for _ in range(2):
                self.assertEqual(services.prepare_requested(self.root, ["main"]), 0)
                for action, name in (
                    ("_workflow-prepare", "main"),
                    ("_workflow-task", "build"),
                    ("_workflow-service", "demo"),
                    ("_workflow-probe", "demo"),
                ):
                    self.assertEqual(
                        services.execute_internal(self.root, action, [name, expected]),
                        0,
                    )
        self.assertEqual(len(endpoints), 10)
        self.assertEqual(len(set(endpoints)), len(endpoints))
        self.assertEqual((self.root / "effective").read_text(), "from file")
        self.assertEqual((self.root / "probed").read_text(), "from file")
        self.assertEqual(os.environ["AUTH_MODE"], "provider")
        self.assertNotIn("FIXTURE_SEED", os.environ)

    def test_context_conflicts_fail_before_setup_and_project_policy_wins(self):
        self.body += 'context_environment={FIXTURE_SEED="dependency"}\n[tasks.test]\ndepends_on=["build"]\ncontext_environment={FIXTURE_SEED="different"}\n'
        self.write_config()
        with self.assertRaisesRegex(ValueError, "Conflicting task context"):
            workflows.run(self.root, "test", [])
        self.assertFalse((self.root / "installed").exists())
        self.body = self.body.replace(
            'FIXTURE_SEED="different"', 'FIXTURE_SEED="dependency"'
        )
        self.body += '\n[environment.values]\nFIXTURE_SEED="project policy"\n'
        self.write_config()
        cfg = workflows.configuration(self.root)
        selected = workflows.context_environment(
            self.root, cfg, "test", {"FIXTURE_SEED": "caller"}
        )
        self.assertEqual(selected["FIXTURE_SEED"], "project policy")

    def test_context_rejects_managed_variables_and_nonliteral_values(self):
        for value in (
            "[]",
            '{CHAINMAN_ROOT="bad"}',
            '{RUSTC_WRAPPER="bad"}',
            "{FIXTURE_SEED=4}",
        ):
            with self.subTest(value=value):
                (self.root / "chainman.toml").write_text(
                    self.body + "context_environment=" + value + "\n"
                )
                with self.assertRaises(ValueError):
                    workflows.configuration(self.root)

    def test_environment_inputs_reject_patterns_duplicates_and_internal_names(self):
        for value in (
            '"NAME"',
            '["NAME", "NAME"]',
            '["NAME_*"]',
            "[1]",
            '["CHAINMAN_ROOT"]',
        ):
            with self.subTest(value=value):
                (self.root / "chainman.toml").write_text(
                    self.body.replace(
                        "[setup.dependencies]",
                        f"[setup.dependencies]\nenvironment_inputs={value}",
                    )
                )
                with self.assertRaises(ValueError):
                    workflows.configuration(self.root)

    def test_exclusive_service_access_requires_services_and_a_boolean(self):
        for value in ("true", '"yes"'):
            with self.subTest(value=value):
                self.body += f"\nexclusive_services={value}\n"
                self.write_config()
                with self.assertRaisesRegex(ValueError, "exclusive_services"):
                    workflows.configuration(self.root)
                self.body = self.body.rsplit("\nexclusive_services=", 1)[0]

    def test_public_run_preserves_literal_arguments(self):
        result = self.run_cli("run", "build", "--", "two words", "", "$(literal)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads((self.root / "arguments.json").read_text()),
            ["two words", "", "$(literal)"],
        )

    def test_changed_digest_artifact_rebuilds_even_when_all_inputs_are_unchanged(self):
        self.body = self.body.replace(
            'artifacts=["installed"]', 'artifacts=[{path="installed",digest=true}]'
        )
        self.write_config()
        self.assertEqual(self.run_cli("run", "build").returncode, 0)
        (self.root / "installed").write_text("another workflow's build variant")
        result = self.run_cli("run", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "installed").read_text(), "one")
        self.assertEqual((self.root / "install-count").read_text(), "2")
        self.assertEqual(self.run_cli("run", "build").returncode, 0)
        self.assertEqual((self.root / "install-count").read_text(), "2")

    def test_declared_python_interpreter_remains_valid_after_setup(self):
        self.body = self.body.replace(
            'artifacts=["installed"]',
            'artifacts=["installed",{path=".venv/bin/python",interpreter="python"}]',
        )
        with (self.root / "install.py").open("a") as script:
            script.write(
                "import os\np=Path('.venv/bin/python');p.parent.mkdir(parents=True,exist_ok=True)\np.unlink(missing_ok=True)\np.symlink_to(os.environ['UV_PYTHON'])\n"
            )
        self.write_config()
        os.environ["UV_PYTHON"] = sys.executable
        self.assertTrue(sys.executable.startswith("/nix/store/"))
        for _ in range(2):
            result = self.run_cli("run", "build")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.run_cli("setup-status").returncode, 0)
        self.assertEqual((self.root / "install-count").read_text(), "1")
        interpreter = self.root / ".venv/bin/python"
        interpreter.unlink()
        interpreter.symlink_to(self.root / "input.lock")
        self.assertEqual(self.run_cli("setup-status").returncode, 1)
        self.assertEqual((self.root / "install-count").read_text(), "1")
        self.assertEqual(self.run_cli("run", "build").returncode, 0)
        self.assertEqual((self.root / "install-count").read_text(), "2")
        for declaration in (
            '".venv/bin/python"',
            '{path=".venv/bin/python",digest=true}',
        ):
            (self.root / "chainman.toml").write_text(
                self.body.replace(
                    '{path=".venv/bin/python",interpreter="python"}', declaration
                )
            )
            with self.assertRaisesRegex(ValueError, "symlink"):
                workflows.configuration(self.root)
        self.write_config()
        interpreter.unlink()
        interpreter.parent.rmdir()
        interpreter.parent.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "symlink"):
            workflows.configuration(self.root)

    def test_interpreter_declarations_preserve_lexical_path_restrictions(self):
        for path in ("", ".", "..", ".git", "a/..", "a/.git", "../outside", "/outside"):
            with self.subTest(path=path):
                artifact = "{path=" + json.dumps(path) + ',interpreter="python"}'
                (self.root / "chainman.toml").write_text(
                    self.body.replace(
                        'artifacts=["installed"]', "artifacts=[" + artifact + "]"
                    )
                )
                with self.assertRaises(ValueError):
                    workflows.configuration(self.root)

    def test_nix_python_selection_is_required_before_installation(self):
        self.body = self.body.replace(
            'artifacts=["installed"]',
            'artifacts=[{path=".venv/bin/python",interpreter="python"}]',
        )
        self.write_config()
        for value in (None, "/usr/bin/python3", "/nix/store/missing-python"):
            with self.subTest(value=value):
                os.environ.pop("UV_PYTHON", None)
                if value is not None:
                    os.environ["UV_PYTHON"] = value
                result = self.run_cli("setup")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("Traceback", result.stderr)
                self.assertRegex(result.stderr, "Python readiness|pinned Nix shell")
                self.assertFalse((self.root / "installed").exists())
                self.assertFalse((self.root / "install-count").exists())

    def test_setup_group_can_be_requested_explicitly(self):
        result = self.run_cli("setup", "dependencies")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "installed").exists())
        self.assertFalse((self.root / "arguments.json").exists())

    def test_setup_status_never_installs_or_blesses_stale_outputs(self):
        self.assertFalse((self.root / ".cache").exists())
        result = self.run_cli("setup-status", "dependencies")
        self.assertFalse((self.root / ".cache").exists())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["groups"], {"dependencies": False})
        self.assertEqual(
            json.loads(result.stdout)["details"]["dependencies"]["reason"],
            "not-installed",
        )
        self.assertFalse((self.root / "install-count").exists())
        self.assertEqual(self.run_cli("setup").returncode, 0)
        self.assertEqual(self.run_cli("setup-status").returncode, 0)
        (self.root / "input.lock").write_text("changed")
        last_used = next((self.root / ".cache/toolchain/work").glob("*/last-used"))
        before = last_used.stat().st_mtime_ns
        result = self.run_cli("setup-status")
        self.assertEqual(before, last_used.stat().st_mtime_ns)
        self.assertEqual(
            json.loads(result.stdout)["details"]["dependencies"]["reason"],
            "inputs-changed",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.root / "install-count").read_text(), "1")
        self.assertFalse(json.loads(result.stdout)["current"])

    def test_setup_excludes_declared_generated_dependency_trees(self):
        self.body = self.body.replace(
            'inputs=["input.lock","install.py"]',
            'inputs=["**/*.lock","install.py"]\nexclude_inputs=["node_modules/**"]',
        )
        self.write_config()
        with (self.root / "install.py").open("a") as script:
            script.write(
                "Path('node_modules').mkdir(exist_ok=True)\nPath('node_modules/download.lock').write_text('generated')\n"
            )
        self.assertEqual(self.run_cli("setup").returncode, 0)
        (self.root / "node_modules/download.lock").write_text("another download")
        self.assertEqual(self.run_cli("setup-status").returncode, 0)
        (self.root / "input.lock").write_text("source changed")
        self.assertEqual(self.run_cli("setup-status").returncode, 1)

    def test_automatic_prune_runs_only_without_active_work(self):
        obsolete = self.root / ".cache/toolchain/work/old-context"
        obsolete.mkdir(parents=True)
        (obsolete / "output").write_text("disposable")
        # A recent context over budget must still survive an active operation.
        self.body += "\n[cache]\nbuild_limit_gib=0\nstale_hours=48\n"
        self.write_config()
        with tc.operation(self.root, exclusive=False):
            result = self.run_cli("run", "build")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(obsolete.exists())
        result = self.run_cli("run", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(obsolete.exists())

    def test_reserved_setup_task_is_rejected_instead_of_silently_skipped(self):
        self.body += '\n[tasks.setup]\ncommands=[["false"]]\n'
        self.write_config()
        for args in [("run", "setup"), ("setup",)]:
            result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Task name 'setup' is reserved", result.stderr)
            self.assertFalse((self.root / "installed").exists())

    def test_aggregate_task_runs_dependencies_once_without_a_noop_command(self):
        self.body += '\n[tasks.all]\ndepends_on=["build"]\n'
        self.write_config()
        result = self.run_cli("run", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "install-count").read_text(), "1")
        self.assertTrue((self.root / "arguments.json").exists())

    def test_exclusive_maintenance_refuses_another_active_task(self):
        self.body += '\n[tasks.clean]\nexclusive=true\ncommands=[["python3","-c","from pathlib import Path; Path(\\"cleaned\\").touch()"]]\n'
        self.write_config()
        with tc.operation(self.root, exclusive=False, new_execution=True):
            result = self.run_cli("run", "clean")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "cleaned").exists())
        result = self.run_cli("run", "clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "cleaned").exists())

    def test_task_without_setup_remains_available_when_outputs_are_missing(self):
        self.body += '\n[tasks.inspect]\ncommands=[["python3","-c","print(42)"]]\n'
        self.write_config()
        result = self.run_cli("run", "inspect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "42")
        self.assertFalse((self.root / "installed").exists())

    def test_invalid_deadline_override_refuses_to_start_the_task(self):
        self.body += '\n[tasks.bounded]\ntimeout_seconds=600\ntimeout_env="APP_TEST_TIMEOUT"\ncommands=[["python3","-c","from pathlib import Path; Path(\\"started\\").touch()"]]\n'
        self.write_config()
        for value in ("0", "-1", "1.5", "86401", "unlimited"):
            with patch.dict(os.environ, APP_TEST_TIMEOUT=value):
                result = self.run_cli("run", "bounded")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("APP_TEST_TIMEOUT must be an integer", result.stderr)
            self.assertFalse((self.root / "started").exists())

    def test_reinstall_is_refused_while_another_task_uses_outputs(self):
        workflows.run(self.root, "build", [])
        cfg = workflows.configuration(self.root)
        with workflows.setup_use(
            self.root, cfg, ["dependencies"], tc.environment(self.root)
        ):
            (self.root / "input.lock").write_text("two")
            with self.assertRaisesRegex(
                ValueError, "another task uses installed artifacts"
            ):
                workflows.run(self.root, "build", [])
        self.assertEqual((self.root / "install-count").read_text(), "1")
        workflows.run(self.root, "build", [])
        self.assertEqual((self.root / "installed").read_text(), "two")

    def test_missing_artifact_and_changed_input_do_not_record_readiness(self):
        for script, error in [
            ("pass\n", "did not create"),
            (
                'from pathlib import Path\nPath("installed").touch()\nPath("input.lock").write_text("changed")\n',
                "inputs changed",
            ),
        ]:
            with self.subTest(error=error):
                (self.root / "install.py").write_text(script)
                with self.assertRaisesRegex(ValueError, error):
                    workflows.run(self.root, "build", [])
                self.assertFalse(
                    workflows.stamp_path(self.root, "dependencies").exists()
                )

    def test_unknown_reference_and_cycle_fail_before_installation(self):
        for extra in ['depends_on=["missing"]\n', 'depends_on=["build"]\n']:
            self.write_config()
            with (self.root / "chainman.toml").open("a") as out:
                out.write(extra)
            result = self.run_cli("run", "build")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "install-count").exists())

    def test_dependency_task_runs_once_before_requested_task(self):
        self.body = self.body.replace(
            "[tasks.build]\n", '[tasks.build]\ndepends_on=["prepare"]\n'
        )
        self.body += '\n[tasks.prepare]\ncommands=[["python3","prepare.py"]]\n'
        (self.root / "prepare.py").write_text(
            'from pathlib import Path\nPath("prepared").touch()\n'
        )
        self.write_config()
        workflows.run(self.root, "build", [])
        self.assertTrue((self.root / "prepared").exists())

    def test_task_child_retains_setup_lease_after_parent_is_killed(self):
        self.body += (
            '\n[tasks.hold]\nsetup=["dependencies"]\ncommands=[["python3","hold.py"]]\n'
        )
        self.write_config()
        (self.root / "hold.py").write_text(
            'import time\nfrom pathlib import Path\nPath("ready").touch()\ntime.sleep(30)\n'
        )
        parent = subprocess.Popen(
            [
                sys.executable,
                str(Path(workflows.__file__).with_name("chainman.py")),
                "--root",
                str(self.root),
                "run",
                "hold",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not (self.root / "ready").exists()
                and parent.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue((self.root / "ready").exists())
            parent.kill()
            parent.wait(timeout=5)
            (self.root / "input.lock").write_text("changed")
            result = self.run_cli("run", "build")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another task uses installed artifacts", result.stderr)
            self.assertEqual((self.root / "install-count").read_text(), "1")
        finally:
            try:
                os.killpg(parent.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            parent.wait(timeout=5)
            parent.stderr.close()

    def test_serial_group_excludes_peers_but_not_other_groups(self):
        workflows.run(self.root, "setup", [])
        self.body = self.body.replace(
            "[tasks.build]", '[tasks.build]\nserial_group="data"'
        )
        self.write_config()
        with workflows.serial_use(self.root, {"serial_group": "data"}):
            result = self.run_cli("run", "build")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Task group data is busy", result.stderr)
            with workflows.serial_use(self.root, {"serial_group": "other"}):
                pass
        self.assertEqual(self.run_cli("run", "build").returncode, 0)

    def test_serial_group_released_before_wait_for_services(self):
        self.body += '\n[services.database]\ncommand=["true"]\n'
        self.body = self.body.replace(
            "[tasks.build]",
            '[tasks.build]\nserial_group="data"\nservices=["database"]\nwait_for_services=true',
        )
        self.write_config()

        def waiting():
            self.assertEqual(json.loads(progress.read_text())["phase"], "ready")
            with workflows.serial_use(self.root, {"serial_group": "data"}):
                pass

        progress = self.root / "progress.json"
        with (
            patch.dict(
                os.environ,
                CHAINMAN_DEV_CHANNEL=str(self.root),
                CHAINMAN_DEV_OPERATION="c" * 32,
                CHAINMAN_DEV_TASK="build",
            ),
            patch.object(workflows, "wait_for_services", side_effect=waiting) as wait,
        ):
            workflows.run(self.root, "build", [], service_context=True)
            (self.root / "task.py").write_text("raise SystemExit(17)")
            with self.assertRaises(subprocess.CalledProcessError):
                workflows.run(self.root, "build", [], service_context=True)
            self.assertEqual(json.loads(progress.read_text())["phase"], "preparing")
        wait.assert_called_once()

    def test_serial_group_child_retains_lease_after_parent_death(self):
        self.body += (
            '\n[tasks.hold]\nserial_group="data"\ncommands=[["python3","hold.py"]]\n'
        )
        self.body = self.body.replace(
            "[tasks.build]", '[tasks.build]\nserial_group="data"'
        )
        self.write_config()
        workflows.run(self.root, "setup", [])
        (self.root / "hold.py").write_text(
            'import time\nfrom pathlib import Path\nPath("ready").touch()\ntime.sleep(30)\n'
        )
        parent = subprocess.Popen(
            [
                sys.executable,
                str(Path(workflows.__file__).with_name("chainman.py")),
                "--root",
                str(self.root),
                "run",
                "hold",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not (self.root / "ready").exists()
                and parent.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertTrue((self.root / "ready").exists())
            parent.kill()
            parent.wait(timeout=5)
            result = self.run_cli("run", "build")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Task group data is busy", result.stderr)
        finally:
            try:
                os.killpg(parent.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            parent.wait(timeout=5)
            parent.stderr.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.run_cli("run", "build")
            if result.returncode == 0:
                break
            time.sleep(0.02)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_serial_group_requires_a_safe_name(self):
        for value in ('"../escape"', '""', "true"):
            with self.subTest(value=value):
                self.body = (
                    self.body.split("[tasks.build]")[0]
                    + '[tasks.build]\ncommands=[["true"]]\nserial_group='
                    + value
                    + "\n"
                )
                self.write_config()
                with self.assertRaisesRegex(ValueError, "Workflow names"):
                    workflows.configuration(self.root)

    def test_resource_budget_reaches_the_project_command(self):
        self.body += '\n[resources]\njob_variables=["CARGO_BUILD_JOBS"]\nmax_jobs=1\n'
        self.write_config()
        with (self.root / "task.py").open("a") as handle:
            handle.write(
                '\nimport os\nPath("jobs").write_text(os.environ["CARGO_BUILD_JOBS"])\n'
            )
        with patch.dict(os.environ):
            os.environ.pop("CARGO_BUILD_JOBS", None)
            result = self.run_cli("run", "build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "jobs").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
