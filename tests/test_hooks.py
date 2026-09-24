"""Declarative hook contract; native execution is qualified by hooks-test."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import os

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hooks
import chainman
import trojan_source
import reentry
import hook_worker


class HookDeclarations(unittest.TestCase):
    def test_preset_is_formatting_only_and_extensible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chainman.toml").write_text(
                'schema=3\n[hooks]\nenabled=true\nconfig="lefthook.yml"\n'
            )
            (root / "lefthook.yml").write_text("pre-push: {}\n")
            data = json.loads(hooks.effective(root, root / "output").read_text())
            self.assertTrue(data["no_auto_install"])
            self.assertEqual(set(data["pre-commit"]["commands"]), {"format-staged"})
            self.assertEqual(set(data["pre-push"]["commands"]), {"trojan-source"})
            self.assertEqual(data["extends"], [str(root / "lefthook.yml")])

    def test_config_reentry_skips_setup_and_profile_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(reentry, "validate") as validate,
                patch.object(reentry.chainman, "main", return_value=0) as main,
                patch.object(
                    reentry.workflows,
                    "configuration",
                    side_effect=AssertionError("transport admission"),
                ),
                patch.dict(os.environ, {"CHAINMAN_ACTIVE_MODE": "container-nix"}),
            ):
                self.assertEqual(
                    reentry.main([str(root), "--entry", "hooks", "config"]), 0
                )
                validate.assert_called_once_with(root)
                main.assert_called_once_with(["--root", str(root), "_hooks-config"])

    def test_config_requires_enabled_hooks_and_exact_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                hook_worker.config_inspection, "validated", return_value={}
            ):
                with self.assertRaisesRegex(ValueError, "enabled=true"):
                    hook_worker.inspect_config(root, [])
            with self.assertRaisesRegex(ValueError, "usage"):
                hook_worker.inspect_config(root, ["install"])

    def test_unknown_hook_settings_are_rejected(self):
        with self.assertRaises(ValueError):
            hooks.declaration({"hooks": {"enabled": "yes"}})
        with self.assertRaises(ValueError):
            hooks.declaration({"hooks": {"legacy": True}})

    def test_internal_complete_setup_refuses_hook_installation_before_side_effects(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chainman.toml").write_text("""schema=3
[hooks]
enabled=true
[project]
default_profile="host"
[setup.one]
inputs=["chainman.toml"]
artifacts=["ready"]
commands=[["sh","-c","touch ready"]]
""")
            self.assertNotEqual(chainman.main(["--root", str(root), "setup"]), 0)
            self.assertFalse((root / "ready").exists())
            self.assertEqual(
                chainman.main(["--root", str(root), "setup", "--no-hooks"]), 0
            )
            self.assertTrue((root / "ready").exists())

    def test_binary_executable_classification_is_narrow(self):
        self.assertTrue(trojan_source.binary_executable(b"\x7fELF\x00\xff"))
        self.assertFalse(trojan_source.binary_executable(b"\x00text"))
        self.assertFalse(trojan_source.binary_executable(b"MZshort"))

    def test_media_classification_requires_matching_extension_and_signature(self):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff"
        self.assertTrue(trojan_source.binary_asset("icon.png", png))
        self.assertFalse(trojan_source.binary_asset("source.ts", png))
        self.assertFalse(trojan_source.binary_asset("icon.png", b"#!/bin/sh\nexit 0\n"))
        audio = b"ID3\x03\0\0\0\0\0\0\xff\xfb\x90\0"
        self.assertTrue(trojan_source.binary_asset("beep.mp3", audio))
        self.assertFalse(trojan_source.binary_asset("beep.mp3", b"ID3short"))
        self.assertFalse(
            trojan_source.binary_asset("beep.mp3", audio[:6] + b"\x7f" * 4 + audio[10:])
        )
