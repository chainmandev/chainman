"""Neutral Cargo eligibility, native failure boundaries, and owned restoration."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ecosystem_updates as native
import registry

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class CargoResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Cargo eligibility space ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.put("chainman.toml", "schema=1\n")
        self.manifest = self.put(
            "Cargo.toml",
            '# public contract\n[package]\nname="app"\nversion="0.1.0"\n[dependencies]\nalias={package="parent",version="^1.0.0"}\n',
        )
        self.manifest.chmod(0o640)
        self.source = self.put("src/lib.rs", "// unchanged source\n")
        self.spec = {"adapter": "rust", "profile": "native", "mode": "compatible"}
        self.releases = {
            "parent": [
                self.release("parent", v, days)
                for v, days in [("1.0.0", 90), ("1.2.0", 60), ("1.2.1", 1)]
            ],
            "leaf": [
                self.release("leaf", v, days)
                for v, days in [
                    ("1.0.0", 90),
                    ("1.5.0", 60),
                    ("1.6.0", 1),
                    ("2.0.0", 60),
                ]
            ],
        }
        self.write_lock(self.root, {"parent": "1.0.0", "leaf": "1.0.0"})
        (self.root / "Cargo.lock").chmod(0o640)
        self.original_lock = native.cargo_file_state(self.root, "Cargo.lock")
        self.calls = []

    def put(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def release(self, name, version, days):
        published = NOW - timedelta(days=days)
        digest = "sha256:" + hashlib.sha256(f"{name}@{version}".encode()).hexdigest()
        return registry.Release(
            version,
            published,
            artifacts=(
                registry.Artifact(
                    f"https://crates.io/api/v1/crates/{name}/{version}/download",
                    digest,
                    published,
                ),
            ),
        )

    def write_lock(self, directory, versions, *, wrong=None, dependencies=None):
        body = "version=4\n"
        for name, version in versions.items():
            item = next(r for r in self.releases[name] if r.version == version)
            digest = (
                "0" * 64 if name == wrong else item.artifacts[0].digest.split(":")[1]
            )
            body += f'[[package]]\nname="{name}"\nversion="{version}"\nsource="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="{digest}"\n'
            if dependencies and name in dependencies:
                body += f"dependencies={dependencies[name]!r}\n"
        (directory / "Cargo.lock").write_text(body)

    def resolver(self, root, profile, argv, **kwargs):
        self.assertEqual(root, self.root)
        self.assertEqual(profile, "native")
        self.assertEqual(kwargs["env"]["TOOLCHAIN_FRESH"], "1")
        directory = kwargs["cwd"]
        document = native.manifests.document(directory / "Cargo.toml")[0]
        version = document["dependencies"]["alias"]["version"]
        self.calls.append((directory, list(argv), version))
        if len(argv) == 2:
            self.write_lock(
                directory,
                {
                    "parent": "1.2.0" if version == "=1.2.0" else "1.2.1",
                    "leaf": "1.6.0",
                },
            )
        else:
            self.assertEqual(argv[:2], ["cargo", "update"])
            self.assertEqual(
                argv[2:4],
                [
                    "-p",
                    "registry+https://github.com/rust-lang/crates.io-index#leaf@1.6.0",
                ],
            )
            self.assertEqual(argv[4], "--precise")
            value = argv[5]
            if value == "2.0.0":
                raise subprocess.CalledProcessError(
                    101,
                    argv,
                    output='Updating crates.io index\nerror: failed to select a version for the requirement `leaf = "<2"`\n',
                )
            self.write_lock(directory, {"parent": "1.2.0", "leaf": value})
        return subprocess.CompletedProcess(argv, 0, stdout="native fixture completed\n")

    def resolve(self, *, resolver=None, spec=None, policy=None):
        with (
            patch.object(
                registry,
                "releases",
                side_effect=lambda provider, name: self.releases[name],
            ),
            patch.object(
                native.chainman, "execute", side_effect=resolver or self.resolver
            ),
        ):
            return native.resolve(self.root, spec or self.spec, policy or {}, NOW)

    def test_coupled_parent_is_repaired_before_blocked_child(self):
        self.releases["z-parent"] = [
            self.release("z-parent", "1.0.0", 60),
            self.release("z-parent", "1.1.0", 1),
        ]
        attempts = []

        def coupled(root, profile, argv, **kwargs):
            document = native.manifests.document(self.manifest)[0]
            self.assertEqual(document["dependencies"]["alias"]["version"], "=1.2.0")
            self.assertEqual(native.CARGO_SOLVER_STATES, 64)
            if len(argv) == 2:
                versions = {"parent": "1.2.0", "leaf": "1.6.0", "z-parent": "1.1.0"}
            else:
                versions = {
                    item["name"]: item["version"]
                    for item in native.tomllib.loads(
                        (self.root / "Cargo.lock").read_text()
                    )["package"]
                }
                package = argv[3].split("#")[1].split("@")[0]
                attempts.append((package, argv[-1]))
                if package == "leaf" and (
                    versions["z-parent"] == "1.1.0" or argv[-1] == "2.0.0"
                ):
                    raise subprocess.CalledProcessError(
                        101,
                        argv,
                        output="error: failed to select a version for the requirement `leaf`\n",
                    )
                versions[package] = argv[-1]
            self.write_lock(
                self.root,
                versions,
                dependencies={"parent": ["z-parent"], "z-parent": ["leaf"]},
            )
            return subprocess.CompletedProcess(argv, 0, stdout="coupled fixture\n")

        result = self.resolve(resolver=coupled)
        self.assertEqual(
            attempts, [("z-parent", "1.0.0"), ("leaf", "2.0.0"), ("leaf", "1.5.0")]
        )
        self.assertTrue(
            any(
                item[:3] == ["crates", "leaf", "1.5.0"]
                for item in result["cargo_identities"]["rust-0"]
            )
        )
        self.assertIn('version="1.2.0"', self.manifest.read_text())
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o640)

    def test_latest_eligible_fallback_and_direct_exact_public_restoration(self):
        result = self.resolve()
        self.assertEqual([c[1][-1] for c in self.calls], ["update", "2.0.0", "1.5.0"])
        self.assertEqual({c[2] for c in self.calls}, {"=1.2.0"})
        self.assertIn('version="1.2.0"', self.manifest.read_text())
        self.assertTrue(self.manifest.read_text().startswith("# public contract\n"))
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o640)
        self.assertIn('version="1.5.0"', (self.root / "Cargo.lock").read_text())
        self.assertEqual(set(result["cargo_identities"]), {"rust-0"})

    def coordinated_resolver(
        self, *, wrong_target=False, young_peer=False, mutate=None
    ):
        for name in ("target", "peer"):
            self.releases[name] = [
                self.release(name, value, days)
                for value, days in [("0.9.0", 90), ("1.0.0", 60), ("1.1.0", 1)]
            ]
        source = "registry+https://github.com/rust-lang/crates.io-index"
        self.coordinated_calls = []

        def resolver(root, profile, argv, **kwargs):
            self.assertEqual(kwargs["cwd"], self.root)
            if len(argv) == 2:
                versions = {
                    "parent": "1.2.0",
                    "target": "1.1.0",
                    "peer": "1.0.0",
                    "leaf": "1.5.0",
                }
            else:
                self.coordinated_calls.append(list(argv))
                versions = {
                    item["name"]: item["version"]
                    for item in native.tomllib.loads(
                        (self.root / "Cargo.lock").read_text()
                    )["package"]
                }
                if argv[3] == f"{source}#peer@1.1.0":
                    self.assertEqual(
                        argv,
                        [
                            "cargo",
                            "update",
                            "-p",
                            f"{source}#peer@1.1.0",
                            "--precise",
                            "1.0.0",
                        ],
                    )
                    versions["peer"] = "1.0.0"
                elif len(argv) == 6:
                    raise subprocess.CalledProcessError(
                        101,
                        argv,
                        output="error: failed to select a version for `leaf`\n",
                    )
                else:
                    self.assertEqual(
                        argv,
                        [
                            "cargo",
                            "update",
                            "-p",
                            f"{source}#target@1.1.0",
                            "-p",
                            f"{source}#peer@1.0.0",
                            "--precise",
                            "1.0.0",
                        ],
                    )
                    if mutate is not None:
                        mutate()
                    versions["target"] = "0.9.0" if wrong_target else "1.0.0"
                    if young_peer:
                        versions["peer"] = "1.1.0"
            self.write_lock(
                self.root,
                versions,
                dependencies={
                    "parent": ["target", "peer"],
                    "target": ["leaf"],
                    "peer": ["leaf"],
                },
            )
            return subprocess.CompletedProcess(argv, 0, stdout="coordinated fixture\n")

        return resolver

    def test_coordinated_retry_includes_eligible_exact_peer(self):
        result = self.resolve(resolver=self.coordinated_resolver())
        self.assertEqual(len(self.coordinated_calls), 2)
        self.assertTrue(
            any(
                item[:3] == ["crates", "target", "1.0.0"]
                for item in result["cargo_identities"]["rust-0"]
            )
        )
        self.assertEqual(stat.S_IMODE((self.root / "Cargo.lock").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o640)

    def test_coordinated_success_with_wrong_eligible_target_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "requested precise registry identity"):
            self.resolve(resolver=self.coordinated_resolver(wrong_target=True))
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

    def test_coordinated_peer_is_not_exempt_from_maturity(self):
        result = self.resolve(resolver=self.coordinated_resolver(young_peer=True))
        self.assertEqual(len(self.coordinated_calls), 3)
        self.assertTrue(
            any(
                item[:3] == ["crates", "peer", "1.0.0"]
                for item in result["cargo_identities"]["rust-0"]
            )
        )

    def test_coordinated_retry_uses_the_same_attempt_budget(self):
        resolver = self.coordinated_resolver()
        with patch.object(native, "CARGO_SOLVER_STATES", 1):
            with self.assertRaisesRegex(ValueError, "1-state bound"):
                self.resolve(resolver=resolver)
        self.assertEqual(len(self.coordinated_calls), 1)
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )
        resolver = self.coordinated_resolver()
        with patch.object(native, "CARGO_SOLVER_STATES", 2):
            self.resolve(resolver=resolver)
        self.assertEqual(len(self.coordinated_calls), 2)

    def test_coordinated_retry_preserves_unexpected_source_change(self):
        with self.assertRaisesRegex(ValueError, "changed non-lock inputs"):
            self.resolve(
                resolver=self.coordinated_resolver(
                    mutate=lambda: self.source.write_text("// concurrent change\n"),
                )
            )
        self.assertEqual(self.source.read_text(), "// concurrent change\n")

    def test_coordinated_retry_retains_original_native_failure(self):
        resolver = self.coordinated_resolver()

        def fail(root, profile, argv, **kwargs):
            if len(argv) > 6:
                raise subprocess.CalledProcessError(
                    41, argv, output="error: download failed\n"
                )
            return resolver(root, profile, argv, **kwargs)

        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.resolve(resolver=fail)
        self.assertEqual(caught.exception.returncode, 41)
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

    def test_coordinated_search_never_runs_attempt_65(self):
        resolver = self.coordinated_resolver()
        self.releases["target"] = [self.release("target", "1.1.0", 1)] + [
            self.release("target", f"2.{index}.0", 60) for index in range(33)
        ]
        calls = []

        def fail(root, profile, argv, **kwargs):
            if len(argv) == 2:
                return resolver(root, profile, argv, **kwargs)
            calls.append(argv)
            raise subprocess.CalledProcessError(
                101, argv, output="error: failed to select a version for `leaf`\n"
            )

        with self.assertRaisesRegex(ValueError, "64-state bound"):
            self.resolve(resolver=fail)
        self.assertEqual(len(calls), 64)
        self.assertEqual([len(argv) for argv in calls], [6, 8] * 32)
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

    def test_individual_success_with_peer_does_not_retry(self):
        resolver = self.coordinated_resolver()
        calls = []

        def succeed(root, profile, argv, **kwargs):
            if len(argv) == 2:
                return resolver(root, profile, argv, **kwargs)
            calls.append(argv)
            self.assertEqual(len(argv), 6)
            self.write_lock(
                self.root,
                {
                    "parent": "1.2.0",
                    "target": "1.0.0",
                    "peer": "1.0.0",
                    "leaf": "1.5.0",
                },
                dependencies={
                    "parent": ["target", "peer"],
                    "target": ["leaf"],
                    "peer": ["leaf"],
                },
            )
            return subprocess.CompletedProcess(argv, 0, stdout="individual success\n")

        self.resolve(resolver=succeed)
        self.assertEqual(len(calls), 1)

    def test_no_eligible_graph_restores_owned_lock(self):
        self.releases["leaf"] = [
            r for r in self.releases["leaf"] if r.version != "1.5.0"
        ]

        def fail(root, profile, argv, **kwargs):
            if len(argv) > 2:
                self.calls.append((kwargs["cwd"], argv, "=1.2.0"))
                raise subprocess.CalledProcessError(
                    101, argv, output="error: failed to select a version for `leaf`\n"
                )
            return self.resolver(root, profile, argv, **kwargs)

        with self.assertRaisesRegex(ValueError, "No eligible Cargo graph"):
            self.resolve(resolver=fail)
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )
        self.assertIn('version="1.2.0"', self.manifest.read_text())

    def test_attempt_bound_restores_missing_and_existing_locks(self):
        for absent in [False, True]:
            with self.subTest(absent=absent):
                if absent:
                    (self.root / "Cargo.lock").unlink()
                with (
                    patch.object(native, "CARGO_SOLVER_STATES", 1),
                    self.assertRaisesRegex(ValueError, "1-state bound"),
                ):
                    self.resolve()
                self.assertEqual(
                    native.cargo_file_state(self.root, "Cargo.lock"),
                    None if absent else self.original_lock,
                )

    def test_unrelated_native_status_is_preserved_even_with_solver_words(self):
        for initial in [False, True]:
            for status in [23, 41, 101, -15]:
                with self.subTest(initial=initial, status=status):

                    def fail(root, profile, argv, **kwargs):
                        if initial or len(argv) > 2:
                            raise subprocess.CalledProcessError(
                                status,
                                argv,
                                output="error: download failed; failed to select a version for `leaf`\n",
                            )
                        return self.resolver(root, profile, argv, **kwargs)

                    with self.assertRaises(subprocess.CalledProcessError) as caught:
                        self.resolve(resolver=fail)
                    self.assertEqual(caught.exception.returncode, status)
                    self.assertEqual(
                        native.cargo_file_state(self.root, "Cargo.lock"),
                        self.original_lock,
                    )

    def test_full_graph_bad_checksum_after_age_issue_is_not_hidden(self):
        self.releases["zzbad"] = [self.release("zzbad", "1.0.0", 60)]

        def bad(root, profile, argv, **kwargs):
            result = self.resolver(root, profile, argv, **kwargs)
            self.write_lock(
                kwargs["cwd"],
                {"parent": "1.2.0", "leaf": "1.6.0", "zzbad": "1.0.0"},
                wrong="zzbad",
            )
            return result

        with self.assertRaisesRegex(ValueError, "absent from registry"):
            self.resolve(resolver=bad)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

    def test_future_artifact_and_missing_registry_evidence_fail_without_retry(self):
        for issue in ["future", "absent"]:
            with self.subTest(issue=issue):
                if issue == "future":
                    self.releases["leaf"] = [
                        self.release("leaf", r.version, -1)
                        if r.version == "1.6.0"
                        else r
                        for r in self.releases["leaf"]
                    ]
                else:
                    self.releases["leaf"] = [
                        r for r in self.releases["leaf"] if r.version != "1.6.0"
                    ]

                def native_output(root, profile, argv, **kwargs):
                    self.calls.append(argv)
                    (kwargs["cwd"] / "Cargo.lock").write_text(
                        'version=4\n[[package]]\nname="leaf"\nversion="1.6.0"\nsource="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="'
                        + hashlib.sha256(b"leaf@1.6.0").hexdigest()
                        + '"\n'
                    )
                    return subprocess.CompletedProcess(argv, 0, stdout="")

                count = len(self.calls)
                with self.assertRaises(ValueError):
                    self.resolve(resolver=native_output)
                self.assertEqual(len(self.calls), count + 1)

    def test_exact_young_baseline_retention_does_not_create_new_age_exemption(self):
        self.write_lock(self.root, {"parent": "1.0.0", "leaf": "1.6.0"})
        self.resolve()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('version="1.6.0"', (self.root / "Cargo.lock").read_text())

    def test_safe_floor_blocks_baseline_fallback_and_active_exception_is_exact(self):
        exception = {
            "package": "crates:leaf",
            "version": "1.6.0",
            "minimum_safe": "1.6.0",
            "reason": "specific fix",
            "advisory": "https://example.invalid/fix",
            "expires": (NOW + timedelta(days=1)).isoformat(),
        }
        policy = {
            "constraints": {
                "crates:leaf": {
                    "range": ">=1.6.0 <2.0.0",
                    "reason": "parent compatibility and safe floor",
                }
            },
            "exceptions": [exception],
        }
        self.resolve(policy=policy)
        self.assertEqual(len(self.calls), 1)
        # Removing the exact exception cannot authorize the unsafe adopted1.0.
        self.write_lock(self.root, {"parent": "1.0.0", "leaf": "1.0.0"})
        exception["expires"] = NOW.isoformat()
        with self.assertRaisesRegex(ValueError, "Expired"):
            self.resolve(policy=policy)

    def test_manifest_source_and_config_drift_is_preserved(self):
        for name in ["Cargo.toml", "src/lib.rs", ".cargo/config.toml"]:
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes() if path.exists() else None

                def drift(root, profile, argv, **kwargs):
                    result = self.resolver(root, profile, argv, **kwargs)
                    self.put(name, "unexpected change\n")
                    return result

                with self.assertRaises(
                    (ValueError, native.manifests.tomlkit.exceptions.ParseError)
                ):
                    self.resolve(resolver=drift)
                self.assertEqual(path.read_text(), "unexpected change\n")
                # Test-only reset between isolated controls, not production cleanup.
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_lock_structural_or_mode_change_is_preserved_without_outside_write(self):
        for issue in ["mode", "symlink", "missing"]:
            with self.subTest(issue=issue):
                lock = self.root / "Cargo.lock"
                outside = self.put("outside", "outside retained\n")

                def drift(root, profile, argv, **kwargs):
                    result = self.resolver(root, profile, argv, **kwargs)
                    if issue == "mode":
                        lock.chmod(0o600)
                    elif issue == "missing":
                        lock.unlink()
                    else:
                        lock.unlink()
                        lock.symlink_to(outside)
                    return result

                with self.assertRaises(ValueError):
                    self.resolve(resolver=drift)
                self.assertEqual(outside.read_text(), "outside retained\n")
                if issue == "mode":
                    self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
                elif issue == "missing":
                    self.assertFalse(lock.exists())
                else:
                    self.assertTrue(lock.is_symlink())
                if lock.is_symlink():
                    lock.unlink()
                lock.write_bytes(self.original_lock[0])
                lock.chmod(self.original_lock[1])

    def test_post_hook_cargo_graph_change_is_rejected(self):
        before = native.snapshot(self.root, self.spec)
        before["resolution"] = self.resolve()
        self.write_lock(self.root, {"parent": "1.0.0", "leaf": "1.5.0"})
        with self.assertRaisesRegex(ValueError, "selected Cargo artifact graph"):
            native.audit(self.root, self.spec, before, {}, NOW)

    def test_custom_resolver_stays_literal_and_audit_only(self):
        command = ["wrapper", "cargo", "update", "--custom", "opaque ; $(value)"]

        def custom(root, profile, argv, **kwargs):
            self.assertEqual(argv, command)
            self.assertIn('version="1.2.0"', self.manifest.read_text())
            self.write_lock(kwargs["cwd"], {"parent": "1.2.1", "leaf": "1.6.0"})

        with self.assertRaisesRegex(ValueError, "not mature"):
            self.resolve(resolver=custom, spec={**self.spec, "resolve": [command]})

    def test_two_workspaces_are_independent_and_later_failure_restores_both(self):
        second = self.put("second/Cargo.toml", self.manifest.read_text())
        self.put("second/src/lib.rs", "// second source\n")
        self.write_lock(second.parent, {"parent": "1.0.0", "leaf": "1.0.0"})
        initial = {
            name: native.cargo_file_state(self.root, name)
            for name in ["Cargo.lock", "second/Cargo.lock"]
        }
        spec = {**self.spec, "directories": [".", "second"]}
        self.resolve(spec=spec)
        self.assertEqual(
            {str(c[0].relative_to(self.root)) for c in self.calls}, {".", "second"}
        )
        for name, previous in initial.items():
            path = self.root / name
            path.write_bytes(previous[0])
            path.chmod(previous[1])

        def fail(root, profile, argv, **kwargs):
            if kwargs["cwd"] == second.parent:
                raise subprocess.CalledProcessError(
                    41, argv, output="second workspace failure\n"
                )
            return self.resolver(root, profile, argv, **kwargs)

        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.resolve(resolver=fail, spec=spec)
        self.assertEqual(caught.exception.returncode, 41)
        self.assertEqual(
            initial,
            {name: native.cargo_file_state(self.root, name) for name in initial},
        )

    def test_pinning_preserves_post_planning_caret_alias_and_inherited_fields(self):
        root = self.put(
            "Cargo.toml",
            '# frozen planned range\n[workspace]\nmembers=["member"]\n[workspace.dependencies]\nrenamed={package="parent",version="^1.2.0",features=["feature"]}\n',
        )
        member = self.put(
            "member/Cargo.toml",
            '[package]\nname="member"\nversion="0.1.0"\n[dependencies]\nrenamed={workspace=true}\n[target."cfg(unix)".build-dependencies]\nalias={package="parent",version="~2.0.0",optional=true}\n',
        )
        before = {
            name: native.cargo_file_state(self.root, name)
            for name in ["Cargo.toml", "member/Cargo.toml"]
        }
        pins = native.pins(
            self.root, self.spec, native.specifications(self.root, self.spec)
        )
        planned = [
            (
                pin,
                registry.Release(
                    "1.2.0" if pin["file"] == "Cargo.toml" else "2.0.0", NOW
                ),
            )
            for pin in pins
        ]
        with native.cargo_resolution_pins(self.root, planned):
            self.assertIn('version="=1.2.0"', root.read_text())
            self.assertIn('version="=2.0.0"', member.read_text())
            self.assertIn("renamed={workspace=true}", member.read_text())
            self.assertIn("optional=true", member.read_text())
        self.assertEqual(
            before, {name: native.cargo_file_state(self.root, name) for name in before}
        )

    def test_changed_prior_choice_backtracks_and_new_artifacts_are_checked(self):
        self.releases["leaf"] = [
            r for r in self.releases["leaf"] if r.version != "2.0.0"
        ]
        self.releases["child"] = [
            self.release("child", "1.0.0", 60),
            self.release("child", "1.1.0", 1),
        ]
        attempts = []

        def cascade(root, profile, argv, **kwargs):
            directory = kwargs["cwd"]
            if len(argv) == 2:
                return self.resolver(root, profile, argv, **kwargs)
            package = argv[3].split("#")[1].split("@")[0]
            value = argv[-1]
            attempts.append((package, value))
            if package == "leaf" and value == "1.5.0":
                self.write_lock(
                    directory, {"parent": "1.2.0", "leaf": "1.5.0", "child": "1.1.0"}
                )
            elif package == "child":
                # A native conservative update may still move a previous choice.
                self.write_lock(
                    directory, {"parent": "1.2.0", "leaf": "1.6.0", "child": "1.0.0"}
                )
            else:
                self.write_lock(directory, {"parent": "1.2.0", "leaf": "1.0.0"})
            return subprocess.CompletedProcess(argv, 0, stdout="cascade fixture\n")

        self.resolve(resolver=cascade)
        self.assertEqual(
            attempts, [("leaf", "1.5.0"), ("child", "1.0.0"), ("leaf", "1.0.0")]
        )
        self.assertIn('version="1.0.0"', (self.root / "Cargo.lock").read_text())

    def test_source_symlinks_are_recorded_without_following(self):
        target = self.put("owned.rs", "// owned source\n")
        self.source.unlink()
        self.source.symlink_to("../owned.rs")
        linked_directory = self.root / "src" / "linked-directory"
        linked_directory.symlink_to(
            self.root / "missing-source-directory", target_is_directory=True
        )
        self.resolve()
        self.assertEqual(self.source.readlink(), Path("../owned.rs"))
        self.assertEqual(target.read_text(), "// owned source\n")
        self.assertTrue(linked_directory.is_symlink())

    def test_changed_source_link_text_is_preserved_and_fails(self):
        self.put("first.rs", "// first\n")
        self.put("second.rs", "// second\n")
        self.source.unlink()
        self.source.symlink_to("../first.rs")

        def drift(root, profile, argv, **kwargs):
            result = self.resolver(root, profile, argv, **kwargs)
            self.source.unlink()
            self.source.symlink_to("../second.rs")
            return result

        with self.assertRaisesRegex(ValueError, "non-lock inputs"):
            self.resolve(resolver=drift)
        self.assertEqual(self.source.readlink(), Path("../second.rs"))
        self.assertEqual((self.root / "first.rs").read_text(), "// first\n")
        self.assertEqual((self.root / "second.rs").read_text(), "// second\n")

    def test_partial_direct_pin_setup_restores_every_known_safe_write(self):
        second = self.put("second/Cargo.toml", '[dependencies]\nother="^1.0.0"\n')
        pins = [
            (
                {"provider": "crates", "file": name, "pointer": pointer},
                registry.Release("1.2.0", NOW),
            )
            for name, pointer in [
                ("Cargo.toml", ["dependencies", "alias", "version"]),
                ("second/Cargo.toml", ["dependencies", "other"]),
            ]
        ]
        before = {
            name: native.cargo_file_state(self.root, name)
            for name in ["Cargo.toml", "second/Cargo.toml"]
        }
        atomic = native.tc.atomic_bytes

        def fail(path, data, mode):
            if path == second and b"=1.2.0" in data:
                raise OSError("second write failed")
            return atomic(path, data, mode)

        with (
            patch.object(native.tc, "atomic_bytes", side_effect=fail),
            self.assertRaisesRegex(OSError, "second write"),
        ):
            with native.cargo_resolution_pins(self.root, pins):
                self.fail("must not yield with partial pins")
        self.assertEqual(
            before, {n: native.cargo_file_state(self.root, n) for n in before}
        )


class CargoRepairGraphTests(unittest.TestCase):
    SOURCE = "registry+https://github.com/rust-lang/crates.io-index"

    def lock(self, rows):
        body = "version=4\n"
        for name, version, source, dependencies in rows:
            body += f"[[package]]\nname={name!r}\nversion={version!r}\n"
            if source:
                body += f"source={source!r}\n"
            body += f"dependencies={dependencies!r}\n"
        return body.encode()

    def issue(self, name, version="1.0.0", workspace="one"):
        return (
            workspace,
            ("crates", name, version, "", "sha256:" + "1" * 64),
            ["0.9.0"],
        )

    def test_indirect_dependency_order_through_eligible_local_package(self):
        issues = [self.issue("a-child"), self.issue("z-parent")]
        lock = self.lock(
            [
                ("a-child", "1.0.0", self.SOURCE, []),
                ("middle", "0.1.0", "", ["a-child"]),
                ("z-parent", "1.0.0", self.SOURCE, ["middle"]),
            ]
        )
        self.assertEqual(native.cargo_repair_order(issues, {"one": lock}), issues[::-1])

    def test_version_and_source_qualified_edges_do_not_conflate_packages(self):
        issues = [
            self.issue("a-child", "2.0.0"),
            self.issue("z-parent"),
            self.issue("a-child"),
        ]
        lock = self.lock(
            [
                ("a-child", "1.0.0", self.SOURCE, []),
                ("a-child", "1.0.0", "", []),
                ("a-child", "2.0.0", self.SOURCE, []),
                ("z-parent", "1.0.0", self.SOURCE, [f"a-child 1.0.0 ({self.SOURCE})"]),
            ]
        )
        self.assertEqual(native.cargo_repair_order(issues, {"one": lock}), issues)

    def test_omitted_source_prefers_local_after_resolving_unique_version(self):
        issues = [self.issue("a-child"), self.issue("z-parent")]
        for dependency in ["bridge", "bridge 1.0.0"]:
            with self.subTest(dependency=dependency):
                lock = self.lock(
                    [
                        ("a-child", "1.0.0", self.SOURCE, []),
                        ("bridge", "1.0.0", "", ["a-child"]),
                        ("bridge", "1.0.0", self.SOURCE, []),
                        ("z-parent", "1.0.0", self.SOURCE, [dependency]),
                    ]
                )
                self.assertEqual(
                    native.cargo_repair_order(issues, {"one": lock}), issues[::-1]
                )
        lock = self.lock(
            [
                ("a-child", "1.0.0", self.SOURCE, []),
                ("bridge", "1.0.0", "", ["a-child"]),
                ("bridge", "2.0.0", self.SOURCE, []),
                ("z-parent", "1.0.0", self.SOURCE, ["bridge"]),
            ]
        )
        with self.assertRaisesRegex(
            ValueError, "ambiguous Cargo lock dependency version"
        ):
            native.cargo_repair_order(issues, {"one": lock})

    def test_version_only_edge_is_supported(self):
        issues = [self.issue("child"), self.issue("parent")]
        lock = self.lock(
            [
                ("child", "1.0.0", self.SOURCE, []),
                ("child", "2.0.0", self.SOURCE, []),
                ("parent", "1.0.0", self.SOURCE, ["child 1.0.0"]),
            ]
        )
        self.assertEqual(native.cargo_repair_order(issues, {"one": lock}), issues[::-1])

    def test_cycles_have_stable_order_and_precede_downstream_nodes(self):
        issues = [self.issue("child"), self.issue("a"), self.issue("b")]
        lock = self.lock(
            [
                ("child", "1.0.0", self.SOURCE, []),
                ("a", "1.0.0", self.SOURCE, ["b"]),
                ("b", "1.0.0", self.SOURCE, ["a", "child"]),
            ]
        )
        self.assertEqual(
            native.cargo_repair_order(issues, {"one": lock}),
            [issues[1], issues[2], issues[0]],
        )

    def test_workspace_graphs_do_not_leak_dependency_order(self):
        issues = [self.issue("child"), self.issue("parent", workspace="two")]
        lock = self.lock(
            [
                ("child", "1.0.0", self.SOURCE, []),
                ("parent", "1.0.0", self.SOURCE, ["child"]),
            ]
        )
        self.assertEqual(
            native.cargo_repair_order(issues, {"one": lock, "two": lock}), issues
        )

    def test_ambiguous_missing_and_invalid_dependencies_fail_closed(self):
        issues = [self.issue("parent")]
        for dependency in [
            "child",
            "absent",
            "child 1.0.0 trailing garbage",
            f"child ({self.SOURCE})",
            42,
        ]:
            with self.subTest(dependency=dependency):
                lock = self.lock(
                    [
                        ("child", "1.0.0", self.SOURCE, []),
                        ("child", "2.0.0", self.SOURCE, []),
                        ("parent", "1.0.0", self.SOURCE, [dependency]),
                    ]
                )
                with self.assertRaisesRegex(ValueError, "Cargo lock dependency"):
                    native.cargo_repair_order(issues, {"one": lock})

    def test_duplicate_identity_and_absent_repair_identity_fail_closed(self):
        row = ("parent", "1.0.0", self.SOURCE, [])
        for rows in [[row, row], []]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                native.cargo_repair_order(
                    [self.issue("parent")], {"one": self.lock(rows)}
                )

    def test_peer_cohort_uses_exact_shared_children_and_registry(self):
        other_source = "registry+https://example.invalid/index"
        lock = self.lock(
            [
                ("target", "2.0.0", self.SOURCE, ["child 3.0.0", "second"]),
                ("a-peer", "1.0.0", self.SOURCE, ["child 3.0.0", "second"]),
                ("a-peer", "2.0.0", self.SOURCE, ["child 2.0.0"]),
                ("a-peer", "1.0.0", "", ["child 3.0.0"]),
                ("a-peer", "1.0.0", other_source, ["child 3.0.0"]),
                ("z-peer", "1.0.0", self.SOURCE, ["second"]),
                ("unrelated", "1.0.0", self.SOURCE, []),
                ("child", "2.0.0", self.SOURCE, []),
                ("child", "3.0.0", self.SOURCE, []),
                ("second", "1.0.0", self.SOURCE, []),
            ]
        )
        self.assertEqual(
            native.cargo_repair_peers(self.issue("target", "2.0.0")[1], lock),
            [
                ("a-peer", "1.0.0", self.SOURCE),
                ("z-peer", "1.0.0", self.SOURCE),
            ],
        )

    def test_peer_cohort_excludes_indirect_and_other_workspace_edges(self):
        one = self.lock(
            [
                ("target", "1.0.0", self.SOURCE, ["middle"]),
                ("middle", "1.0.0", self.SOURCE, ["child"]),
                ("peer", "1.0.0", self.SOURCE, ["child"]),
                ("child", "1.0.0", self.SOURCE, []),
            ]
        )
        two = self.lock(
            [
                ("target", "1.0.0", self.SOURCE, ["child"]),
                ("peer", "1.0.0", self.SOURCE, ["child"]),
                ("child", "1.0.0", self.SOURCE, []),
            ]
        )
        identity = self.issue("target")[1]
        self.assertEqual(native.cargo_repair_peers(identity, one), [])
        self.assertEqual(
            native.cargo_repair_peers(identity, two), [("peer", "1.0.0", self.SOURCE)]
        )

    def test_peer_cohort_rejects_absent_and_malformed_graph(self):
        for lock in [
            self.lock([]),
            self.lock([("target", "1.0.0", self.SOURCE, ["missing"])]),
        ]:
            with self.subTest(lock=lock), self.assertRaises(ValueError):
                native.cargo_repair_peers(self.issue("target")[1], lock)


if __name__ == "__main__":
    unittest.main()
