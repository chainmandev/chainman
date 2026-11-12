"""GitHub API economy: credentials, quota, durable immutable answers and Git refs."""

from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from email.message import Message

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry as registry_module

TOKEN = "sentinel_discovered_OnlyFixture123"
COMMIT = "a" * 40
TAG_OBJECT = "b" * 40


def fresh_registry():
    """A separately loaded module represents a new command with clean state."""
    spec = importlib.util.spec_from_file_location(
        "registry_github_fixture", Path(registry_module.__file__)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def response(body: bytes, headers: dict[str, str] | None = None):
    stream = io.BytesIO(body)
    stream.headers = headers or {}
    return stream


def http_error(url: str, status: int, headers: dict[str, str]) -> HTTPError:
    message = Message()
    for key, value in headers.items():
        message[key] = value
    return HTTPError(url, status, "fixture", message, io.BytesIO(b"body"))


class CredentialDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"PATH": "/fixture"}, clear=True))
        self.registry = fresh_registry()
        self.calls = []

    def helpers(self, gh=b"", git=b"", fail=None):
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            if fail is not None and argv[0] == fail:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            output = gh if argv[0] == "gh" else git
            return subprocess.CompletedProcess(argv, 0 if output else 1, output, b"")

        return patch.object(self.registry.subprocess, "run", side_effect=run)

    def test_discovery_requires_a_command_entry_point(self):
        with self.helpers(gh=TOKEN.encode()):
            self.assertEqual(self.registry.github_token(), "")
        self.assertEqual(self.calls, [])

    def test_environment_tokens_precede_gh_which_precedes_git(self):
        self.registry.configure_github(cache_home=Path("/nonexistent"))
        with self.helpers(gh=b"gh_token_value\n", git=b"password=git_value\n"):
            self.assertEqual(self.registry.discover_github_token(), "gh_token_value")
        self.assertEqual([argv[0] for argv, _ in self.calls], ["gh"])
        self.calls.clear()
        with self.helpers(git=b"protocol=https\nhost=github.com\npassword=git_value\n"):
            self.assertEqual(self.registry.discover_github_token(), "git_value")
        self.assertEqual([argv[0] for argv, _ in self.calls], ["gh", "git"])
        self.calls.clear()
        os.environ["GH_TOKEN"] = "environment_value"
        with self.helpers(gh=b"gh_token_value"):
            self.assertEqual(self.registry.github_token(), "environment_value")
        self.assertEqual(self.calls, [])

    def test_helpers_are_read_only_non_interactive_and_bounded(self):
        self.registry.configure_github(cache_home=Path("/nonexistent"))
        os.environ.update(
            GIT_ASKPASS="/fixture/askpass",
            SSH_ASKPASS="/fixture/askpass",
            GIT_DIR="/fixture/repository",
        )
        with self.helpers():
            self.assertEqual(self.registry.discover_github_token(), "")
        self.assertEqual(len(self.calls), 2)
        for argv, kwargs in self.calls:
            self.assertNotIn("approve", argv)
            self.assertNotIn("reject", argv)
            self.assertNotIn("store", argv)
            self.assertEqual(kwargs["timeout"], 5)
            self.assertEqual(kwargs["cwd"], "/")
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertIsInstance(kwargs["input"], bytes)
            env = kwargs["env"]
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env["GCM_INTERACTIVE"], "never")
            self.assertEqual(env["GH_PROMPT_DISABLED"], "1")
            for name in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR"):
                self.assertNotIn(name, env)
        git_argv, git_kwargs = self.calls[1]
        self.assertEqual(git_argv[-2:], ["credential", "fill"])
        self.assertIn("core.askPass=", git_argv)
        self.assertEqual(git_kwargs["input"], b"protocol=https\nhost=github.com\n\n")
        self.assertEqual(self.calls[0][1]["input"], b"")

    def test_failures_timeouts_and_malformed_values_are_anonymous(self):
        self.registry.configure_github(cache_home=Path("/nonexistent"))
        for options in (
            {"fail": "gh", "git": b""},
            {"fail": "git"},
            {"gh": b"bad token with spaces"},
            {"git": b"password=bad\x01value\n"},
        ):
            with self.subTest(options=options), self.helpers(**options):
                self.assertEqual(self.registry.discover_github_token(), "")
        with patch.object(
            self.registry.subprocess, "run", side_effect=FileNotFoundError("gh")
        ):
            self.assertEqual(self.registry.discover_github_token(), "")

    def test_opt_out_never_starts_helpers(self):
        self.registry.configure_github(cache_home=Path("/nonexistent"))
        os.environ["CHAINMAN_GITHUB_CREDENTIALS"] = "off"
        with self.helpers(gh=TOKEN.encode()):
            self.assertEqual(self.registry.github_token(), "")
        self.assertEqual(self.calls, [])

    def test_discovered_token_reaches_only_api_github_and_is_not_exported(self):
        self.registry.configure_github(cache_home=Path("/nonexistent"))
        authenticated, anonymous = [], []

        class Opener:
            def open(self, request, timeout):
                authenticated.append(request)
                return response(b"authenticated")

        def send(request, timeout):
            anonymous.append(request)
            return response(b"anonymous")

        with (
            self.helpers(gh=TOKEN.encode()),
            patch.object(self.registry, "build_opener", return_value=Opener()),
            patch.object(self.registry, "urlopen", side_effect=send),
        ):
            self.registry._fetch("https://api.github.com/repos/example/demo")
            self.registry._fetch("https://registry.npmjs.org/demo")
            self.registry._fetch("https://github.com/example/demo.git/info/refs")
        self.assertEqual(
            [r.get_header("Authorization") for r in authenticated],
            ["Bearer " + TOKEN],
        )
        self.assertTrue(all(r.get_header("Authorization") is None for r in anonymous))
        self.assertEqual(len(anonymous), 2)
        self.assertNotIn(TOKEN, json.dumps(dict(os.environ)))
        self.assertEqual(len(self.calls), 1)


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.registry = fresh_registry()

    def test_exhausted_quota_reports_reset_and_credential_guidance(self):
        url = "https://api.github.com/repos/example/demo/releases"
        error = http_error(
            url, 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000000"}
        )
        with (
            patch.object(self.registry, "urlopen", side_effect=error) as send,
            patch.object(self.registry.time, "sleep") as sleep,
        ):
            with self.assertRaises(self.registry.GitHubRateLimitError) as caught:
                self.registry._fetch(url)
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()
        message = str(caught.exception)
        reset = datetime.fromtimestamp(1800000000, timezone.utc).isoformat()
        self.assertIn(reset, message)
        self.assertIn("GITHUB_TOKEN", message)
        self.assertIn("gh auth login", message)
        self.assertIsInstance(caught.exception, self.registry.RegistryHTTPError)

    def test_other_hosts_keep_their_existing_error_handling(self):
        url = "https://registry.npmjs.org/demo"
        error = http_error(url, 403, {"X-RateLimit-Remaining": "0"})
        with patch.object(self.registry, "urlopen", side_effect=error):
            with self.assertRaises(self.registry.RegistryHTTPError) as caught:
                self.registry._fetch(url)
        self.assertNotIsInstance(caught.exception, self.registry.GitHubRateLimitError)

    def test_preflight_fails_early_only_when_core_quota_is_exhausted(self):
        def limit(remaining):
            body = {
                "resources": {"core": {"remaining": remaining, "reset": 1800000000}}
            }
            return response(json.dumps(body).encode())

        with patch.object(self.registry, "urlopen", return_value=limit(12)) as send:
            self.registry.github_quota_check()
        self.assertEqual(
            send.call_args.args[0].full_url, "https://api.github.com/rate_limit"
        )
        with patch.object(self.registry, "urlopen", return_value=limit(0)):
            with self.assertRaisesRegex(
                self.registry.GitHubRateLimitError, "rate limit exhausted until 20"
            ):
                self.registry.github_quota_check()
        for failure in (
            response(b"not json"),
            response(b'{"resources": {}}'),
        ):
            with patch.object(self.registry, "urlopen", return_value=failure):
                self.registry.github_quota_check()
        with (
            patch.object(
                self.registry,
                "urlopen",
                side_effect=http_error("https://api.github.com/rate_limit", 500, {}),
            ),
            patch.object(self.registry.time, "sleep"),
        ):
            self.registry.github_quota_check()


class ImmutableCacheTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = fresh_registry()
        self.registry.configure_github(cache_home=self.home)
        self.registry._github_settings.discover = False

    def commit_body(self, sha=COMMIT, date="2026-01-02T03:04:05Z"):
        return {"sha": sha, "commit": {"committer": {"date": date}}}

    def test_commit_dates_are_reused_by_later_commands(self):
        url = f"https://api.github.com/repos/example/demo/commits/{COMMIT}"
        with patch.object(
            self.registry, "data", return_value=self.commit_body()
        ) as read:
            first = self.registry.github_commit_date("example/demo", COMMIT)
        read.assert_called_once_with(url)
        later = fresh_registry()
        later.configure_github(cache_home=self.home)
        with patch.object(later, "data", side_effect=AssertionError("network")):
            self.assertEqual(later.github_commit_date("example/demo", COMMIT), first)
        entries = list(self.home.rglob("*.json"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].stat().st_mode & 0o777, 0o600)

    def test_mismatched_commit_identity_is_never_cached(self):
        with patch.object(
            self.registry, "data", return_value=self.commit_body(sha="c" * 40)
        ):
            with self.assertRaisesRegex(ValueError, "different commit identity"):
                self.registry.github_commit_date("example/demo", COMMIT)
        self.assertEqual(list(self.home.rglob("*.json")), [])

    def test_entries_must_restate_their_complete_key(self):
        key = ["commit-committer-date", "example/demo", COMMIT]
        self.registry.immutable_put(key, "2026-01-02T03:04:05+00:00")
        (entry,) = self.home.rglob("*.json")
        for corrupt in (
            b"{not json",
            json.dumps(
                {
                    "key": ["1", "commit-committer-date", "other/repo", COMMIT],
                    "value": "2020-01-01T00:00:00+00:00",
                }
            ).encode(),
            json.dumps({"key": ["1", *key]}).encode(),
        ):
            entry.write_bytes(corrupt)
            self.assertIsNone(self.registry.immutable_get(key))
        with patch.object(
            self.registry, "data", return_value=self.commit_body()
        ) as read:
            self.registry.github_commit_date("example/demo", COMMIT)
        read.assert_called_once()
        self.assertEqual(self.registry.immutable_get(key), "2026-01-02T03:04:05+00:00")

    def test_cache_is_inactive_without_a_command_entry_point(self):
        plain = fresh_registry()
        with patch.object(plain, "data", return_value=self.commit_body()) as read:
            plain.github_commit_date("example/demo", COMMIT)
            plain.github_commit_date("example/demo", COMMIT)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(list(self.home.rglob("*.json")), [])

    def test_annotated_tag_objects_are_cached_but_tag_names_are_read_live(self):
        self.registry._github_settings.git_refs = False
        responses = {
            "https://api.github.com/repos/example/demo/git/ref/tags/v1.0.0": {
                "object": {"type": "tag", "sha": TAG_OBJECT}
            },
            f"https://api.github.com/repos/example/demo/git/tags/{TAG_OBJECT}": {
                "object": {"type": "commit", "sha": COMMIT}
            },
        }
        seen = []

        def read(url):
            seen.append(url)
            return responses[url]

        with patch.object(self.registry, "data", side_effect=read):
            self.assertEqual(
                self.registry.github_commit("example/demo", "v1.0.0"), COMMIT
            )
            self.assertEqual(
                self.registry.github_commit("example/demo", "v1.0.0"), COMMIT
            )
        self.assertEqual(
            seen,
            [
                "https://api.github.com/repos/example/demo/git/ref/tags/v1.0.0",
                f"https://api.github.com/repos/example/demo/git/tags/{TAG_OBJECT}",
                "https://api.github.com/repos/example/demo/git/ref/tags/v1.0.0",
            ],
        )

    def test_only_immutable_published_releases_are_reused(self):
        import source_updates

        tag = "v4.1.0"
        immutable = {
            "tag_name": tag,
            "draft": False,
            "prerelease": False,
            "published_at": "2026-01-02T03:04:05Z",
            "immutable": True,
        }
        with (
            patch.object(source_updates, "registry", self.registry),
            patch.object(self.registry, "data", return_value=immutable) as read,
        ):
            first = source_updates.action_release_by_tag("example/demo", tag)
            second = source_updates.action_release_by_tag("example/demo", tag)
        self.assertEqual(first, second)
        read.assert_called_once()
        mutable = dict(immutable, tag_name="v4.2.0", immutable=False)
        with (
            patch.object(source_updates, "registry", self.registry),
            patch.object(self.registry, "data", return_value=mutable) as read,
        ):
            source_updates.action_release_by_tag("example/demo", "v4.2.0")
            source_updates.action_release_by_tag("example/demo", "v4.2.0")
        self.assertEqual(read.call_count, 2)


class GitTagTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = fresh_registry()
        self.registry.configure_github(cache_home=self.home)
        self.registry._github_settings.discover = False
        import git_runtime

        self.git_runtime = git_runtime

    def listing(self, text):
        calls = []

        def git(*args, input=None):
            calls.append(args)
            if isinstance(text, BaseException):
                raise text
            return text.encode()

        return calls, patch.object(self.git_runtime, "git", side_effect=git)

    def test_annotated_tags_peel_without_rest_reference_requests(self):
        calls, patched = self.listing(
            f"{TAG_OBJECT}\trefs/tags/v1.0.0\n{COMMIT}\trefs/tags/v1.0.0^{{}}\n"
        )
        body = {
            "sha": COMMIT,
            "commit": {"committer": {"date": "2026-01-02T03:04:05Z"}},
        }
        with patched, patch.object(self.registry, "data", return_value=body) as read:
            self.assertEqual(
                self.registry.github_commit("example/demo", "v1.0.0"), COMMIT
            )
        read.assert_called_once_with(
            f"https://api.github.com/repos/example/demo/commits/{COMMIT}"
        )
        args = calls[1]
        self.assertIn("ls-remote", args)
        self.assertIn("credential.helper=", args)
        self.assertEqual(
            args[-2:], ("https://github.com/example/demo.git", "refs/tags/v1.0.0")
        )

    def test_lightweight_tags_use_the_direct_object(self):
        _, patched = self.listing(f"{COMMIT}\trefs/tags/1.2.3\n")
        with patched:
            self.assertEqual(
                self.registry.git_tag_target("example/demo", "1.2.3"), COMMIT
            )

    def test_unusable_git_answers_fall_back_to_rest(self):
        for text in (
            "",
            f"{COMMIT}\trefs/tags/other\n",
            "short\trefs/tags/v1.0.0\n",
            f"{COMMIT}\trefs/tags/v1.0.0\n{TAG_OBJECT}\trefs/tags/v1.0.0\n",
            subprocess.CalledProcessError(128, ["git"]),
            OSError("git"),
        ):
            with self.subTest(text=repr(text)[:40]):
                _, patched = self.listing(text)
                with patched:
                    self.assertIsNone(
                        self.registry.git_tag_target("example/demo", "v1.0.0")
                    )
        for tag in ("-c", "v1 .0", "a" * 129, "../x\n"):
            with self.subTest(tag=tag):
                calls, patched = self.listing(f"{COMMIT}\trefs/tags/{tag}\n")
                with patched:
                    self.assertIsNone(self.registry.git_tag_target("example/demo", tag))
                self.assertEqual(calls, [])

    def test_non_commit_targets_keep_the_bounded_identity_error(self):
        _, patched = self.listing(f"{COMMIT}\trefs/tags/v1.0.0\n")
        with (
            patched,
            patch.object(
                self.registry,
                "data",
                side_effect=self.registry.RegistryHTTPError(422, "api.github.com"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "bounded commit identity"):
                self.registry.github_commit("example/demo", "v1.0.0")

    def test_git_refs_require_a_command_entry_point(self):
        plain = fresh_registry()
        calls, patched = self.listing(f"{COMMIT}\trefs/tags/v1.0.0\n")
        with patched:
            self.assertIsNone(plain.git_tag_target("example/demo", "v1.0.0"))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
