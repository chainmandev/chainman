"""Presentation is explicit data; workflow completion is a separate signal."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import development_status as status
import configuration


class DevelopmentStatusTests(unittest.TestCase):
    def test_selected_values_expand_without_environment_dump_or_label_shadowing(self):
        result = status.resolve(
            {
                "title": "Demo",
                "urls": {"Browser": "{env:URL}"},
                "details": {"value": "{env:value}"},
            },
            Path("/project"),
            {
                "URL": "http://localhost:1234",
                "value": "declared",
                "SECRET": "not printed",
            },
            "dev",
        )
        self.assertEqual(result["details"], {"value": "declared"})
        self.assertNotIn("not printed", json.dumps(result))
        configuration.fields(
            {"presentation": {"title": "Demo", "urls": {"Browser": "{env:URL}"}}},
            configuration.FIELDS["tasks"],
            "tasks.dev",
        )

    def test_rejects_unsafe_or_unknown_presentation(self):
        for value in (
            {"unknown": "field"},
            {"title": "\x1b[2J"},
            {"urls": {"Browser": "file:///secret"}},
            {"urls": {"Browser": "https://user:password@example.org"}},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                status.resolve(value, Path("/project"), {}, "dev")

    def test_atomic_observation_only_for_selected_task(self):
        with tempfile.TemporaryDirectory(prefix="development channel ") as directory:
            path = Path(directory) / "progress.json"
            with patch.dict(
                os.environ,
                CHAINMAN_DEV_CHANNEL=directory,
                CHAINMAN_DEV_OPERATION="a" * 32,
                CHAINMAN_DEV_TASK="dev",
            ):
                status.publish("unrelated", "ready")
                self.assertFalse(path.exists())
                status.publish("dev", "preparing")
                self.assertEqual(json.loads(path.read_text())["phase"], "preparing")
                status.publish("dev", "ready")
                self.assertEqual(json.loads(path.read_text())["operation"], "a" * 32)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_container_channel_mount_preserves_literal_arguments_and_stdin(self):
        with tempfile.TemporaryDirectory(prefix="development channel ") as directory:
            root = Path(directory)
            engine = root / "engine"
            engine.write_text(
                f"#!{sys.executable}\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\nprint(sys.stdin.read(), end='')\n"
            )
            engine.chmod(0o700)
            result = subprocess.run(
                [
                    "sh",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "bootstrap/setup-prompt.sh"
                    ),
                    str(engine),
                    "argument with $() spaces",
                ],
                env=dict(
                    os.environ,
                    CHAINMAN_SETUP="error",
                    CHAINMAN_DEV_CHANNEL=directory,
                    CHAINMAN_DEV_OPERATION="b" * 32,
                    CHAINMAN_DEV_TASK="dev",
                ),
                input="literal stdin\n",
                text=True,
                capture_output=True,
                check=True,
            )
            args, stdin = result.stdout.split("\n", 1)
            self.assertIn(
                "type=bind,src=" + directory + ",dst=/chainman-development",
                json.loads(args),
            )
            self.assertEqual(json.loads(args)[-1], "argument with $() spaces")
            self.assertEqual(stdin, "literal stdin\n")
