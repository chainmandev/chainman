"""Real Git policy precedence without requiring a container or project setup."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class GitTextPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Git text policy ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project [literal]?*"
        self.root.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.config = self.base / "global"
        self.config.write_text("")
        self.env.update(
            GIT_CONFIG_GLOBAL=str(self.config),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_ATTR_NOSYSTEM="true",
        )
        self.git("init", "-q")

    def git(self, *args, root=None, env=None):
        return subprocess.check_output(
            ["git", "-C", str(root or self.root), *args], env=env or self.env, text=True
        ).strip()

    def snapshot(self, scope="repository"):
        output = self.base / "snapshot"
        subprocess.run(
            [
                "sh",
                str(SOURCE / "bootstrap/git-text-policy.sh"),
                str(self.root),
                str(output),
                scope,
            ],
            env=self.env,
            check=True,
        )
        # Substitute only the container's bind-mount destination for this host test.
        for name in ("global", "repository", "command"):
            path = output / name
            path.write_text(
                path.read_text().replace("/chainman-git-policy", str(output))
            )
        gitdir = self.git("rev-parse", "--absolute-git-dir")
        pattern = subprocess.check_output(
            ["sed", r"s/[][?*\\]/\\&/g"], input=gitdir, text=True
        )
        env = {k: v for k, v in self.env.items() if not k.startswith("GIT_CONFIG_")}
        env.update(
            GIT_CONFIG_GLOBAL=str(output / "global"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0=f"includeIf.gitdir:{pattern}.path",
            GIT_CONFIG_VALUE_0=str(output / "repository"),
            GIT_CONFIG_KEY_1="include.path",
            GIT_CONFIG_VALUE_1=str(output / "command"),
        )
        return env

    def test_scoped_policy_with_literal_globs_and_linked_worktree(self):
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "Initial",
        )
        linked = self.base / "linked [literal]?*"
        self.git("worktree", "add", "-qb", "linked", str(linked))
        self.root = linked
        attrs = self.base / "external attrs"
        attrs.write_text("*.txt text eol=crlf\n")
        self.git("config", "core.attributesFile", str(attrs))
        self.git("config", "core.autocrlf", "true")
        self.git("config", "core.eol", "crlf")
        self.git("config", "--global", "core.autocrlf", "input")
        self.git("config", "--global", "core.eol", "lf")
        env = self.snapshot()
        # Remove the original local values and file: the scoped snapshot must be
        # doing the work, including when the Git dir lives outside the worktree.
        attrs.unlink()
        for key in ("core.attributesFile", "core.autocrlf", "core.eol"):
            self.git("config", "--unset", key)
        self.assertEqual(self.git("config", "--get", "core.autocrlf", env=env), "true")
        self.assertIn(
            "eol: crlf", self.git("check-attr", "eol", "--", "a.txt", env=env)
        )
        nested = self.root / "nested"
        nested.mkdir()
        self.git("init", "-q", root=nested)
        self.assertEqual(
            self.git("config", "--get", "core.autocrlf", root=nested, env=env), "input"
        )
        self.assertEqual(
            self.git("config", "--get", "core.eol", root=nested, env=env), "lf"
        )
        self.assertIn(
            "eol: unspecified",
            self.git("check-attr", "eol", "--", "a.txt", root=nested, env=env),
        )

    def test_explicit_command_policy_still_overrides_nested_local_settings(self):
        attrs = self.base / "command attrs"
        attrs.write_text("*.txt text eol=crlf\n")
        self.env.update(
            GIT_CONFIG_COUNT="3",
            GIT_CONFIG_KEY_0="core.autocrlf",
            GIT_CONFIG_VALUE_0="true",
            GIT_CONFIG_KEY_1="core.eol",
            GIT_CONFIG_VALUE_1="crlf",
            GIT_CONFIG_KEY_2="core.attributesFile",
            GIT_CONFIG_VALUE_2=str(attrs),
        )
        env = self.snapshot()
        nested = self.root / "nested"
        nested.mkdir()
        self.git("init", "-q", root=nested)
        self.git("config", "core.autocrlf", "input", root=nested)
        self.git("config", "core.eol", "lf", root=nested)
        self.git("config", "core.attributesFile", "/dev/null", root=nested)
        self.assertEqual(
            self.git("config", "--get", "core.autocrlf", root=nested, env=env), "true"
        )
        self.assertEqual(
            self.git("config", "--get", "core.eol", root=nested, env=env), "crlf"
        )
        self.assertIn(
            "eol: crlf",
            self.git("check-attr", "eol", "--", "a.txt", root=nested, env=env),
        )

    def test_unversioned_project_uses_defaults_not_enclosing_repository(self):
        self.git("config", "core.autocrlf", "true")
        self.git("config", "--global", "core.autocrlf", "input")
        nested = self.root / "unversioned"
        nested.mkdir()
        self.root = nested
        self.snapshot(scope="global")
        for name in ("global", "repository"):
            value = self.git(
                "config",
                "--file",
                str(self.base / "snapshot" / name),
                "--get",
                "core.autocrlf",
            )
            self.assertEqual(value, "input")

    def test_disabled_attribute_sources_are_empty_and_preflight_works(self):
        helper = SOURCE / "bootstrap/git-attributes.sh"
        subprocess.run(["sh", str(helper), "--check"], env=self.env, check=True)
        for value in ("", "/dev/null"):
            with self.subTest(value=value):
                self.git("config", "core.attributesFile", value)
                output = self.base / "attributes"
                subprocess.run(
                    ["sh", str(helper), str(self.root), str(output)],
                    env=self.env,
                    check=True,
                )
                self.assertEqual(output.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
