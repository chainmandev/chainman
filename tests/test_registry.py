"""Policy oracles apply equally to requested releases and resolver-selected locks."""

from datetime import datetime, timedelta, timezone
import base64
import copy
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registry
import updates

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def release(version, days):
    return registry.Release(version, NOW - timedelta(days=days))


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
