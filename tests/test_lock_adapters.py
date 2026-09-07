"""Independent source, artifact, and age oracles for native ecosystem locks."""

import base64
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lock_adapters
import manifests
import registry
import updates

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)
GO = "github.com/google/uuid"
SWIFT = "apple/swift-argument-parser"
MAVEN = "org.jetbrains.skiko:skiko-awt"
SHA = "a" * 64
H1 = "h1:" + base64.b64encode(b"a" * 32).decode()


class NativeLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-lock-evidence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for kind in ("go", "swift", "maven"):
            self.write(
                f"modules/{kind}.toml",
                f'name="{kind}"\ndirectory="{kind}"\necosystem="{kind}"\n',
            )
        self.write(
            "go/sub/go.mod",
            "module example.test/local\n\ngo 1.24\nrequire github.com/google/uuid v1.6.0\n",
        )
        self.write(
            "swift/Package.swift",
            '// swift-tools-version: 5.10\nimport PackageDescription\nlet package = Package(name: "Demo", dependencies: [.package(url: "https://github.com/apple/swift-argument-parser", exact: "1.5.0")])\n',
        )
        self.lock("go")
        self.lock("swift")
        self.lock("maven")

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def lock(self, kind, checksum=None):
        if kind == "go":
            self.write(
                "go/sub/go.sum",
                f"{GO} v1.6.0 {checksum or H1}\n{GO} v1.6.0/go.mod {H1}\n",
            )
        elif kind == "swift":
            self.write(
                "swift/Package.resolved",
                json.dumps(
                    {
                        "version": 3,
                        "originHash": "b" * 64,
                        "pins": [
                            {
                                "identity": "swift-argument-parser",
                                "kind": "remoteSourceControl",
                                "location": f"https://github.com/{SWIFT}",
                                "state": {
                                    "version": "1.5.0",
                                    "revision": checksum or "a" * 40,
                                },
                            }
                        ],
                    }
                ),
            )
        else:
            self.write(
                "maven/settings-gradle.lockfile", "empty=incomingCatalogForLibs0\n"
            )
            for target in (
                "linux-arm64",
                "linux-x64",
                "macos-arm64",
                "macos-x64",
                "windows-x64",
            ):
                self.write(
                    f"maven/gradle/dependency-locks/{target}.lockfile",
                    f"{MAVEN}:0.9.4.2=runtimeClasspath\n",
                )
            self.write(
                "maven/gradle/verification-metadata.xml",
                '<verification-metadata xmlns="https://schema.gradle.org/dependency-verification">'
                "<configuration><verify-metadata>true</verify-metadata></configuration><components>"
                '<component group="org.jetbrains.skiko" name="skiko-awt" version="0.9.4.2">'
                f'<artifact name="skiko-awt-0.9.4.2.jar"><sha256 value="{checksum or SHA}"/></artifact>'
                "</component></components></verification-metadata>",
            )
        return updates.lock_identities(self.root, [kind])

    def native(self, root, profile, argv, **kwargs):
        if argv[-3:] == ["env", "-json", "GOWORK"]:
            return {
                "GOWORK": str(self.root / "go/go.work")
                if (self.root / "go/go.work").is_file()
                else ""
            }
        if argv[:4] == ["go", "mod", "edit", "-json"]:
            return {
                "Module": {"Path": "example.test/local"},
                "Require": [{"Path": GO, "Version": "v1.6.0"}],
            }
        if argv[:3] == ["go", "list", "-m"]:
            return {
                "Path": GO,
                "Version": argv[-1].rsplit("@", 1)[1],
                "Versions": ["v1.6.0"],
            }
        if argv[:2] == ["swift", "package"]:
            return {
                "dependencies": [
                    {
                        "sourceControl": [
                            {
                                "identity": "swift-argument-parser",
                                "location": {
                                    "remote": [
                                        {"urlString": f"https://github.com/{SWIFT}"}
                                    ]
                                },
                                "requirement": {"exact": ["1.5.0"]},
                            }
                        ]
                    }
                ]
            }
        self.fail(f"Unexpected native query: {argv}")

    def audit(self, kind, before, days=40, checksum=None, policy=None, missing=None):
        at = NOW - timedelta(days=days)

        def data(url):
            if url.endswith(".info"):
                return {
                    "Version": "v1.6.0",
                    **({} if missing == "age" else {"Time": at.isoformat()}),
                }
            if "/releases?" in url:
                return [
                    {
                        "tag_name": "v1.5.0",
                        "draft": False,
                        "prerelease": False,
                        "published_at": None if missing == "age" else at.isoformat(),
                    }
                ]
            if "/git/ref/tags/" in url:
                return {"object": {"type": "commit", "sha": checksum or "a" * 40}}
            self.fail(f"Unexpected metadata query: {url}")

        def fetch(url, accept="application/json", method="GET"):
            if "/lookup/" in url:
                return (
                    f"123\n{GO} v1.6.0 {checksum or H1}\n"
                    + ("" if missing == "hash" else f"{GO} v1.6.0/go.mod {H1}\n")
                    + "\nsigned tree\n"
                ).encode(), {}
            if url.endswith(".sha256"):
                return ("" if missing == "hash" else checksum or SHA).encode(), {}
            if method == "HEAD":
                return b"", {} if missing == "age" else {
                    "Last-Modified": format_datetime(at, usegmt=True)
                }
            self.fail(f"Unexpected artifact query: {url}")

        with (
            patch.object(registry, "data", side_effect=data),
            patch.object(registry, "fetch", side_effect=fetch),
            patch.object(lock_adapters, "native", side_effect=self.native),
        ):
            updates.audit_locks(self.root, [kind], before, policy or {}, NOW)

    def test_each_new_artifact_requires_thirty_days(self):
        for kind in ("go", "swift", "maven"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "mature|eligible"):
                    self.audit(kind, set(), days=29)
                self.audit(kind, set(), days=30)

    def test_unchanged_artifact_exempts_only_age(self):
        for kind, package, bound in (
            ("go", GO, "<1.0.0"),
            ("swift", SWIFT, "<1.0.0"),
            ("maven", MAVEN, "<0.9"),
        ):
            with self.subTest(kind=kind):
                before = updates.lock_identities(self.root, [kind])
                self.audit(kind, before, days=1)
                with self.assertRaisesRegex(ValueError, "constraint"):
                    self.audit(
                        kind,
                        before,
                        policy={
                            "constraints": {
                                f"{kind}:{package}": {
                                    "range": bound,
                                    "reason": "API requirement",
                                }
                            }
                        },
                    )
                with self.assertRaisesRegex(ValueError, "Future"):
                    self.audit(kind, before, days=-1)
                with self.assertRaises((ValueError, TypeError)):
                    self.audit(kind, before, missing="age")

    def test_changed_hash_or_revision_must_match_current_public_evidence(self):
        for kind, checksum in (
            ("go", "h1:" + base64.b64encode(b"b" * 32).decode()),
            ("swift", "b" * 40),
            ("maven", "b" * 64),
        ):
            with self.subTest(kind=kind):
                before = updates.lock_identities(self.root, [kind])
                self.lock(kind, checksum)
                with self.assertRaisesRegex(ValueError, "identity|artifact"):
                    self.audit(kind, before)
                with self.assertRaisesRegex(ValueError, "mature|eligible"):
                    self.audit(kind, before, days=1, checksum=checksum)

    def test_go_checks_recursive_work_sums_and_module_and_go_mod_hashes(self):
        self.write("go/nested/go.work.sum", f"{GO} v1.6.0/go.mod {H1}\n")
        identities = updates.lock_identities(self.root, ["go"])
        self.assertEqual(len(identities), 2)
        self.assertEqual({i[3].rsplit(".", 1)[-1] for i in identities}, {"mod", "zip"})
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.audit("go", identities, missing="hash")

    def test_go_proxy_creation_time_and_sumdb_bind_exact_case_and_version(self):
        with patch.object(
            registry,
            "data",
            return_value={"Version": "v1.2.3", "Time": "2020-01-01T00:00:00Z"},
        ) as data:
            registry.go_info("github.com/Example/Thing", "v1.2.3")
        self.assertEqual(
            data.call_args.args[0],
            "https://proxy.golang.org/github.com/!example/!thing/@v/v1.2.3.info",
        )
        with (
            patch.object(
                registry,
                "data",
                return_value={"Version": "v1.2.4", "Time": "2020-01-01T00:00:00Z"},
            ),
            self.assertRaises(ValueError),
        ):
            registry.go_info(GO, "v1.2.3")
        self.assertTrue(registry.go_version("v0.0.0-20200101000000-abcdef123456"))
        self.assertIsNone(registry.version("go", "v0.0.0-20200101000000-abcdef123456"))

    def test_native_go_retraction_is_checked_for_unchanged_module(self):
        before = updates.lock_identities(self.root, ["go"])
        with patch.object(
            lock_adapters,
            "go_query",
            return_value={"Retracted": ["Security regression"]},
        ):
            with self.assertRaisesRegex(ValueError, "retracted"):
                self.audit("go", before)

    def test_go_selection_uses_native_unretracted_list(self):
        releases = [
            registry.Release(v, NOW - timedelta(days=40)) for v in ("v1.0.0", "v1.1.0")
        ]
        with (
            patch.object(registry, "releases", return_value=releases),
            patch.object(
                lock_adapters, "go_query", return_value={"Versions": ["v1.0.0"]}
            ),
        ):
            self.assertEqual(
                [r.version for r in lock_adapters.go_candidates(self.root, GO)],
                ["v1.0.0"],
            )

    def test_missing_go_and_swift_locks_fail_when_manifest_requires_them(self):
        for kind, filename in (
            ("go", "go/sub/go.sum"),
            ("swift", "swift/Package.resolved"),
        ):
            with self.subTest(kind=kind):
                (self.root / filename).unlink()
                with self.assertRaisesRegex(ValueError, "lacks|identity"):
                    self.audit(kind, set())

    def test_go_remote_replacement_cannot_bypass_source_audit(self):
        body = {
            "Module": {"Path": "example.test/local"},
            "Require": [{"Path": GO, "Version": "v1.6.0"}],
            "Replace": [
                {
                    "Old": {"Path": GO},
                    "New": {"Path": "other.example/module", "Version": "v1.0.0"},
                }
            ],
        }
        with (
            patch.object(
                lock_adapters,
                "native",
                side_effect=lambda root, profile, argv, **kwargs: (
                    {"GOWORK": ""} if kwargs.get("workspace") else body
                ),
            ),
            self.assertRaisesRegex(ValueError, "replacement"),
        ):
            lock_adapters.validate_go_sources(self.root, {"directory": "go"}, set())

    def test_unselected_go_module_cannot_substitute_for_missing_remote_lock(self):
        self.write("go/unselected/go.mod", f"module {GO}\n\ngo 1.24\n")

        def native(root, profile, argv, **kwargs):
            if argv[-1].endswith("unselected/go.mod"):
                return {"Module": {"Path": GO}}
            return self.native(root, profile, argv, **kwargs)

        with (
            patch.object(lock_adapters, "native", side_effect=native),
            self.assertRaisesRegex(ValueError, "checksum"),
        ):
            lock_adapters.validate_go_sources(self.root, {"directory": "go"}, set())

    def test_swift_branch_registry_credentials_and_non_github_sources_fail(self):
        path = self.root / "swift/Package.resolved"
        original = path.read_text()
        for source in (
            "https://user:secret@github.com/apple/swift-argument-parser",
            "https://other.example/apple/repo",
            "https://github.com/apple/repo?token=secret",
        ):
            body = json.loads(original)
            body["pins"][0]["location"] = source
            path.write_text(json.dumps(body))
            with self.assertRaises(ValueError):
                updates.lock_identities(self.root, ["swift"])
        for field, value in (("branch", "main"), ("revision", ""), ("version", None)):
            body = json.loads(original)
            body["pins"][0]["state"][field] = value
            path.write_text(json.dumps(body))
            with self.assertRaises(ValueError):
                updates.lock_identities(self.root, ["swift"])

    def test_swift_v1_and_v2_resolved_schemas_are_real_source_identities(self):
        for schema in (1, 2):
            item = {"state": {"version": "1.5.0", "revision": "a" * 40}}
            if schema == 1:
                item.update(
                    package="ArgumentParser",
                    repositoryURL=f"https://github.com/{SWIFT}.git",
                )
                body = {"version": 1, "object": {"pins": [item]}}
            else:
                item.update(
                    identity="swift-argument-parser",
                    kind="remoteSourceControl",
                    location=f"https://github.com/{SWIFT}.git",
                )
                body = {"version": 2, "pins": [item]}
            self.write("swift/Package.resolved", json.dumps(body))
            self.assertEqual(
                next(iter(updates.lock_identities(self.root, ["swift"])))[1:3],
                (SWIFT, "1.5.0"),
            )

    def test_swift_release_tag_must_resolve_to_exact_locked_revision(self):
        before = updates.lock_identities(self.root, ["swift"])
        with self.assertRaisesRegex(ValueError, "identity"):
            self.audit("swift", before, checksum="b" * 40)

    def test_explicit_go_and_swift_pins_preserve_exact_version_syntax(self):
        for kind, file, name, value in (
            ("go", "go/sub/go.mod", GO, "v1.7.0"),
            ("swift", "swift/Package.swift", SWIFT, "1.6.0"),
        ):
            pin = {
                "provider": kind,
                "name": name,
                "file": file,
                "format": "regex",
                "pattern": r"uuid (?P<value>v[0-9.]+)"
                if kind == "go"
                else r'exact: "(?P<value>[0-9.]+)"',
            }
            self.assertTrue(
                manifests.replace(pin, registry.Release(value, NOW), self.root)
            )
            self.assertIn(value, (self.root / file).read_text())
            self.assertFalse(
                manifests.replace(pin, registry.Release(value, NOW), self.root)
            )

    def test_maven_stable_four_component_numeric_order_and_constraints(self):
        self.assertGreater(
            registry.version("maven", "0.9.4.2"), registry.version("maven", "0.9.4")
        )
        self.assertTrue(registry.compatible("maven", "0.9.4.2", ">=0.9.4,<0.10"))
        for value in ("0.9.4-rc1", "1.0-SNAPSHOT", "0.9.4.2-dev", "v1.0"):
            self.assertIsNone(registry.version("maven", value))

    def test_every_platform_lock_and_settings_lock_needs_artifact_evidence(self):
        for file in (
            "maven/settings-gradle.lockfile",
            "maven/gradle/dependency-locks/windows-x64.lockfile",
        ):
            path = self.root / file
            old = path.read_text()
            path.write_text(old + "org.example:unverified:1.0.0=runtimeClasspath\n")
            with self.assertRaisesRegex(ValueError, "lacks"):
                updates.lock_identities(self.root, ["maven"])
            path.write_text(old)

    def test_declared_missing_platform_lock_fails(self):
        path = self.root / "modules/maven.toml"
        path.write_text(
            path.read_text()
            + 'artifacts=["maven/gradle/dependency-locks/windows-x64.lockfile"]\n'
        )
        (self.root / "maven/gradle/dependency-locks/windows-x64.lockfile").unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            updates.lock_identities(self.root, ["maven"])

    def test_maven_missing_or_multiple_checksum_and_source_traversal_fail(self):
        path = self.root / "maven/gradle/verification-metadata.xml"
        original = path.read_text()
        for change in (
            original.replace(f'<sha256 value="{SHA}"/>', ""),
            original.replace(
                f'<sha256 value="{SHA}"/>',
                f'<sha256 value="{SHA}"><also-trust value="{"b" * 64}"/></sha256>',
            ),
            original.replace("skiko-awt-0.9.4.2.jar", "../unrelated.jar"),
        ):
            path.write_text(change)
            with self.assertRaises(ValueError):
                updates.lock_identities(self.root, ["maven"])

    def test_google_repository_is_declared_and_group_scoped(self):
        with self.assertRaisesRegex(ValueError, "undeclared"):
            lock_adapters.maven_repository({}, "androidx.annotation:annotation")
        self.assertEqual(
            lock_adapters.maven_repository(
                {"maven_repositories": ["central", "google"]},
                "androidx.annotation:annotation",
            ),
            "google",
        )
        with self.assertRaisesRegex(ValueError, "scope"):
            registry.maven_prefix("org.example:artifact", "google")
        with self.assertRaises(ValueError):
            lock_adapters.maven_repository(
                {"maven_repositories": ["https://untrusted.invalid"]}, MAVEN
            )

    def test_symlink_nested_lock_is_not_followed(self):
        (self.root / "go/escape").symlink_to(
            self.root / "go/sub", target_is_directory=True
        )
        (self.root / "go/go.work.sum").symlink_to(self.root / "go/sub/go.sum")
        with self.assertRaisesRegex(ValueError, "symlink"):
            updates.lock_identities(self.root, ["go"])

    def test_native_queries_enter_fresh_selected_profile(self):
        result = type("Result", (), {"stdout": '{"Path":"github.com/google/uuid"}'})()
        with (
            patch.object(lock_adapters, "environment", return_value={}),
            patch.object(lock_adapters, "managed_run", return_value=result) as run,
        ):
            lock_adapters.native(
                self.root, "go", ["go", "list", "-m", "-json", GO + "@v1.6.0"]
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0][1], "go")
        self.assertEqual(kwargs["env"]["TOOLCHAIN_FRESH"], "1")
        self.assertEqual(kwargs["env"]["GOSUMDB"], "sum.golang.org")
        self.assertEqual(kwargs["env"]["GOPROXY"], "https://proxy.golang.org")

    def test_young_artifact_security_exception_is_exact_expiring_and_retires(self):
        for kind, package, value, floor in (
            ("go", GO, "v1.6.0", "v1.5.0"),
            ("swift", SWIFT, "1.5.0", "1.4.0"),
            ("maven", MAVEN, "0.9.4.2", "0.9.4.1"),
        ):
            exception = {
                "package": f"{kind}:{package}",
                "version": value,
                "minimum_safe": floor,
                "reason": "verified security fix",
                "advisory": "https://example.invalid/advisory",
                "expires": "2026-09-01T00:00:00Z",
            }
            candidates = [registry.Release(value, NOW - timedelta(days=1), "v" + value)]
            target = {
                "go": "go_candidates",
                "maven": "maven_releases",
                "swift": "releases",
            }[kind]
            owner = lock_adapters if kind == "go" else registry
            with (
                self.subTest(kind=kind),
                patch.object(owner, target, return_value=candidates),
            ):
                self.audit(kind, set(), days=1, policy={"exceptions": [exception]})
                candidates.append(registry.Release(floor, NOW - timedelta(days=30)))
                with self.assertRaisesRegex(ValueError, "mature|eligible"):
                    self.audit(kind, set(), days=1, policy={"exceptions": [exception]})
                exception["expires"] = NOW.isoformat()
                # The mature safe alternative retires this exception; unchanged
                # baseline bytes retain only their ordinary age grandfathering.
                self.audit(
                    kind,
                    updates.lock_identities(self.root, [kind]),
                    days=1,
                    policy={"exceptions": [exception]},
                )
                candidates.pop()
                with self.assertRaisesRegex(ValueError, "Expired"):
                    self.audit(
                        kind,
                        updates.lock_identities(self.root, [kind]),
                        days=1,
                        policy={"exceptions": [exception]},
                    )

    def test_plugin_portal_hashes_actual_bytes_not_redirected_checksum_sidecar(self):
        modified = format_datetime(NOW - timedelta(days=40), usegmt=True)

        class Response(io.BytesIO):
            headers = {"Last-Modified": modified}

        def fetch(url, accept, method):
            self.assertEqual(method, "HEAD")
            self.assertFalse(url.endswith(".sha256"))
            return b"", {"Last-Modified": modified}

        with (
            patch.object(registry, "fetch", side_effect=fetch),
            patch.object(registry, "urlopen", return_value=Response(b"abc")) as opened,
        ):
            artifact = registry.maven_artifact(
                "org.jetbrains.compose:compose-gradle-plugin",
                "1.1.0",
                "compose-gradle-plugin-1.1.0.jar",
                "plugins",
            )
        self.assertEqual(
            artifact.digest,
            "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(artifact.published, NOW - timedelta(days=40))
        self.assertEqual(opened.call_args.args[0].full_url, artifact.url)

    def test_plugin_routing_requires_exact_declared_coordinate_and_origin(self):
        package = "org.jetbrains.compose:compose-gradle-plugin"
        spec = {
            "maven_repositories": ["central", "plugins"],
            "maven_plugin_packages": [package],
        }
        self.assertEqual(lock_adapters.maven_repository(spec, package), "plugins")
        self.assertEqual(
            lock_adapters.maven_repository(
                spec, "org.jetbrains.compose:other-gradle-plugin"
            ),
            "central",
        )
        with self.assertRaisesRegex(ValueError, "undeclared"):
            lock_adapters.maven_repository(
                {"maven_plugin_packages": [package]}, package
            )
        with self.assertRaises(ValueError):
            registry.maven_prefix("org.unrelated:unexpected-gradle-plugin", "plugins")

    def test_maven_hash_fallback_requires_unchanged_artifact_date(self):
        class Response(io.BytesIO):
            headers = {"Last-Modified": "Wed, 01 Jul 2026 00:00:00 GMT"}

        with (
            patch.object(registry, "urlopen", return_value=Response(b"abc")),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            registry.maven_content_digest(
                "https://repo.maven.apache.org/maven2/org/example/demo/1.0/demo-1.0.jar",
                "Thu, 02 Jul 2026 00:00:00 GMT",
            )


class GoReplacementTests(unittest.TestCase):
    """Native Go is the independent oracle for replacement version and scope."""

    source_root = Path(__file__).resolve().parents[1]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="go-replacement-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.write(
            "go/sub/go.mod",
            f"module example.test/local\n\ngo 1.24\nrequire {GO} v1.6.0\n",
        )
        self.write(
            "go/sub/main.go",
            f'package main\nimport "{GO}"\nfunc main() {{ println(uuid.Value) }}\n',
        )
        self.replacement("go/replacement", "selected")
        # Bootstrap an isolated file proxy with the real Go resolver. The oracle
        # must not depend on a developer's pre-existing public module cache.
        self.write("proxy/" + GO + "/@v/v1.6.0.mod", f"module {GO}\n\ngo 1.24\n")
        self.write(
            "proxy/" + GO + "/@v/v1.6.0.info",
            '{"Version":"v1.6.0","Time":"2024-01-01T00:00:00Z"}',
        )
        self.write(
            "bootstrap/go.mod",
            f"module example.invalid/bootstrap\n\ngo 1.24\nrequire {GO} v1.6.0\n",
        )
        lock_adapters.native(
            self.source_root,
            "go",
            [
                "env",
                "GOMODCACHE=" + str(self.root / "module-cache"),
                "GOPROXY=" + (self.root / "proxy").as_uri(),
                "GOSUMDB=off",
                "GOWORK=off",
                "go",
                "-C",
                str(self.root / "bootstrap"),
                "list",
                "-mod=mod",
                "-m",
                "-json",
                GO,
            ],
        )
        self.env = patch.dict(os.environ, {"GOWORK": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def replacement(self, directory, value):
        self.write(directory + "/go.mod", f"module {GO}\n\ngo 1.24\n")
        self.write(directory + "/uuid.go", f'package uuid\nconst Value = "{value}"\n')

    def audit(self, items=None):
        actual = lock_adapters.native
        with patch.object(
            lock_adapters,
            "native",
            side_effect=lambda root, profile, argv, **kwargs: actual(
                self.source_root, profile, argv, **kwargs
            ),
        ):
            lock_adapters.validate_go_sources(
                self.root, {"directory": "go"}, items or set()
            )

    def resolved(self, workspace="off"):
        # Explicit offline, read-only native resolution: no registry result can
        # accidentally make a nonapplicable local replacement look legitimate.
        return lock_adapters.native(
            self.source_root,
            "go",
            [
                "env",
                "GOMODCACHE=" + str(self.root / "module-cache"),
                "GOPROXY=off",
                "GOSUMDB=off",
                "GOWORK=" + workspace,
                "go",
                "-C",
                str(self.root / "go/sub"),
                "list",
                "-mod=readonly",
                "-m",
                "-json",
                GO,
            ],
        )

    def test_version_specific_replacement_does_not_waive_other_required_version(self):
        manifest = self.root / "go/sub/go.mod"
        manifest.write_text(
            manifest.read_text() + f"replace {GO} v1.0.0 => ../replacement\n"
        )
        self.assertNotIn("Replace", self.resolved())
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.audit()

    def test_matching_exact_and_versionless_local_replacements_are_legitimate(self):
        manifest = self.root / "go/sub/go.mod"
        original = manifest.read_text()
        for old_version in (" v1.6.0", ""):
            with self.subTest(old_version=old_version):
                manifest.write_text(
                    original + f"replace {GO}{old_version} => ../replacement\n"
                )
                self.assertEqual(
                    Path(self.resolved()["Replace"]["Dir"]),
                    self.root / "go/replacement",
                )
                self.audit()

    def test_active_workspace_local_replacement_is_applied_to_member(self):
        work = self.write(
            "go/go.work", f"go 1.24\nuse ./sub\nreplace {GO} => ./replacement\n"
        )
        resolved = self.resolved(str(work))
        self.assertEqual(Path(resolved["Replace"]["Dir"]), self.root / "go/replacement")
        package = lock_adapters.native(
            self.source_root,
            "go",
            [
                "env",
                "GOPROXY=off",
                "GOSUMDB=off",
                "GOWORK=" + str(work),
                "go",
                "-C",
                str(self.root / "go/sub"),
                "list",
                "-mod=readonly",
                "-json",
                "./...",
            ],
        )
        self.assertEqual(package["ImportPath"], "example.test/local")
        self.assertFalse((self.root / "go/sub/go.sum").exists())
        self.audit()

    def test_workspace_override_supersedes_member_remote_replacement(self):
        manifest = self.root / "go/sub/go.mod"
        manifest.write_text(
            manifest.read_text()
            + f"replace {GO} v1.6.0 => other.example/remote v1.0.0\n"
        )
        work = self.write(
            "go/go.work", f"go 1.24\nuse ./sub\nreplace {GO} => ./replacement\n"
        )
        self.assertEqual(
            Path(self.resolved(str(work))["Replace"]["Dir"]),
            self.root / "go/replacement",
        )
        self.audit()

    def test_unrelated_nested_workspace_cannot_waive_remote_member_requirement(self):
        self.write("go/nested/go.work", "go 1.24\nuse ../replacement\n")
        self.assertNotIn("Replace", self.resolved())
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.audit()

    def test_active_workspace_use_does_not_waive_unrelated_module_requirement(self):
        self.write("go/go.work", "go 1.24\nuse ./replacement\n")
        # The selected command workspace contains only the replacement module;
        # the unrelated recursive sub module is still a standalone remote user.
        self.assertNotIn("Replace", self.resolved())
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.audit()

    def test_workspace_exact_replacement_does_not_cover_other_versions(self):
        work = self.write(
            "go/go.work", f"go 1.24\nuse ./sub\nreplace {GO} v1.0.0 => ./replacement\n"
        )
        self.assertNotIn("Replace", self.resolved(str(work)))
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.audit()

    def test_workspace_exact_replacement_overrides_member_wildcard(self):
        manifest = self.root / "go/sub/go.mod"
        manifest.write_text(
            manifest.read_text() + f"replace {GO} => other.example/remote v1.0.0\n"
        )
        work = self.write(
            "go/go.work", f"go 1.24\nuse ./sub\nreplace {GO} v1.6.0 => ./replacement\n"
        )
        self.assertEqual(
            Path(self.resolved(str(work))["Replace"]["Dir"]),
            self.root / "go/replacement",
        )
        self.audit()

    def test_member_replacement_applies_to_other_active_members(self):
        self.write(
            "go/helper/go.mod",
            f"module example.test/helper\n\ngo 1.24\nreplace {GO} => ../replacement\n",
        )
        work = self.write("go/go.work", "go 1.24\nuse (\n./sub\n./helper\n)\n")
        self.assertEqual(
            Path(self.resolved(str(work))["Replace"]["Dir"]),
            self.root / "go/replacement",
        )
        self.audit()

    def test_conflicting_member_replacements_require_workspace_override(self):
        self.replacement("go/other", "other")
        self.write(
            "go/helper/go.mod",
            f"module example.test/helper\n\ngo 1.24\nreplace {GO} => ../other\n",
        )
        manifest = self.root / "go/sub/go.mod"
        manifest.write_text(manifest.read_text() + f"replace {GO} => ../replacement\n")
        work = self.write("go/go.work", "go 1.24\nuse (\n./sub\n./helper\n)\n")
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.resolved(str(work))
        self.assertIn("conflicting replacements", error.exception.stderr)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.audit()
        work.write_text(work.read_text() + f"replace {GO} => ./replacement\n")
        self.assertEqual(
            Path(self.resolved(str(work))["Replace"]["Dir"]),
            self.root / "go/replacement",
        )
        self.audit()

    def test_ancestor_workspace_inside_project_is_selected_by_real_go(self):
        work = self.write(
            "go.work", f"go 1.24\nuse ./go/sub\nreplace {GO} => ./go/replacement\n"
        )
        self.assertEqual(
            Path(self.resolved(str(work))["Replace"]["Dir"]),
            self.root / "go/replacement",
        )
        self.audit()

    def test_gowork_off_cannot_inherit_an_existing_workspace_exemption(self):
        self.write("go/go.work", f"go 1.24\nuse ./sub\nreplace {GO} => ./replacement\n")
        with patch.dict(os.environ, {"GOWORK": "off"}):
            self.assertNotIn("Replace", self.resolved())
            with self.assertRaisesRegex(ValueError, "checksum"):
                self.audit()

    def test_explicit_workspace_outside_project_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="external-go-workspace-") as tmp:
            work = Path(tmp) / "external.work"
            work.write_text(
                f"go 1.24\nuse {self.root / 'go/sub'}\nreplace {GO} => {self.root / 'go/replacement'}\n"
            )
            self.assertEqual(
                Path(self.resolved(str(work))["Replace"]["Dir"]),
                self.root / "go/replacement",
            )
            with (
                patch.dict(os.environ, {"GOWORK": str(work)}),
                self.assertRaisesRegex(ValueError, "outside.*root"),
            ):
                self.audit()


if __name__ == "__main__":
    unittest.main()
