"""Container port projection uses declared host inputs as data."""

import os
import unittest
from unittest.mock import patch

import test_workflows
import bootstrap_plan


class TransportPortTests(unittest.TestCase):
    setUp = test_workflows.WorkflowTests.setUp
    write_config = test_workflows.WorkflowTests.write_config

    def test_dynamic_port_uses_effective_environment_and_rejects_non_ports(self):
        self.body += """
[environment.defaults]
DOCS_PORT="4321"
[tasks.preview]
commands=[["true"]]
transport={ports=["127.0.0.1:{env:DOCS_PORT}:{env:DOCS_PORT}"]}
"""
        self.write_config()
        for value in ("4321", "54321"):
            with patch.dict(os.environ, DOCS_PORT=value):
                controller, options = bootstrap_plan.plan(self.root, "run", "preview")
                self.assertFalse(controller)
                self.assertIn(f"127.0.0.1:{value}:{value}/tcp", options)
        for value in ("0", "65536", "123:456", "$(command)", "1\n2", ""):
            with (
                patch.dict(os.environ, DOCS_PORT=value),
                self.assertRaisesRegex(ValueError, "port"),
            ):
                bootstrap_plan.plan(self.root, "run", "preview")

    def test_port_planner_uses_only_declared_host_inputs_as_data(self):
        self.body += """
[environment]
pass=["DOCS_PORT"]
[environment.defaults]
DOCS_PORT="4321"
[tasks.preview]
commands=[["true"]]
transport={ports=["127.0.0.1:{env:DOCS_PORT}:{env:DOCS_PORT}"]}
"""
        self.write_config()
        snapshot = self.root / "planner"
        snapshot.mkdir()
        (snapshot / "host-environment").write_bytes(
            b"DOCS_PORT=54321\0SECRET=must-not-appear\0CHAINMAN_MODE=host\0"
        )
        with patch.dict(
            os.environ,
            CHAINMAN_BOOTSTRAP_INPUTS=str(snapshot),
            CHAINMAN_MODE="container-nix",
        ):
            _, options = bootstrap_plan.plan(self.root, "run", "preview")
        self.assertIn("127.0.0.1:54321:54321/tcp", options)
        self.assertNotIn("must-not-appear", str(options))
