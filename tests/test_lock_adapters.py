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


def swift_graph_node(url, version="unspecified", dependencies=(), *, path=None):
    return {
        "identity": url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower(),
        "name": url.rstrip("/").rsplit("/", 1)[-1],
        "url": url,
        "version": version,
        "path": str(path) if path is not None else url,
        "dependencies": list(dependencies),
    }


class NativeLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native-lock-evidence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "toolchain.toml").write_text('schema=1\nmodules=["swift"]\n')
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
            if "show-dependencies" in argv:
                scratch = Path(argv[argv.index("--scratch-path") + 1])
                self.assertIn("--force-resolved-versions", argv)
                return swift_graph_node(
                    str(self.root / "swift"),
                    dependencies=[
                        swift_graph_node(
                            f"https://github.com/{SWIFT}",
                            "1.5.0",
                            path=scratch / "checkouts/swift-argument-parser",
                        )
                    ],
                )
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
            if "/commits/" in url:
                return {
                    "sha": checksum or "a" * 40,
                    "commit": {"committer": {"date": at.isoformat()}},
                }
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
            patch.object(registry, "fetch", return_value=(b"v1.0.0\nv1.1.0\n", {})),
            patch.object(
                registry,
                "go_info",
                side_effect=lambda package, value: next(
                    item for item in releases if item.version == value
                ),
            ) as metadata,
            patch.object(
                lock_adapters, "go_query", return_value={"Versions": ["v1.0.0"]}
            ),
        ):
            self.assertEqual(
                [r.version for r in lock_adapters.go_candidates(self.root, GO)],
                ["v1.0.0"],
            )
            metadata.assert_called_once_with(GO, "v1.0.0")

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
            registry.maven_prefix("org.unrelated:ordinary-library", "plugins")
        marker = "dev.example.compiler:dev.example.compiler.gradle.plugin"
        configured = {
            "maven_repositories": ["central", "plugins"],
            "maven_plugin_packages": [marker],
        }
        self.assertEqual(lock_adapters.maven_repository(configured, marker), "plugins")
        self.assertEqual(
            lock_adapters.maven_repository(
                configured, "dev.example.other:dev.example.other.gradle.plugin"
            ),
            "central",
        )

    def test_swift_retargeted_release_uses_newer_commit_age(self):
        import source_updates

        items = updates.lock_identities(self.root, ["swift"])
        published = NOW - timedelta(days=90)
        changed = NOW - timedelta(days=1)
        with (
            patch.object(
                registry,
                "releases",
                return_value=[registry.Release("1.5.0", published, "v1.5.0")],
            ),
            patch.object(registry, "github_commit", return_value="a" * 40),
            patch.object(source_updates, "commit_time", return_value=changed),
        ):
            evidence = lock_adapters.evidence(self.root, "swift", SWIFT, items)
            self.assertEqual(evidence[0].published, changed)
            self.assertTrue(all(a.published == changed for a in evidence[0].artifacts))

    def test_swift_exception_uses_commit_age_for_retained_and_new_artifacts(self):
        import source_updates

        current = updates.lock_identities(self.root, ["swift"])
        exception = {
            "package": "swift:" + SWIFT,
            "version": "1.5.0",
            "minimum_safe": "1.5.0",
            "reason": "Security repair",
            "advisory": "https://example.invalid/advisory",
            "expires": (NOW - timedelta(days=1)).isoformat(),
        }
        with (
            patch.object(
                registry,
                "github_releases",
                return_value=[
                    registry.Release("v1.5.0", NOW - timedelta(days=90), "v1.5.0")
                ],
            ),
            patch.object(registry, "github_commit", return_value="a" * 40),
            patch.object(
                source_updates, "commit_time", return_value=NOW - timedelta(days=1)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Expired"):
                updates.audit_identities(
                    self.root, current, current, {"exceptions": [exception]}, NOW
                )
            active = {**exception, "expires": (NOW + timedelta(days=2)).isoformat()}
            updates.audit_identities(
                self.root, current, set(), {"exceptions": [active]}, NOW
            )

    def test_older_duplicate_metadata_cannot_retire_needed_exception(self):
        exception = {
            "package": "maven:sample:library",
            "version": "1.0.0",
            "minimum_safe": "1.0.0",
            "reason": "Security repair",
            "advisory": "https://example.invalid/advisory",
            "expires": (NOW - timedelta(days=1)).isoformat(),
        }
        values = [
            registry.Release("1.0.0", NOW - timedelta(days=60)),
            registry.Release("1.0.0", NOW - timedelta(days=1)),
        ]
        for ordered in (values, list(reversed(values))):
            with self.assertRaisesRegex(ValueError, "Expired"):
                registry.active_exceptions(
                    "maven", ordered, {"exceptions": [exception]}, "sample:library", NOW
                )

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


class SwiftSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swift declarations ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "toolchain.toml").write_text('schema=1\nmodules=["swift"]\n')
        (self.root / "app").mkdir()
        (self.root / "local").mkdir()
        self.manifest = self.root / "app/Package.swift"
        self.spec = {"directory": "app", "ecosystem": "swift"}
        (self.root / "local/Package.swift").write_text(
            "// empty local dependency inventory\n"
        )

    def check(self, text, dependencies, items=()):
        self.manifest.write_text(text)
        lock = self.root / "app/Package.resolved"
        lock.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pins": [
                        {
                            "kind": "remoteSourceControl",
                            "location": item[3],
                            "state": {
                                "version": item[2],
                                "revision": item[4].removeprefix("git:"),
                            },
                        }
                        for item in items
                    ],
                }
            )
        )

        def evaluate(root, profile, argv, **kwargs):
            directory = argv[argv.index("--package-path") + 1]
            if "show-dependencies" in argv:
                scratch = Path(argv[argv.index("--scratch-path") + 1])
                children = []
                for entry in dependencies:
                    if "sourceControl" in entry:
                        package = entry["sourceControl"][0]["location"]["remote"][0][
                            "urlString"
                        ]
                        value = next(item[2] for item in items if item[3] == package)
                        children.append(
                            swift_graph_node(
                                package,
                                value,
                                path=scratch / "checkouts" / package.rsplit("/", 1)[-1],
                            )
                        )
                    elif "fileSystem" in entry:
                        children.append(
                            swift_graph_node(entry["fileSystem"][0]["path"])
                        )
                return swift_graph_node(directory, dependencies=children)
            return {
                "dependencies": dependencies
                if directory == str(self.root / "app")
                else []
            }

        with patch.object(lock_adapters, "native", side_effect=evaluate):
            lock_adapters.validate_swift_sources(self.root, self.spec, set(items))

    def remote(self, requirement, url="https://github.com/example/package"):
        return {
            "sourceControl": [
                {
                    "identity": "package",
                    "location": {"remote": [{"urlString": url}]},
                    "requirement": requirement,
                    "productFilter": None,
                }
            ]
        }

    def test_native_exact_and_both_next_major_ranges(self):
        for style, lower, selected, requirement in (
            ("exact", "1.2.3", "1.2.3", {"exact": ["1.2.3"]}),
            (
                "from",
                "1.2.3",
                "1.8.0",
                {"range": [{"lowerBound": "1.2.3", "upperBound": "2.0.0"}]},
            ),
            (
                "from",
                "0.63.2",
                "0.65.0",
                {"range": [{"lowerBound": "0.63.2", "upperBound": "1.0.0"}]},
            ),
        ):
            with self.subTest(style=style, lower=lower):
                self.check(
                    f'.package(url: "https://github.com/example/package", {style}: "{lower}")',
                    [self.remote(requirement)],
                    [
                        (
                            "swift",
                            "example/package",
                            selected,
                            "https://github.com/example/package",
                            "git:" + "a" * 40,
                        )
                    ],
                )

    def test_native_literal_local_path_and_name_must_match(self):
        text = '.package(name: "LocalName", path: "../local")'
        source = {
            "fileSystem": [
                {
                    "identity": "local",
                    "path": str(self.root / "local"),
                    "nameForTargetDependencyResolutionOnly": "LocalName",
                    "productFilter": None,
                }
            ]
        }
        self.check(text, [source])
        self.check(
            '.package(path: "../local")',
            [{"fileSystem": [{"path": str(self.root / "local")}]}],
        )
        for field, value in (
            ("path", str(self.root / "app")),
            ("nameForTargetDependencyResolutionOnly", "Wrong"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                changed = json.loads(json.dumps(source))
                changed["fileSystem"][0][field] = value
                self.check(text, [changed])
        with self.assertRaises(ValueError):
            self.check(text, [self.remote({"exact": ["1.2.3"]})])

    def test_native_inventory_and_requirement_substitutions_fail(self):
        text = '.package(url: "https://github.com/example/package", from: "0.63.2")'
        good = self.remote({"range": [{"lowerBound": "0.63.2", "upperBound": "1.0.0"}]})
        items = [
            (
                "swift",
                "example/package",
                "0.65.0",
                "https://github.com/example/package",
                "git:" + "a" * 40,
            )
        ]
        for dependencies in (
            None,
            {},
            [],
            [good, good],
            [self.remote({"branch": ["main"]})],
            [
                self.remote(
                    {"range": [{"lowerBound": "0.63.2", "upperBound": "0.64.0"}]}
                )
            ],
            [self.remote({"exact": ["0.65.0"]})],
            [
                self.remote(
                    good["sourceControl"][0]["requirement"],
                    "https://github.com/example/other",
                )
            ],
            [{"fileSystem": [{"path": str(self.root / "local")}]}],
            [{"registry": []}],
        ):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValueError):
                self.check(text, dependencies, items)
        for changed_items in ([], [("swift", "example/package", "1.0.0", "", "")]):
            with self.subTest(items=changed_items), self.assertRaises(ValueError):
                self.check(text, [good], changed_items)
        self.manifest.write_text(text)
        for malformed in ({}, [], {"dependencies": "invalid"}):
            with (
                patch.object(lock_adapters, "native", return_value=malformed),
                self.assertRaises(ValueError),
            ):
                lock_adapters.validate_swift_sources(self.root, self.spec, set(items))

    def test_literal_parser_rejects_unsupported_or_ambiguous_calls(self):
        call = '.package(url: "https://github.com/example/package", from: "1.2.3")'
        for text in (
            call + "," + call,
            call.replace("from:", "branch:"),
            call.replace('"1.2.3"', "minimumVersion"),
            call.replace('"1.2.3"', '"v1.2.3"'),
            call.replace("https://github.com/", "https://elsewhere.invalid/"),
            ".package(path: computePath())",
            '.package(path: "../../outside")',
            call.replace("from:", 'exact: "1.2.3", from:'),
        ):
            self.manifest.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                lock_adapters.swift_declarations(self.root, "app/Package.swift")
        (self.root / "alias").symlink_to(self.root / "local", target_is_directory=True)
        self.manifest.write_text('.package(path: "../alias/../local")')
        with self.assertRaises(ValueError):
            lock_adapters.swift_declarations(self.root, "app/Package.swift")

    def test_comment_and_string_calls_are_not_dependency_declarations(self):
        self.manifest.write_text(
            '// .package(path: "../not-real")\nlet example = ".package()"\n'
            '.package( /* retain comment */ url: "https://github.com/example/package", from: "1.2.3")'
        )
        declarations = lock_adapters.swift_declarations(self.root, "app/Package.swift")
        self.assertEqual(len(declarations), 1)
        self.assertEqual(declarations[0]["package"], "example/package")


class SwiftGraphTests(unittest.TestCase):
    # Native graph shape and warm-cache behavior are independent Swift SDK oracles.
    write = NativeLockTests.write

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="swift local graph ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "toolchain.toml").write_text('schema=1\nmodules=["swift"]\n')
        self.spec = {
            "directory": "app",
            "ecosystem": "swift",
            "profile": "custom-swift",
        }
        self.url = "https://github.com/apple/swift-numerics"
        self.app = self.write("app/Package.swift", '.package(path: "../bridge")\n')
        self.bridge = self.write(
            "bridge/Package.swift", f'.package(url: "{self.url}", exact: "1.0.2")\n'
        )
        self.app.chmod(0o640)
        self.bridge.chmod(0o640)
        self.lock = self.root / "app/Package.resolved"
        self.set_lock([(self.url, "1.0.2")])
        self.all_local = False
        self.transitive = False
        self.graph_calls = []
        self.graph_change = lambda graph: graph

    def set_lock(self, entries):
        self.lock.write_text(
            json.dumps(
                {
                    "version": 3,
                    "pins": [
                        {
                            "kind": "remoteSourceControl",
                            "location": url,
                            "state": {"version": value, "revision": "a" * 40},
                        }
                        for url, value in entries
                    ],
                }
            )
        )
        self.lock.chmod(0o640)

    def native(self, root, profile, argv, **kwargs):
        self.assertEqual(profile, "custom-swift")
        directory = argv[argv.index("--package-path") + 1]
        if "show-dependencies" in argv:
            self.assertIn("--force-resolved-versions", argv)
            self.assertNotIn("--skip-update", argv)
            scratch = Path(argv[argv.index("--scratch-path") + 1])
            self.assertTrue(scratch.is_relative_to(self.root / ".cache/toolchain/work"))
            self.graph_calls.append(argv)
            remote = (
                []
                if self.all_local
                else [
                    swift_graph_node(
                        self.url,
                        "1.0.2",
                        path=scratch / "checkouts/swift-numerics",
                        dependencies=[
                            swift_graph_node(
                                "https://github.com/example/transitive",
                                "2.0.0",
                                path=scratch / "checkouts/transitive",
                            )
                        ]
                        if self.transitive
                        else [],
                    )
                ]
            )
            return self.graph_change(
                swift_graph_node(
                    str(self.root / "app"),
                    dependencies=[
                        swift_graph_node(str(self.root / "bridge"), dependencies=remote)
                    ],
                )
            )
        if directory == str(self.root / "app"):
            return {
                "dependencies": [{"fileSystem": [{"path": str(self.root / "bridge")}]}]
            }
        self.assertEqual(directory, str(self.root / "bridge"))
        return {
            "dependencies": []
            if self.all_local
            else [
                {
                    "sourceControl": [
                        {
                            "location": {"remote": [{"urlString": self.url}]},
                            "requirement": {"exact": ["1.0.2"]},
                        }
                    ]
                }
            ]
        }

    def validate(self):
        with patch.object(lock_adapters, "native", side_effect=self.native):
            lock_adapters.validate_swift_sources(
                self.root, self.spec, lock_adapters.identities(self.root, self.spec)
            )

    def test_real_shaped_local_remote_and_all_local_graphs(self):
        self.validate()
        self.assertEqual(len(self.graph_calls), 1)
        self.all_local = True
        self.bridge.write_text("// genuinely all local\n")
        self.lock.unlink()
        self.validate()
        self.set_lock([])
        self.validate()
        self.assertEqual(len(self.graph_calls), 3)

    def test_missing_empty_and_nested_decoy_cannot_supply_the_root_lock(self):
        original = self.lock.read_bytes()
        self.write("app/unrelated/Package.resolved", original.decode())
        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    self.lock.unlink()
                else:
                    self.set_lock([])
                self.assertEqual(lock_adapters.identities(self.root, self.spec), set())
                with self.assertRaises(ValueError):
                    self.validate()
                self.assertEqual(
                    (self.root / "app/unrelated/Package.resolved").read_bytes(),
                    original,
                )

    def test_native_success_with_partial_lock_or_missing_graph_node_fails(self):
        self.transitive = True
        with self.assertRaisesRegex(ValueError, "lock inventory"):
            self.validate()
        self.assertEqual(len(self.graph_calls), 1)
        self.set_lock(
            [(self.url, "1.0.2"), ("https://github.com/example/transitive", "2.0.0")]
        )
        self.validate()
        self.transitive = False
        with self.assertRaisesRegex(ValueError, "lock inventory"):
            self.validate()
        self.assertEqual(len(self.graph_calls), 3)

    def test_malformed_missing_local_and_substituted_sources_fail(self):
        changes = [
            lambda graph: {},
            lambda graph: {**graph, "dependencies": []},
            lambda graph: {**graph, "url": str(self.root / "bridge")},
            lambda graph: {
                **graph,
                "dependencies": [
                    swift_graph_node(
                        "https://elsewhere.invalid/name", "1.0.0", path=self.root
                    )
                ],
            },
            lambda graph: {**graph, "source": "registry"},
            lambda graph: {**graph, "path": str(self.root)},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.graph_change = change
                self.validate()

    def test_audit_preserves_unexpected_manifest_and_lock_changes(self):
        baseline = {
            path: (path.read_bytes(), path.stat().st_mode)
            for path in (self.app, self.bridge, self.lock)
        }
        for path, mode_only in (
            (self.bridge, False),
            (self.bridge, True),
            (self.lock, False),
            (self.lock, True),
        ):

            def change(graph):
                if mode_only:
                    path.chmod(0o600)
                else:
                    path.write_bytes(baseline[path][0] + b" ")
                return graph

            with self.subTest(path=path, mode=mode_only):
                self.graph_change = change
                with self.assertRaisesRegex(ValueError, "changes preserved"):
                    self.validate()
                self.assertNotEqual(
                    (path.read_bytes(), path.stat().st_mode), baseline[path]
                )
                path.write_bytes(baseline[path][0])
                path.chmod(baseline[path][1])

    def test_native_failure_and_conflicting_edit_keep_original_diagnostic(self):
        error = subprocess.CalledProcessError(
            41,
            ["swift", "package"],
            output="original native output",
            stderr="original diagnostic",
        )

        def change(graph):
            self.bridge.write_text("// concurrent change\n")
            raise error

        self.graph_change = change
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.validate()
        self.assertIs(caught.exception, error)
        self.assertEqual(error.returncode, 41)
        self.assertEqual(error.stderr, "original diagnostic")
        self.assertTrue(any("changes preserved" in note for note in error.__notes__))
        self.assertEqual(self.bridge.read_text(), "// concurrent change\n")

    def test_local_closure_guards_components_and_handles_repeated_edges(self):
        self.bridge.write_text('.package(path: "../app")\n')
        self.assertEqual(
            set(lock_adapters.swift_manifest_state(self.root, self.spec)),
            {"app/Package.swift", "bridge/Package.swift"},
        )
        saved = self.bridge.read_bytes()
        self.bridge.unlink()
        self.bridge.symlink_to(self.app)
        with self.assertRaises(ValueError):
            lock_adapters.swift_manifest_state(self.root, self.spec)
        self.bridge.unlink()
        self.bridge.write_bytes(saved)
        (self.root / "alias").symlink_to(self.root / "bridge", target_is_directory=True)
        self.app.write_text('.package(path: "../alias/../bridge")\n')
        with self.assertRaises(ValueError):
            lock_adapters.swift_manifest_state(self.root, self.spec)
        self.app.write_text('.package(path: "../../outside")\n')
        with self.assertRaises(ValueError):
            lock_adapters.swift_manifest_state(self.root, self.spec)

    def test_independent_command_roots_use_distinct_scoped_graph_caches(self):
        observed = []

        def evaluate(root, profile, argv, **kwargs):
            self.assertEqual(profile, "custom-swift")
            self.assertIn("--force-resolved-versions", argv)
            directory = argv[argv.index("--package-path") + 1]
            observed.append(Path(argv[argv.index("--scratch-path") + 1]))
            return swift_graph_node(directory)

        with patch.object(lock_adapters, "native", side_effect=evaluate):
            for name in ("app", "other"):
                directory = self.root / name
                directory.mkdir(exist_ok=True)
                lock_adapters.validate_swift_graph(
                    self.root,
                    {**self.spec, "directory": name},
                    set(),
                    {str(directory): set()},
                )
        self.assertEqual(len(set(observed)), 2)
        self.assertTrue(
            all(
                path.is_relative_to(self.root / ".cache/toolchain/work")
                for path in observed
            )
        )


if __name__ == "__main__":
    unittest.main()
