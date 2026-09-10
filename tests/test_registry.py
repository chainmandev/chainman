"""Policy oracles apply equally to requested releases and resolver-selected locks."""

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import base64
import copy
import io
import importlib.util
import json
import os
import ssl
import traceback
from pathlib import Path
import sys
import tempfile
from threading import Event
import tomllib
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, build_opener
from urllib.response import addinfourl
from email.message import Message

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import updates

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def release(version, days):
    return registry.Release(version, NOW - timedelta(days=days))


class ConjunctionTests(unittest.TestCase):
    def test_each_range_retains_its_own_prerelease_boundary(self):
        bounds = [">=5.0.0 <6.0.0", "^5.0.0-beta.0"]
        # Independently checked with strict node-semver membership per range.
        for value, expected in (
            ("5.0.0-alpha.1", False),
            ("5.0.0-beta.0", False),
            ("5.0.0-beta.9", False),
            ("5.0.0", True),
            ("5.1.0", True),
            ("5.1.1-alpha", False),
            ("5.18.2+build.7", True),
            ("6.0.0", False),
        ):
            for ordered in (bounds, bounds[::-1], bounds + bounds):
                with self.subTest(value=value, bounds=ordered):
                    self.assertEqual(
                        registry.compatible("npm", value, ordered), expected
                    )

    def test_invalid_members_never_hide_behind_a_false_constraint(self):
        invalid = (
            [],
            ["^1", "not-a-range"],
            ["not-a-range", "^1"],
            ["^1", ""],
            ["^1", None],
            [["^1"]],
            ["* "] * 129,
            [" || ".join("^1" for _ in range(129))],
            [" " * 65536 + "^1"],
        )
        for bounds in invalid:
            with self.subTest(bounds=str(bounds)[:80]), self.assertRaises(ValueError):
                registry.compatible("npm", "5.0.0", bounds)
        for provider in ("pypi", "crates", "pub", "github", "go", "maven"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                registry.compatible(provider, "5.0.0", ["^5"])

    def test_maturity_and_exception_retirement_use_every_bound(self):
        bounds = [">=5.0.0 <7.0.0", "<6.0.0"]
        policy = {
            "constraints": {"npm:demo": {"range": bounds, "reason": "Supported ABI"}}
        }
        self.assertEqual(
            registry.select(
                "npm", [release("5.1.0", 31), release("6.0.0", 60)], policy, "demo", NOW
            ).version,
            "5.1.0",
        )
        policy["exceptions"] = [
            {
                "package": "npm:demo",
                "version": "5.2.0",
                "minimum_safe": "5.2.0",
                "reason": "Verified correction",
                "advisory": "https://example.invalid/advisory",
                "expires": (NOW + timedelta(days=1)).isoformat(),
            }
        ]
        candidates = [release("5.2.0", 1), release("6.0.0", 60)]
        self.assertEqual(
            registry.select("npm", candidates, policy, "demo", NOW).version, "5.2.0"
        )
        policy["exceptions"][0]["expires"] = NOW.isoformat()
        with self.assertRaisesRegex(ValueError, "Expired"):
            registry.select("npm", candidates, policy, "demo", NOW)
        candidates.append(release("5.3.0", 30))
        self.assertEqual(
            registry.select("npm", candidates, policy, "demo", NOW).version, "5.3.0"
        )
        self.assertEqual(
            registry.active_exceptions("npm", candidates, policy, "demo", NOW), []
        )

    def test_final_unchanged_artifact_still_obeys_every_bound(self):
        identity = ("npm", "demo", "6.0.0", "", "sha256:" + "a" * 64)
        policy = {
            "constraints": {
                "npm:demo": {"range": [">=5", "<6"], "reason": "Supported ABI"}
            }
        }
        evidence = registry.Release(
            "6.0.0",
            NOW - timedelta(days=90),
            artifacts=(registry.Artifact("", identity[4], NOW - timedelta(days=90)),),
        )
        with (
            patch.object(registry, "releases", return_value=[evidence]),
            self.assertRaisesRegex(ValueError, "compatibility"),
        ):
            updates.audit_identities(Path("."), {identity}, {identity}, policy, NOW)


class RegistryTransportTests(unittest.TestCase):
    def setUp(self):
        registry.fetch.cache_clear()
        self.addCleanup(registry.fetch.cache_clear)

    def test_large_package_history_keeps_exact_maturity_and_artifact_evidence(self):
        metadata = {
            "versions": {
                "1.0.0": {
                    "dist": {
                        "tarball": "https://registry.npmjs.org/sample/-/sample-1.0.0.tgz",
                        "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode(),
                    }
                }
            },
            "time": {"1.0.0": "2026-07-02T00:00:00Z"},
        }
        body = json.dumps(metadata).encode()
        # Real package histories can exceed the former 32 MiB transport cap.
        # Whitespace makes a neutral, fully parsed 39 MiB JSON response.
        body += b" " * (39 * 1024 * 1024 - len(body))
        with io.BytesIO(body) as response:
            response.headers = {"Content-Type": "application/json"}
            with patch.object(registry, "urlopen", return_value=response):
                releases = registry.releases("npm", "sample")
        chosen = registry.select("npm", releases, {}, "sample", NOW)
        self.assertEqual(chosen.version, "1.0.0")
        self.assertEqual(chosen.published, datetime(2026, 7, 2, tzinfo=timezone.utc))
        self.assertEqual(chosen.artifacts[0].digest, "sha512:" + (b"x" * 64).hex())
        with self.assertRaises(ValueError):
            registry.select("npm", releases, {}, "sample", NOW - timedelta(seconds=1))

    def test_response_limit_is_bounded_and_diagnostic_omits_url_secrets(self):
        limit = 64 * 1024 * 1024
        for size in (limit, limit + 1):
            with self.subTest(size=size), io.BytesIO(b"x" * size) as response:
                response.headers = {}
                with (
                    patch.object(registry, "urlopen", return_value=response),
                    patch.object(response, "read", wraps=response.read) as read,
                ):
                    url = f"https://registry.example.invalid/private-name?token=hidden-{size}"
                    if size == limit:
                        self.assertEqual(len(registry.fetch(url)[0]), limit)
                    else:
                        with self.assertRaises(ValueError) as caught:
                            registry.fetch(url)
                        self.assertEqual(
                            str(caught.exception),
                            "Registry response exceeds 64 MiB from registry.example.invalid",
                        )
                    read.assert_called_once_with(limit + 1)

    def transport(self, outcomes, *, elapsed=0, dispatch_delays=()):
        """Record real transport entry times under an independently advanced clock."""
        clock = [100.0]
        delays = iter(dispatch_delays)
        sent, waits, errors = [], [], []

        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            for error in errors:
                if isinstance(error, HTTPError):
                    self.assertTrue(error.fp.closed)
            waits.append(seconds)
            clock[0] += seconds

        def send(request, timeout):
            self.assertEqual(timeout, 30)
            clock[0] += next(delays, 0)
            sent.append((clock[0], request))
            clock[0] += elapsed
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                errors.append(outcome)
                raise outcome
            response = io.BytesIO(outcome)
            response.headers = {"Content-Type": "application/json"}
            return response

        self.enterContext(
            patch.object(registry.time, "monotonic", side_effect=lambda: clock[0])
        )
        self.enterContext(patch.object(registry.time, "sleep", side_effect=sleep))
        self.enterContext(patch.object(registry, "urlopen", side_effect=send))
        self.enterContext(
            patch.object(registry, "_crates_last_request", None, create=True)
        )
        return sent, waits, errors

    def http_error(self, code, retry=None):
        headers = {} if retry is None else {"Retry-After": retry}
        return HTTPError(
            "https://crates.io/private?secret=hidden",
            code,
            "secret server text",
            headers,
            io.BytesIO(b"secret body"),
        )

    def test_crates_api_paces_cache_misses_fresh_reads_and_retries(self):
        error = self.http_error(429)
        sent, waits, _ = self.transport([b"one", b"two", error, b"three"])
        first = "https://crates.io/api/v1/crates/one"
        self.assertEqual(registry.fetch(first)[0], b"one")
        self.assertEqual(registry.fetch(first)[0], b"one")
        self.assertEqual(registry._fetch(first, fresh=True)[0], b"two")
        self.assertEqual(
            registry.fetch("https://crates.io/api/v1/crates/two")[0], b"three"
        )
        self.assertEqual([row[0] for row in sent], [100, 101, 102, 103])
        self.assertEqual(waits, [1, 1, 1])
        self.assertEqual(sent[1][1].get_header("Cache-control"), "no-cache")
        self.assertIn("chainman", sent[0][1].get_header("User-agent").lower())
        self.assertTrue(error.fp.closed)

    def test_slow_crates_responses_keep_a_conservative_completion_gap(self):
        sent, waits, _ = self.transport([b"a", b"b"], elapsed=1.5)
        registry._fetch("https://crates.io/api/v1/crates/a")
        registry._fetch("https://crates.io/api/v1/crates/b")
        self.assertEqual([row[0] for row in sent], [100, 102.5])
        self.assertEqual(waits, [1])

    def test_delayed_opener_cannot_leave_a_stale_dispatch_timestamp(self):
        sent, waits, _ = self.transport([b"a", b"b"], dispatch_delays=(2, 0))
        registry._fetch("https://crates.io/api/v1/crates/a")
        registry._fetch("https://crates.io/api/v1/crates/b")
        self.assertEqual([t for t, _ in sent], [102, 103])
        self.assertEqual(waits, [1])

    def test_concurrent_crates_requests_do_not_reserve_then_bunch_dispatches(self):
        sent, waits, _ = self.transport([b"first", b"second"])
        first_entered, release_first, second_waiting, second_entered = (
            Event() for _ in range(4)
        )
        send = registry.urlopen.side_effect

        def blocked_send(request, timeout):
            if request.full_url.endswith("/first"):
                first_entered.set()
                self.assertTrue(release_first.wait(3))
            else:
                second_entered.set()
            return send(request, timeout)

        def second_request():
            second_waiting.set()
            return registry._fetch("https://crates.io/api/v1/crates/second")[0]

        with (
            patch.object(registry, "urlopen", side_effect=blocked_send),
            ThreadPoolExecutor(2) as pool,
        ):
            first = pool.submit(
                registry._fetch, "https://crates.io/api/v1/crates/first"
            )
            try:
                self.assertTrue(first_entered.wait(3))
                second = pool.submit(second_request)
                self.assertTrue(second_waiting.wait(3))
                self.assertFalse(second_entered.wait(0.02))
            finally:
                release_first.set()
            self.assertEqual(first.result(timeout=3)[0], b"first")
            self.assertEqual(second.result(timeout=3), b"second")
        self.assertEqual([t for t, _ in sent], [100, 101])
        self.assertEqual(waits, [1])

    def test_other_hosts_including_sparse_index_and_cdn_are_not_paced(self):
        sent, waits, _ = self.transport([b"a"] * 5)
        for host in (
            "index.crates.io",
            "static.crates.io",
            "registry.npmjs.org",
            "crates.io.example.invalid",
            "example.invalid",
        ):
            registry._fetch(f"https://{host}/entry", "text/plain", "HEAD")
        self.assertEqual([row[0] for row in sent], [100] * 5)
        self.assertEqual(waits, [])
        self.assertTrue(
            all(
                r.get_method() == "HEAD" and r.get_header("Accept") == "text/plain"
                for _, r in sent
            )
        )

    def test_retry_after_seconds_and_dates_use_actual_receipt_clock(self):
        cases = (
            (429, "7", 7),
            (503, "Thu, 10 Sep 2026 00:00:09 GMT", 9),
            (503, "Thursday, 10-Sep-26 00:00:09 GMT", 9),
            (503, "Thu Sep 10 00:00:09 2026", 9),
            (500, "Thu, 10 Sep 2026 01:00:06 +0100", 6),
            (502, "Wed, 09 Sep 2026 23:00:00 GMT", 1),
            (504, "60", 60),
        )
        self.enterContext(
            patch.object(
                registry,
                "observation_time",
                return_value=datetime(2026, 9, 10, tzinfo=timezone.utc),
            )
        )
        for code, header, delay in cases:
            with self.subTest(code=code, header=header):
                error = self.http_error(code, header)
                sent, waits, _ = self.transport([error, b"ok"])
                self.assertEqual(
                    registry._fetch("https://example.invalid/data")[0], b"ok"
                )
                self.assertEqual(waits, [delay])
                self.assertEqual([t for t, _ in sent], [100, 100 + delay])
                self.assertTrue(error.fp.closed)

    def test_missing_or_malformed_retry_after_retains_bounded_backoff(self):
        for header in (
            None,
            "",
            "garbage",
            "-1",
            "1.5",
            "NaN",
            "1, 2",
            "Thu, 10 Sep 2026 00:00:09",
            "x" * 129,
        ):
            with self.subTest(header=header):
                errors = [self.http_error(429, header), self.http_error(503, header)]
                sent, waits, _ = self.transport([*errors, b"ok"])
                self.assertEqual(
                    registry._fetch("https://example.invalid/data")[0], b"ok"
                )
                self.assertEqual(waits, [1, 2])
                self.assertEqual(len(sent), 3)
                self.assertTrue(all(e.fp.closed for e in errors))

    def test_excessive_valid_retry_after_fails_without_an_early_retry(self):
        self.enterContext(
            patch.object(
                registry,
                "observation_time",
                return_value=datetime(2026, 9, 10, tzinfo=timezone.utc),
            )
        )
        for header in ("61", "9" * 128, "9" * 5000, "Fri, 11 Sep 2026 00:00:00 GMT"):
            with self.subTest(header=header):
                error = self.http_error(429, header)
                sent, waits, _ = self.transport([error, b"must not request"])
                with self.assertRaisesRegex(
                    registry.RegistryHTTPError, "Retry-After"
                ) as caught:
                    registry._fetch("https://example.invalid/private?secret=hidden")
                self.assertEqual(caught.exception.status, 429)
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("hidden", str(caught.exception))
                self.assertEqual(len(sent), 1)
                self.assertEqual(waits, [])
                self.assertTrue(error.fp.closed)

    def test_http_exhaustion_preserves_final_status_without_final_sleep_or_cache(self):
        errors = [self.http_error(c) for c in (429, 503, 502)]
        sent, waits, _ = self.transport([*errors, b"later"])
        url = "https://example.invalid/private?secret=hidden"
        with self.assertRaises(registry.RegistryHTTPError) as caught:
            registry.fetch(url)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(
            str(caught.exception), "Registry HTTP 502 from example.invalid"
        )
        self.assertEqual(waits, [1, 2])
        self.assertTrue(all(e.fp.closed for e in errors))
        self.assertEqual(registry.fetch(url)[0], b"later")
        self.assertEqual(len(sent), 4)

    def test_permanent_status_and_interrupt_are_not_retried(self):
        for error in (self.http_error(404, "60"), KeyboardInterrupt()):
            sent, waits, _ = self.transport([error])
            expected = (
                registry.RegistryHTTPError
                if isinstance(error, HTTPError)
                else KeyboardInterrupt
            )
            with self.assertRaises(expected):
                registry._fetch("https://crates.io/api/v1/crates/a")
            self.assertEqual(len(sent), 1)
            self.assertEqual(waits, [])
            if isinstance(error, HTTPError):
                self.assertTrue(error.fp.closed)

    def test_transport_exhaustion_keeps_existing_bounded_unavailable_error(self):
        sent, waits, _ = self.transport(
            [URLError("private"), TimeoutError(), URLError("hidden")]
        )
        with self.assertRaisesRegex(
            ValueError, "^Registry unavailable: example.invalid$"
        ):
            registry._fetch("https://example.invalid/private?secret=hidden")
        self.assertEqual(len(sent), 3)
        self.assertEqual(waits, [1, 2])

    def test_interrupted_pacing_releases_lock_without_dispatch_or_cached_success(self):
        sent, waits, _ = self.transport([b"first", b"later"])
        registry.fetch("https://crates.io/api/v1/crates/first")
        url = "https://crates.io/api/v1/crates/later"
        with patch.object(registry.time, "sleep", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                registry.fetch(url)
        self.assertEqual(len(sent), 1)
        self.assertEqual(registry.fetch(url)[0], b"later")
        self.assertEqual([t for t, _ in sent], [100, 101])
        self.assertEqual(waits, [1])


class GitHubAuthTests(unittest.TestCase):
    token = "sentinel_test_credential_OnlyFixture123"
    url = "https://api.github.com/repos/example/demo/releases?private=query-secret"

    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.registry = self.module()

    def module(self):
        # A separate module represents a new command, without resetting its context.
        spec = importlib.util.spec_from_file_location(
            "registry_auth_fixture", Path(registry.__file__)
        )
        module = importlib.util.module_from_spec(spec)
        self.enterContext(patch.dict(sys.modules, {spec.name: module}))
        spec.loader.exec_module(module)
        return module

    def transport(self, outcomes):
        sent, closed, waits = [], [], []
        test = self

        def send(request):
            sent.append(request)
            test.assertEqual(request.timeout, 30)
            value = outcomes.pop(0)
            if isinstance(value, BaseException):
                raise value
            status, headers, body = value
            message = Message()
            for key, text in headers.items():
                message[key] = text
            stream = io.BytesIO(body)
            closed.append(stream)
            response = addinfourl(stream, message, request.full_url, status)
            response.msg = "fixture status"
            return response

        class FixtureHTTPS(HTTPSHandler):
            def https_open(self, request):
                return send(request)

        class FixtureHTTP(HTTPHandler):
            def http_open(self, request):
                return send(request)

        # Run the real opener/error processor/redirect chain; replace only wire I/O.
        def opener(*handlers):
            return build_opener(
                *handlers, FixtureHTTPS(), FixtureHTTP(), ProxyHandler({})
            )

        self.enterContext(
            patch.object(self.registry, "build_opener", side_effect=opener)
        )
        self.enterContext(
            patch.object(
                self.registry,
                "urlopen",
                side_effect=lambda request, timeout: opener().open(
                    request, timeout=timeout
                ),
            )
        )
        self.enterContext(
            patch.object(self.registry.time, "sleep", side_effect=waits.append)
        )
        return sent, closed, waits

    def assert_redacted(self, error):
        rendered = "".join(traceback.format_exception(error))
        for secret in (
            self.token,
            "query-secret",
            "location-secret",
            "body-secret",
            "Authorization",
        ):
            self.assertNotIn(secret, rendered)

    def test_only_exact_github_api_origin_gets_the_explicit_token(self):
        os.environ["GITHUB_TOKEN"] = self.token
        sent, _, _ = self.transport([(200, {}, b"ok")] * 3)
        anonymous = []

        def send(request, timeout):
            anonymous.append(request)
            self.assertEqual(timeout, 30)
            self.assertIsNone(request.get_header("Authorization"))
            response = io.BytesIO(b"anonymous")
            response.headers = {}
            return response

        with patch.object(self.registry, "urlopen", side_effect=send):
            for url in (
                self.url,
                "https://API.GITHUB.COM/data",
                "https://api.github.com:443/data",
            ):
                self.assertEqual(self.registry._fetch(url)[0], b"ok")
            for url in (
                "https://github.com/data",
                "https://api.github.com.example.invalid/data",
                "https://api.github.com./data",
                "https://api.github.com:444/data",
                "https://raw.githubusercontent.com/data",
                "https://codeload.github.com/data",
                "https://registry.npmjs.org/data",
                "https://crates.io.example.invalid/data",
            ):
                self.assertEqual(self.registry._fetch(url)[0], b"anonymous")
            for url in (
                "http://api.github.com/data",
                "https://user@api.github.com/data",
                "https://user:password@api.github.com/data",
            ):
                with self.assertRaises(ValueError):
                    self.registry._fetch(url)
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(anonymous), 8)
        self.assertTrue(
            all(r.get_header("Authorization") == "Bearer " + self.token for r in sent)
        )

    def test_absent_empty_token_and_unrelated_environment_keep_anonymous_transport(
        self,
    ):
        os.environ["GH_TOKEN"] = "not_selected"
        with patch.object(self.registry, "build_opener") as authenticated:
            for supplied in (None, ""):
                if supplied is not None:
                    os.environ["GITHUB_TOKEN"] = supplied
                response = io.BytesIO(b"public")
                response.headers = {}
                with patch.object(
                    self.registry, "urlopen", return_value=response
                ) as send:
                    self.assertEqual(self.registry._fetch(self.url)[0], b"public")
                self.assertIsNone(send.call_args.args[0].get_header("Authorization"))
            authenticated.assert_not_called()

    def test_malformed_token_is_rejected_before_any_transport(self):
        with (
            patch.object(self.registry, "urlopen") as anonymous,
            patch.object(self.registry, "build_opener") as authenticated,
        ):
            for value in (
                " ",
                "bad token",
                "a\rb",
                "a\nb",
                "a\tb",
                "a\x00b",
                "a\x7fb",
                "nonascii-é",
                "Bearer token",
                "a" * 4097,
            ):
                # The real OS rejects NUL before the product can inspect it.
                with (
                    self.subTest(value=repr(value)),
                    patch.object(self.registry.os, "environ", {"GITHUB_TOKEN": value}),
                ):
                    with self.assertRaisesRegex(
                        ValueError, "Invalid GITHUB_TOKEN"
                    ) as caught:
                        self.registry.fetch(self.url)
                    self.assertEqual(
                        str(caught.exception), "Invalid GITHUB_TOKEN header value"
                    )
            anonymous.assert_not_called()
            authenticated.assert_not_called()

    def test_real_urllib_redirect_processing_never_dispatches_a_successor(self):
        os.environ["GITHUB_TOKEN"] = self.token
        for status in (301, 302, 303, 307, 308):
            for location in (
                "/same?location-secret",
                self.url,
                "//other.invalid/location-secret",
                "https://api.github.com:444/location-secret",
                "http://api.github.com/location-secret",
                "https://user@api.github.com/location-secret",
                "https://other.invalid/location-secret",
                "file:///location-secret",
                "https://[malformed/location-secret",
            ):
                with self.subTest(status=status, location=location):
                    sent, closed, waits = self.transport(
                        [(status, {"Location": location}, b"body-secret")]
                    )
                    with self.assertRaises(ValueError) as caught:
                        self.registry._fetch(self.url)
                    self.assert_redacted(caught.exception)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0].full_url, self.url)
                    self.assertEqual(waits, [])
                    self.assertTrue(all(stream.closed for stream in closed))

    def test_permanent_http_errors_are_sanitized_closed_and_never_fall_back(self):
        os.environ["GITHUB_TOKEN"] = self.token
        for status in (401, 403, 404):
            sent, closed, waits = self.transport(
                [(status, {"Location": "location-secret"}, b"body-secret")]
            )
            with patch.object(self.registry, "urlopen") as anonymous:
                with self.assertRaises(self.registry.RegistryHTTPError) as caught:
                    self.registry.fetch(self.url)
                anonymous.assert_not_called()
            self.assertEqual(caught.exception.status, status)
            self.assert_redacted(caught.exception)
            self.assertEqual(len(sent), 1)
            self.assertEqual(waits, [])
            self.assertTrue(all(stream.closed for stream in closed))

    def test_authenticated_retries_keep_credentials_bounds_and_uncached_failures(self):
        os.environ["GITHUB_TOKEN"] = self.token
        sent, closed, waits = self.transport(
            [
                (429, {"Retry-After": "3"}, b"body-secret"),
                (503, {}, b"body-secret"),
                (502, {}, b"body-secret"),
                (200, {}, b"later"),
            ]
        )
        with patch.object(self.registry, "urlopen") as anonymous:
            with self.assertRaises(self.registry.RegistryHTTPError) as caught:
                self.registry.fetch(self.url)
            self.assertEqual(caught.exception.status, 502)
            self.assert_redacted(caught.exception)
            self.assertEqual(self.registry.fetch(self.url)[0], b"later")
            self.assertEqual(self.registry.fetch(self.url)[0], b"later")
            anonymous.assert_not_called()
        self.assertEqual(len(sent), 4)
        self.assertEqual(waits, [3, 2])
        self.assertTrue(
            all(r.get_header("Authorization") == "Bearer " + self.token for r in sent)
        )
        self.assertTrue(all(stream.closed for stream in closed))
        sent, closed, waits = self.transport(
            [(429, {"Retry-After": "61"}, b"body-secret")]
        )
        with self.assertRaises(self.registry.RegistryHTTPError) as caught:
            self.registry._fetch(self.url, fresh=True)
        self.assert_redacted(caught.exception)
        self.assertEqual(len(sent), 1)
        self.assertEqual(waits, [])
        self.assertTrue(all(stream.closed for stream in closed))

    def test_authenticated_transport_and_header_errors_are_sanitized(self):
        os.environ["GITHUB_TOKEN"] = self.token
        for error in (
            URLError(self.token),
            TimeoutError(self.token),
            ssl.SSLError(self.token),
        ):
            sent, _, waits = self.transport([error, error, error])
            with self.assertRaisesRegex(ValueError, "Registry unavailable") as caught:
                self.registry._fetch(self.url)
            self.assert_redacted(caught.exception)
            self.assertEqual(len(sent), 3)
            self.assertEqual(waits, [1, 2])
        sent, _, waits = self.transport([ValueError("Authorization " + self.token)])
        with self.assertRaisesRegex(ValueError, "Registry request failed") as caught:
            self.registry._fetch(self.url)
        self.assert_redacted(caught.exception)
        self.assertEqual(len(sent), 1)
        self.assertEqual(waits, [])

    def test_authenticated_body_limit_closes_response_without_retry(self):
        os.environ["GITHUB_TOKEN"] = self.token
        sent, closed, waits = self.transport([(200, {}, b"body-secret")])
        with patch.object(self.registry, "MAX_RESPONSE_BYTES", 4):
            with self.assertRaises(ValueError) as caught:
                self.registry._fetch(self.url)
        self.assert_redacted(caught.exception)
        self.assertEqual(len(sent), 1)
        self.assertEqual(waits, [])
        self.assertTrue(all(stream.closed for stream in closed))

    def test_context_change_cannot_return_cached_privileged_data_or_switch_identity(
        self,
    ):
        for initial, changed in (
            (self.token, "other_token"),
            (self.token, ""),
            ("", self.token),
        ):
            for entry in ("cached", "fresh", "cache-cleared", "other-origin"):
                with self.subTest(initial=bool(initial), entry=entry):
                    self.registry = self.module()
                    os.environ["GITHUB_TOKEN"] = initial
                    sent, _, _ = self.transport([(200, {}, b"privileged")])
                    response = io.BytesIO(b"anonymous")
                    response.headers = {}
                    with patch.object(
                        self.registry, "urlopen", return_value=response
                    ) as anonymous:
                        self.registry.fetch(self.url)
                        os.environ["GITHUB_TOKEN"] = changed
                        if entry == "cache-cleared":
                            self.registry.fetch.cache_clear()
                        with self.assertRaisesRegex(
                            ValueError, "GITHUB_TOKEN changed"
                        ) as caught:
                            if entry == "fresh":
                                self.registry._fetch(self.url, fresh=True)
                            else:
                                self.registry.fetch(
                                    self.url
                                    if entry != "other-origin"
                                    else "https://example.invalid/data"
                                )
                        self.assert_redacted(caught.exception)
                        self.assertEqual(len(sent), int(bool(initial)))
                        self.assertEqual(anonymous.call_count, int(not initial))

    def test_anonymous_redirects_keep_the_existing_urllib_behavior(self):
        sent, closed, waits = self.transport(
            [
                (302, {"Location": "https://example.invalid/next"}, b"redirect"),
                (200, {}, b"public"),
            ]
        )
        self.assertEqual(self.registry._fetch(self.url)[0], b"public")
        self.assertEqual(
            [r.full_url for r in sent], [self.url, "https://example.invalid/next"]
        )
        self.assertTrue(all(r.get_header("Authorization") is None for r in sent))
        self.assertTrue(all(stream.closed for stream in closed))
        self.assertEqual(waits, [])

    def test_authenticated_fresh_head_preserves_headers_and_interrupt_propagation(self):
        os.environ["GITHUB_TOKEN"] = self.token
        sent, closed, waits = self.transport([(200, {}, b""), KeyboardInterrupt()])
        self.registry._fetch(self.url, "text/plain", "HEAD", fresh=True)
        request = sent[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertEqual(request.get_header("Accept"), "text/plain")
        self.assertEqual(request.get_header("Cache-control"), "no-cache")
        self.assertIn("chainman", request.get_header("User-agent"))
        with self.assertRaises(KeyboardInterrupt):
            self.registry._fetch(self.url)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(stream.closed for stream in closed))
        self.assertEqual(waits, [])

    def test_authentication_does_not_change_maturity_or_immutable_revision_checks(self):
        import source_updates

        results = []
        for token in ("", self.token):
            self.registry = self.module()
            os.environ["GITHUB_TOKEN"] = token
            self.transport(
                [
                    (
                        200,
                        {},
                        b'{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","commit":{"committer":{"date":"2026-07-02T00:00:00Z"}}}',
                    ),
                    (
                        200,
                        {},
                        b'{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","commit":{"committer":{"date":"2026-07-02T00:00:00Z"}}}',
                    ),
                ]
            )
            with patch.object(source_updates, "registry", self.registry):
                published = source_updates.commit_time("example/demo", "a" * 40)
                evidence = self.registry.Release("1.0.0", published, identity="a" * 40)
                selected = self.registry.select(
                    "swift", [evidence], {}, "example/demo", NOW
                )
                results.append(
                    (selected.version, selected.published, selected.identity)
                )
                with self.assertRaises(ValueError):
                    self.registry.select(
                        "swift",
                        [evidence],
                        {},
                        "example/demo",
                        NOW - timedelta(seconds=1),
                    )
                with self.assertRaises(ValueError):
                    source_updates.commit_time("example/demo", "c" * 40)
        self.assertEqual(
            results,
            [("1.0.0", datetime(2026, 7, 2, tzinfo=timezone.utc), "a" * 40)] * 2,
        )


class SwiftExactMetadataTests(unittest.TestCase):
    prefix = "https://api.github.com/repos/example/numerics"
    page = "/releases?per_page=100&page="

    def setUp(self):
        registry.fetch.cache_clear()
        self.addCleanup(registry.fetch.cache_clear)
        self.enterContext(patch.object(registry, "observation_time", return_value=NOW))

    def published(self, tag, days=90, **extra):
        return {
            "tag_name": tag,
            "published_at": (NOW - timedelta(days=days)).isoformat(),
            "draft": False,
            "prerelease": False,
            **extra,
        }

    def transport(self, responses):
        sent = []

        def send(request, timeout):
            self.assertEqual(timeout, 30)
            self.assertTrue(request.full_url.startswith(self.prefix))
            path = request.full_url.removeprefix(self.prefix)
            sent.append(path)
            self.assertIn(path, responses, "Unrequested release identity was fetched")
            body = io.BytesIO(json.dumps(responses[path]).encode())
            body.headers = {"Content-Type": "application/json"}
            return body

        self.enterContext(patch.object(registry, "urlopen", side_effect=send))
        return sent

    def commit(self, identity, days):
        return {
            "sha": identity,
            "commit": {"committer": {"date": (NOW - timedelta(days=days)).isoformat()}},
        }

    def test_exact_metadata_enriches_only_matching_aliases_across_all_pages(self):
        first, second, tag = "a" * 40, "b" * 40, "c" * 40
        responses = {
            self.page + "1": [self.published("1.0.0")]
            + [self.published(f"2.0.{index}") for index in range(99)],
            self.page + "2": [
                self.published("v1.0.0", 40),
                self.published("3.0.0", draft=True),
                self.published("4.0.0", prerelease=True),
                self.published("5.0.0-beta.1"),
            ],
            "/git/ref/tags/1.0.0": {"object": {"type": "commit", "sha": first}},
            "/git/ref/tags/v1.0.0": {"object": {"type": "tag", "sha": tag}},
            "/git/tags/" + tag: {"object": {"type": "commit", "sha": second}},
            "/commits/" + first: self.commit(first, 60),
            "/commits/" + second: self.commit(second, 1),
        }
        sent = self.transport(responses)
        values = registry.swift_releases("example/numerics", exact="1.0.0")
        self.assertEqual(
            [(item.version, item.identity, item.published) for item in values],
            [
                ("1.0.0", "1.0.0", NOW - timedelta(days=60)),
                ("1.0.0", "v1.0.0", NOW - timedelta(days=1)),
            ],
        )
        self.assertEqual(
            sent,
            [
                self.page + "1",
                self.page + "2",
                "/git/ref/tags/1.0.0",
                "/commits/" + first,
                "/git/ref/tags/v1.0.0",
                "/git/tags/" + tag,
                "/commits/" + second,
            ],
        )
        self.assertTrue(all(not item.artifacts for item in values))

    def test_missing_exact_metadata_does_not_probe_unrelated_tags(self):
        sent = self.transport({self.page + "1": [self.published("2.0.0")]})
        self.assertEqual(registry.swift_releases("example/numerics", exact="1.0.0"), [])
        self.assertEqual(sent, [self.page + "1"])

    def test_exact_metadata_keeps_selected_immutable_identity_and_date_checks(self):
        identity, tag = "a" * 40, "b" * 40
        valid = {
            self.page + "1": [self.published("1.0.0"), self.published("2.0.0")],
            "/git/ref/tags/1.0.0": {"object": {"type": "commit", "sha": identity}},
            "/commits/" + identity: self.commit(identity, 60),
        }
        invalid = [
            ("/git/ref/tags/1.0.0", {"object": {"type": "branch", "sha": identity}}),
            ("/git/ref/tags/1.0.0", {"object": {"type": "commit", "sha": "bad"}}),
            ("/commits/" + identity, self.commit("d" * 40, 60)),
            ("/commits/" + identity, self.commit(identity, -1)),
            (
                "/commits/" + identity,
                {"sha": identity, "commit": {"committer": {"date": "2026-01-01"}}},
            ),
        ]
        for path, value in invalid:
            with self.subTest(path=path, value=value):
                registry.fetch.cache_clear()
                self.transport({**valid, path: value})
                with self.assertRaises(ValueError):
                    registry.swift_releases("example/numerics", exact="1.0.0")
        registry.fetch.cache_clear()
        sent = self.transport(
            {
                **valid,
                "/git/ref/tags/1.0.0": {"object": {"type": "tag", "sha": tag}},
                "/git/tags/" + tag: {"object": {"type": "tag", "sha": tag}},
            }
        )
        with self.assertRaisesRegex(ValueError, "bounded commit"):
            registry.swift_releases("example/numerics", exact="1.0.0")
        self.assertNotIn("/commits/" + identity, sent)
        self.assertEqual(sent.count("/git/tags/" + tag), 1)

    def test_exact_metadata_rejects_incomplete_or_malformed_release_discovery(self):
        sent = self.transport(
            {
                self.page + str(page): [self.published("2.0.0")] * 100
                for page in range(1, 101)
            }
        )
        with self.assertRaisesRegex(ValueError, "pagination ceiling"):
            registry.swift_releases("example/numerics", exact="1.0.0")
        self.assertEqual(len(sent), 100)
        for payload in ({"unexpected": []}, [self.published("1.0.0", -1)]):
            with self.subTest(payload=payload):
                registry.fetch.cache_clear()
                self.transport({self.page + "1": payload})
                with self.assertRaises(ValueError):
                    registry.swift_releases("example/numerics", exact="1.0.0")

    def test_invalid_exact_metadata_fails_before_transport(self):
        sent = self.transport({})
        for value in (True, 1, "", "main", "v1.0.0", "1.0.0-beta.1", "../1.0.0"):
            with self.subTest(version=value), self.assertRaises(ValueError):
                registry.swift_releases("example/numerics", exact=value)
        with self.assertRaises(ValueError):
            registry.swift_releases("../other", exact="1.0.0")
        self.assertEqual(sent, [])


class PublicationObservationTests(unittest.TestCase):
    def setUp(self):
        self.anchor = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
        self.observed = self.anchor + timedelta(minutes=2)
        clock = patch.object(
            registry, "observation_time", return_value=self.observed, create=True
        )
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def exception(self, version="2.0.0", expiry=None):
        return {
            "package": "npm:demo",
            "version": version,
            "minimum_safe": version,
            "reason": "verified fix for the declared advisory",
            "advisory": "https://example.invalid/advisory/clock",
            "expires": (expiry or self.anchor + timedelta(days=1)).isoformat(),
        }

    def test_observed_post_anchor_releases_do_not_abort_or_become_eligible(self):
        old = registry.Release("1.0.0", self.anchor - timedelta(days=60))
        during = registry.Release("2.0.0", self.anchor + timedelta(minutes=1))
        prerelease = registry.Release("3.0.0-beta.1", during.published)
        for provider in (
            "npm",
            "pypi",
            "crates",
            "pub",
            "go",
            "swift",
            "maven",
            "github",
            "docker",
        ):
            for days in (0, 30):
                with self.subTest(provider=provider, minimum_age_days=days):
                    selected = registry.select(
                        provider,
                        [old, during, prerelease],
                        {"minimum_age_days": days},
                        "demo",
                        self.anchor,
                    )
                    self.assertIs(selected, old)

    def test_observation_cannot_advance_frozen_age_boundary(self):
        old = registry.Release("1.0.0", self.anchor - timedelta(days=60))
        boundary = registry.Release(
            "2.0.0", self.anchor - timedelta(days=30) + timedelta(minutes=1)
        )
        for observed in (self.observed, self.observed + timedelta(days=90)):
            self.clock.return_value = observed
            self.assertIs(
                registry.select("npm", [old, boundary], {}, "demo", self.anchor), old
            )

    def test_exact_security_exception_cannot_admit_post_anchor_release(self):
        during = registry.Release("2.0.0", self.anchor + timedelta(minutes=1))
        policy = {"minimum_age_days": 0, "exceptions": [self.exception()]}
        self.assertEqual(
            registry.active_exceptions("npm", [during], policy, "demo", self.anchor), []
        )
        with self.assertRaisesRegex(ValueError, "eligible"):
            registry.select("npm", [during], policy, "demo", self.anchor)

    def test_duplicate_publication_cannot_hide_post_anchor_identity(self):
        releases = [
            registry.Release("2.0.0", self.anchor - timedelta(days=60)),
            registry.Release("2.0.0", self.anchor + timedelta(minutes=1)),
        ]
        policy = {"exceptions": [self.exception()]}
        self.assertEqual(
            registry.active_exceptions("npm", releases, policy, "demo", self.anchor), []
        )

    def test_exception_expiry_uses_the_same_frozen_anchor(self):
        young = registry.Release("2.0.0", self.anchor - timedelta(days=1))
        policy = {
            "exceptions": [self.exception(expiry=self.anchor + timedelta(minutes=1))]
        }
        self.assertEqual(
            registry.active_exceptions("npm", [young], policy, "demo", self.anchor),
            [young],
        )
        self.clock.return_value += timedelta(days=90)
        self.assertIs(
            registry.select("npm", [young], policy, "demo", self.anchor), young
        )
        policy["exceptions"][0]["expires"] = self.anchor.isoformat()
        with self.assertRaisesRegex(ValueError, "Expired"):
            registry.select("npm", [young], policy, "demo", self.anchor)

    def test_invalid_observed_publication_never_constructs_immutable_evidence(self):
        for published in (
            None,
            self.anchor.replace(tzinfo=None),
            self.observed + timedelta(seconds=1),
        ):
            for constructor in (
                lambda value: registry.Release("1.0.0", value),
                lambda value: registry.Artifact(
                    "https://example.invalid/archive", "sha256:" + "a" * 64, value
                ),
            ):
                with self.subTest(published=published, constructor=constructor):
                    with self.assertRaises(ValueError):
                        constructor(published)


class PolicyTests(unittest.TestCase):
    def test_latest_mature_major_and_boundary(self):
        chosen = registry.select(
            "npm",
            [release("1.0.0", 100), release("2.0.0", 30), release("3.0.0", 29)],
            {},
            "demo",
            NOW,
        )
        self.assertEqual(chosen.version, "2.0.0")

    def test_mature_lock_still_obeys_documented_constraint(self):
        policy = {
            "constraints": {
                "pypi:demo": {"range": "<2", "reason": "temporary API compatibility"}
            }
        }
        with self.assertRaisesRegex(ValueError, "eligible|constraint|policy"):
            registry.select("pypi", [release("2.0", 40)], policy, "demo", NOW)

    def test_exception_retires_when_mature_safe_alternative_exists(self):
        policy = {
            "exceptions": [
                {
                    "package": "npm:demo",
                    "version": "1.3.0",
                    "minimum_safe": "1.2.3",
                    "reason": "fix verified advisory",
                    "advisory": "https://example.invalid/advisory/1",
                    "expires": "2026-09-01T00:00:00Z",
                }
            ]
        }
        candidates = [release("1.2.2", 100), release("1.2.3", 40), release("1.3.0", 1)]
        self.assertEqual(
            registry.select("npm", candidates, policy, "demo", NOW).version, "1.2.3"
        )
        self.assertNotIn(
            "1.3.0",
            [
                r.version
                for r in registry.eligible("npm", candidates, policy, "demo", NOW)
            ],
        )
        self.assertNotIn(
            "1.2.2",
            [
                r.version
                for r in registry.eligible("npm", candidates, policy, "demo", NOW)
            ],
        )

    def test_an_unchanged_lock_cannot_bypass_the_security_safe_floor(self):
        identity = ("npm", "demo", "1.0.0", "", "sha256:" + "a" * 64)
        policy = {
            "exceptions": [
                {
                    "package": "npm:demo",
                    "version": "1.1.0",
                    "minimum_safe": "1.1.0",
                    "reason": "Verified supported-line correction.",
                    "advisory": "https://example.invalid/advisory",
                    "expires": "2026-09-01T00:00:00Z",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "safe floor"):
            updates.audit_identities(Path("."), {identity}, {identity}, policy, NOW)

    def test_no_age_evidence_and_invalid_age_fail_closed(self):
        with self.assertRaises(ValueError):
            registry.select("npm", [], {}, "demo", NOW)
        for days in (-1, True, "30"):
            with self.subTest(days=days), self.assertRaises(ValueError):
                registry.select(
                    "npm",
                    [release("1.0.0", 40)],
                    {"minimum_age_days": days},
                    "demo",
                    NOW,
                )

    def test_expired_exception_retires_only_with_eligible_safe_release(self):
        exception = {
            "package": "npm:demo",
            "version": "2.0.0",
            "minimum_safe": "2.0.0",
            "reason": "specific security fix",
            "advisory": "https://example.invalid/security/1",
            "expires": NOW.isoformat(),
        }
        policy = {"exceptions": [exception]}
        self.assertEqual(
            registry.select("npm", [release("2.0.0", 30)], policy, "demo", NOW).version,
            "2.0.0",
        )
        with self.assertRaisesRegex(ValueError, "Expired"):
            registry.select("npm", [release("2.0.0", 29)], policy, "demo", NOW)
        bounded = copy.deepcopy(policy)
        bounded["constraints"] = {
            "npm:demo": {"range": "<2", "reason": "explicit compatibility limit"}
        }
        with self.assertRaisesRegex(ValueError, "Expired"):
            registry.select("npm", [release("2.0.0", 90)], bounded, "demo", NOW)
        for key, bad in (
            ("expires", "invalid"),
            ("minimum_safe", "nonsense"),
            ("advisory", ""),
        ):
            invalid = copy.deepcopy(policy)
            invalid["exceptions"][0][key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.select("npm", [release("2.0.0", 90)], invalid, "demo", NOW)


class ArtifactTests(unittest.TestCase):
    """Use actual lock and registry field shapes; expected identity is independent."""

    providers = ("npm", "crates", "pypi", "pub")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="registry artifacts ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "modules").mkdir()
        for provider in self.providers:
            (self.root / provider).mkdir()
            (self.root / f"modules/{provider}.toml").write_text(
                f'name="{provider}"\ndirectory="{provider}"\necosystem="{provider}"\n'
            )

    def digest(self, provider, byte="a"):
        return (
            "sha512-" + base64.b64encode(byte.encode() * 64).decode()
            if provider == "npm"
            else byte * 64
        )

    def url(self, provider):
        return {
            "npm": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
            "pypi": "https://files.pythonhosted.org/packages/demo-1.0.0-py3-none-any.whl",
            "pub": "https://pub.dev/api/archives/demo-1.0.0.tar.gz",
        }.get(provider, "")

    def lock(self, provider, byte="a", url=None, missing=False):
        digest = self.digest(provider, byte)
        if provider == "npm":
            resolution = {} if missing else {"integrity": digest}
            if url is not None:
                resolution["tarball"] = url
            (self.root / provider / "pnpm-lock.yaml").write_text(
                json.dumps({"packages": {"demo@1.0.0": {"resolution": resolution}}})
            )
        elif provider == "crates":
            (self.root / provider / "Cargo.lock").write_text(
                '[[package]]\nname="demo"\nversion="1.0.0"\n'
                'source="registry+https://github.com/rust-lang/crates.io-index"\n'
                + ("" if missing else f'checksum="{digest}"\n')
            )
        elif provider == "pypi":
            artifact = f"url={json.dumps(url or self.url(provider))}"
            if not missing:
                artifact += f', hash="sha256:{digest}"'
            (self.root / provider / "uv.lock").write_text(
                '[[package]]\nname="demo"\nversion="1.0.0"\n'
                'source={registry="https://pypi.org/simple"}\nwheels=[{'
                + artifact
                + "}]\n"
            )
        else:
            description = {"name": "demo", "url": "https://pub.dev"}
            if not missing:
                description["sha256"] = digest
            (self.root / provider / "pubspec.lock").write_text(
                json.dumps(
                    {
                        "packages": {
                            "demo": {
                                "version": "1.0.0",
                                "source": "hosted",
                                "description": description,
                            }
                        }
                    }
                )
            )
        return updates.lock_identities(self.root, [provider])

    def metadata(self, provider, days=40, byte="a"):
        at = (NOW - timedelta(days=days)).isoformat()
        digest = self.digest(provider, byte)
        if provider == "npm":
            return {
                "time": {"1.0.0": at},
                "versions": {
                    "1.0.0": {
                        "dist": {"integrity": digest, "tarball": self.url(provider)}
                    }
                },
            }
        if provider == "crates":
            return {
                "versions": [
                    {
                        "num": "1.0.0",
                        "created_at": at,
                        "checksum": digest,
                        "yanked": False,
                    }
                ]
            }
        if provider == "pypi":
            return {
                "releases": {
                    "1.0.0": [
                        {
                            "url": self.url(provider),
                            "digests": {"sha256": digest},
                            "upload_time_iso_8601": at,
                            "yanked": False,
                            "requires_python": None,
                        }
                    ]
                }
            }
        return {
            "versions": [
                {
                    "version": "1.0.0",
                    "published": at,
                    "archive_url": self.url(provider),
                    "archive_sha256": digest,
                }
            ]
        }

    def audit(self, provider, before, metadata, policy=None):
        with patch.object(registry, "data", return_value=metadata):
            updates.audit_locks(self.root, [provider], before, policy or {}, NOW)

    def test_changed_hash_at_same_version_must_match_registry(self):
        for provider in self.providers:
            with self.subTest(provider=provider):
                before = self.lock(provider)
                self.lock(provider, "b")
                with self.assertRaisesRegex(
                    ValueError, "artifact|identity|checksum|integrity"
                ):
                    self.audit(provider, before, self.metadata(provider))

    def test_changed_matching_artifact_must_mature(self):
        for provider in self.providers:
            with self.subTest(provider=provider):
                before = self.lock(provider)
                self.lock(provider, "b")
                with self.assertRaisesRegex(ValueError, "age|eligible|mature"):
                    self.audit(
                        provider, before, self.metadata(provider, days=1, byte="b")
                    )
                self.audit(provider, before, self.metadata(provider, days=30, byte="b"))

    def test_unchanged_artifact_age_only_is_grandfathered(self):
        for provider in self.providers:
            with self.subTest(provider=provider):
                before = self.lock(provider)
                self.audit(provider, before, self.metadata(provider, days=1))
                for rule in (
                    {"range": "<1.0.0", "reason": "old API required"},
                    {"range": "<2.0.0"},
                ):
                    with self.assertRaisesRegex(ValueError, "constraint|reason|policy"):
                        self.audit(
                            provider,
                            before,
                            self.metadata(provider),
                            {"constraints": {f"{provider}:demo": rule}},
                        )

    def test_missing_lock_identity_fails_closed(self):
        for provider in self.providers:
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.lock(provider, missing=True)

    def test_missing_registry_identity_or_age_fails_even_for_unchanged(self):
        for provider in self.providers:
            before = self.lock(provider)
            for missing in ("identity", "age"):
                with self.subTest(provider=provider, missing=missing):
                    body = self.metadata(provider)
                    if provider == "npm":
                        del (
                            body["versions"]["1.0.0"]["dist"]
                            if missing == "identity"
                            else body["time"]
                        )["integrity" if missing == "identity" else "1.0.0"]
                    elif provider == "crates":
                        del body["versions"][0][
                            "checksum" if missing == "identity" else "created_at"
                        ]
                    elif provider == "pypi":
                        del body["releases"]["1.0.0"][0][
                            "digests"
                            if missing == "identity"
                            else "upload_time_iso_8601"
                        ]
                    else:
                        del body["versions"][0][
                            "archive_sha256" if missing == "identity" else "published"
                        ]
                    with self.assertRaises(ValueError):
                        self.audit(provider, before, body)

    def test_changed_url_at_same_hash_is_not_trusted(self):
        for provider in ("npm", "pypi"):
            with self.subTest(provider=provider):
                before = self.lock(provider, url=self.url(provider))
                self.lock(provider, url="https://other.example.invalid/repacked")
                with self.assertRaisesRegex(ValueError, "artifact|identity|URL"):
                    self.audit(provider, before, self.metadata(provider))

    def test_pypi_audits_each_actual_distribution_including_other_python(self):
        self.lock("pypi")
        body = self.metadata("pypi")
        fresh = copy.deepcopy(body["releases"]["1.0.0"][0])
        fresh.update(
            url="https://files.pythonhosted.org/packages/demo-1.0.0.tar.gz",
            digests={"sha256": "b" * 64},
            requires_python=">=99",
            upload_time_iso_8601=(NOW - timedelta(days=1)).isoformat(),
        )
        body["releases"]["1.0.0"].append(fresh)
        self.audit(
            "pypi", set(), body
        )  # An unselected young file must not age an old locked wheel.
        before = updates.lock_identities(self.root, ["pypi"])
        path = self.root / "pypi/uv.lock"
        path.write_text(
            path.read_text()
            + f'sdist={{url="{fresh["url"]}", hash="sha256:{"b" * 64}"}}\n'
        )
        with self.assertRaisesRegex(ValueError, "age|eligible|mature"):
            self.audit("pypi", before, body)

    def exception(self, provider="pypi"):
        return {
            "package": f"{provider}:demo",
            "version": "1.0.0",
            "minimum_safe": "1.0.0",
            "reason": "reviewed fix",
            "advisory": "https://example.invalid/advisory",
            "expires": "2026-09-01T00:00:00Z",
        }

    def test_exact_active_exception_admits_young_artifact(self):
        for provider in self.providers:
            with self.subTest(provider=provider):
                self.lock(provider)
                self.audit(
                    provider,
                    set(),
                    self.metadata(provider, days=1),
                    {"exceptions": [self.exception(provider)]},
                )

    def test_uv_override_is_exact_scoped_and_retires(self):
        policy = {"exceptions": [self.exception()]}
        with patch.object(registry, "data", return_value=self.metadata("pypi", days=1)):
            options = updates.uv_resolution_options(policy, NOW)
        self.assertEqual(
            options,
            [
                "--exclude-newer",
                (NOW - timedelta(days=30)).isoformat(),
                "--exclude-newer-package",
                f"demo={NOW.isoformat()}",
                "--upgrade-package",
                "demo==1.0.0",
            ],
        )
        with patch.object(
            registry, "data", return_value=self.metadata("pypi", days=30)
        ):
            self.assertEqual(updates.uv_resolution_options(policy, NOW), options[:2])
        policy["exceptions"][0]["expires"] = NOW.isoformat()
        with (
            patch.object(registry, "data", return_value=self.metadata("pypi", days=1)),
            self.assertRaises(ValueError),
        ):
            updates.uv_resolution_options(policy, NOW)

    def test_future_artifact_and_expired_exception_are_never_grandfathered(self):
        before = self.lock("pypi")
        with self.assertRaisesRegex(ValueError, "Future"):
            self.audit(
                "pypi",
                before,
                self.metadata("pypi", days=-1),
                {"exceptions": [self.exception()]},
            )
        exception = self.exception()
        exception["expires"] = NOW.isoformat()
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.audit(
                "pypi",
                before,
                self.metadata("pypi", days=1),
                {"exceptions": [exception]},
            )

    def test_mature_safe_alternative_retires_both_uv_and_lock_exception(self):
        self.lock("pypi")
        body = self.metadata("pypi", days=1)
        body["releases"]["0.9.0"] = self.metadata("pypi", days=40)["releases"]["1.0.0"]
        exception = self.exception()
        exception["minimum_safe"] = "0.9.0"
        policy = {"exceptions": [exception]}
        with self.assertRaisesRegex(ValueError, "mature|eligible"):
            self.audit("pypi", set(), body, policy)
        with patch.object(registry, "data", return_value=body):
            self.assertEqual(
                updates.uv_resolution_options(policy, NOW),
                ["--exclude-newer", (NOW - timedelta(days=30)).isoformat()],
            )

    def test_python_constraint_names_follow_registry_normalization(self):
        before = self.lock("pypi")
        with self.assertRaisesRegex(ValueError, "constraint"):
            self.audit(
                "pypi",
                before,
                self.metadata("pypi"),
                {"constraints": {"pypi:DEMO": {"range": "<1", "reason": "old API"}}},
            )

    def test_npm_legacy_shasum_is_real_identity_evidence(self):
        body = self.metadata("npm")
        dist = body["versions"]["1.0.0"]["dist"]
        del dist["integrity"]
        dist["shasum"] = "a" * 40
        path = self.root / "npm/pnpm-lock.yaml"
        path.write_text(
            json.dumps(
                {
                    "packages": {
                        "demo@1.0.0": {
                            "resolution": {
                                "integrity": "sha1-"
                                + base64.b64encode(bytes.fromhex("a" * 40)).decode()
                            }
                        }
                    }
                }
            )
        )
        self.audit("npm", set(), body)

    def test_lock_cannot_mix_registry_hash_with_another_source(self):
        self.lock("npm")
        path = self.root / "npm/pnpm-lock.yaml"
        body = json.loads(path.read_text())
        body["packages"]["demo@1.0.0"]["resolution"].update(
            type="directory", directory="../other"
        )
        path.write_text(json.dumps(body))
        with self.assertRaisesRegex(ValueError, "source|resolution"):
            updates.lock_identities(self.root, ["npm"])

    def test_uv_manifest_persists_cutoff_and_replaces_then_retires_old_overrides(self):
        path = self.root / "pypi/pyproject.toml"
        path.write_text(
            '[project]\nname="demo"\nversion="0.1.0"\n[tool.uv]\n'
            'package=false\nexclude-newer-package={unreviewed="2030-01-01"}\n'
        )
        spec = {"directory": "pypi"}
        policy = {"exceptions": [self.exception()]}
        with patch.object(registry, "data", return_value=self.metadata("pypi", days=1)):
            options = updates.uv_resolution_options(policy, NOW)
        updates.configure_uv(self.root, spec, options)
        content = tomllib.loads(path.read_text())
        self.assertEqual(content["project"]["name"], "demo")
        self.assertEqual(
            content["tool"]["uv"],
            {
                "package": False,
                "exclude-newer": options[1],
                "exclude-newer-package": {"demo": NOW.isoformat()},
            },
        )
        updates.configure_uv(self.root, spec, options[:2])
        self.assertNotIn(
            "exclude-newer-package", tomllib.loads(path.read_text())["tool"]["uv"]
        )

    def test_uv_date_only_resolution_retains_original_bytes_but_not_policy_retirement(
        self,
    ):
        spec = {"directory": "pypi"}
        path = self.root / "pypi/pyproject.toml"
        path.write_text('[project]\nname="demo"\nversion="0.1.0"\n')
        prior = ["--exclude-newer", (NOW - timedelta(days=31)).isoformat()]
        updates.configure_uv(self.root, spec, prior)
        manifest = path.read_text()
        lock = self.root / "pypi/uv.lock"
        old_lock = f'version=1\n[options]\nexclude-newer="{prior[1]}"\n'
        lock.write_text(old_lock)
        latest = ["--exclude-newer", (NOW - timedelta(days=30)).isoformat()]
        configured = updates.configure_uv(self.root, spec, latest)
        lock.write_text(old_lock.replace(prior[1], latest[1]))
        updates.retain_uv_noop(self.root, spec, manifest, old_lock, configured)
        self.assertEqual(path.read_text(), manifest)
        self.assertEqual(lock.read_text(), old_lock)
        # Removing an active override is a real policy change, even without new artifacts.
        active = prior + ["--exclude-newer-package", f"demo={NOW.isoformat()}"]
        updates.configure_uv(self.root, spec, active)
        manifest = path.read_text()
        old_lock += f'[options.exclude-newer-package]\ndemo="{NOW.isoformat()}"\n'
        configured = updates.configure_uv(self.root, spec, latest)
        lock.write_text(f'version=1\n[options]\nexclude-newer="{latest[1]}"\n')
        updates.retain_uv_noop(self.root, spec, manifest, old_lock, configured)
        self.assertNotIn("exclude-newer-package", path.read_text())
        self.assertNotEqual(lock.read_text(), old_lock)


if __name__ == "__main__":
    unittest.main()
