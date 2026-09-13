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
import dependency_api as api
import registry

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


class CargoResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Cargo eligibility space ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
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

    def test_invalid_budget_rejects_before_planning_or_native_work(self):
        original = self.manifest.read_bytes()
        for value in [0, -1, 513, True, False, 1.0, "64", None, [], {}]:
            with (
                self.subTest(value=value),
                patch.object(native, "specifications") as planning,
                patch.object(registry, "releases") as releases,
                patch.object(native.chainman, "execute") as execute,
                self.assertRaisesRegex(ValueError, "cargo_max_attempts"),
            ):
                native.resolve(
                    self.root, {**self.spec, "cargo_max_attempts": value}, {}, NOW
                )
            planning.assert_not_called()
            releases.assert_not_called()
            execute.assert_not_called()
            self.assertEqual(self.manifest.read_bytes(), original)
            self.assertEqual(
                native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
            )

    def test_budget_rejects_inapplicable_adapters_and_custom_commands_early(self):
        specs = [
            {"adapter": kind} for kind in ["python", "flutter", "go", "swift", "gradle"]
        ]
        specs += [
            {**self.spec, "resolve": command}
            for command in [
                [],
                [["cargo", "update", "--workspace"]],
                [["wrapper", "cargo", "update"]],
                [["cargo", "update"], ["cargo", "update"]],
            ]
        ]
        for spec in specs:
            with (
                self.subTest(spec=spec),
                patch.object(native, "specifications") as planning,
                patch.object(registry, "releases") as releases,
                patch.object(native.chainman, "execute") as execute,
                self.assertRaisesRegex(ValueError, "cargo_max_attempts"),
            ):
                native.resolve(self.root, {**spec, "cargo_max_attempts": 256}, {}, NOW)
            planning.assert_not_called()
            releases.assert_not_called()
            execute.assert_not_called()

    def test_budget_endpoints_and_explicit_cargo_path_are_supported(self):
        self.releases["leaf"] = [
            r for r in self.releases["leaf"] if r.version != "2.0.0"
        ]
        for limit in [1, 512]:
            for command in [None, ["cargo", "update"], ["/tools/cargo", "update"]]:
                with self.subTest(limit=limit, command=command):
                    spec = {**self.spec, "cargo_max_attempts": limit}
                    if command is not None:
                        spec["resolve"] = [command]
                    actual = []

                    def execute(root, profile, argv, **kwargs):
                        actual.append(argv)
                        return self.resolver(
                            root, profile, ["cargo", *argv[1:]], **kwargs
                        )

                    self.resolve(spec=spec, resolver=execute)
                    self.assertEqual(len(actual), 2)
                    self.assertTrue(
                        all(a[0] == (command or ["cargo"])[0] for a in actual)
                    )

    def test_public_adapter_configuration_rejects_budget_before_dispatch(self):
        specs = [
            {"adapter": kind, "cargo_max_attempts": 256}
            for kind in [
                "javascript",
                "actions",
                "oci",
                "nix",
                "go",
                "toolchain",
                "artifact",
                "python",
                "flutter",
                "swift",
                "gradle",
            ]
        ]
        specs += [
            {**self.spec, "cargo_max_attempts": value} for value in [0, True, None, 513]
        ]
        specs.append(
            {
                **self.spec,
                "cargo_max_attempts": 256,
                "resolve": [["cargo", "update", "--workspace"]],
            }
        )
        for spec in specs:
            with (
                self.subTest(spec=spec),
                patch.object(api, "implementation") as dispatch,
            ):
                with self.assertRaisesRegex(ValueError, "cargo_max_attempts"):
                    api.configured(self.root, "deps", {"adapters": {"deps": spec}})
                dispatch.assert_not_called()
        expected = {**self.spec, "cargo_max_attempts": 256}
        self.assertEqual(
            api.configured(self.root, "deps", {"adapters": {"deps": expected}}),
            expected,
        )

    def test_explicit_budget_exhaustion_preserves_missing_and_existing_locks(self):
        for absent in [False, True]:
            with self.subTest(absent=absent):
                if absent:
                    (self.root / "Cargo.lock").unlink()
                self.calls.clear()
                with self.assertRaisesRegex(ValueError, "1-state bound"):
                    self.resolve(spec={**self.spec, "cargo_max_attempts": 1})
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(
                    native.cargo_file_state(self.root, "Cargo.lock"),
                    None if absent else self.original_lock,
                )
                self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o640)
                self.assertIn('version="1.2.0"', self.manifest.read_text())

    def test_configured_budget_counts_individual_and_coordinated_trials(self):
        for limit in [1, 2]:
            with self.subTest(limit=limit):
                resolver = self.coordinated_resolver()
                spec = {**self.spec, "cargo_max_attempts": limit}
                if limit == 1:
                    with self.assertRaisesRegex(ValueError, "1-state bound"):
                        self.resolve(spec=spec, resolver=resolver)
                else:
                    self.resolve(spec=spec, resolver=resolver)
                self.assertEqual(len(self.coordinated_calls), limit)

    def test_configured_budget_is_shared_across_workspaces(self):
        second = self.put("second/Cargo.toml", self.manifest.read_text())
        self.put("second/src/lib.rs", "// independent workspace\n")
        self.write_lock(second.parent, {"parent": "1.0.0", "leaf": "1.0.0"})
        initial = {
            name: native.cargo_file_state(self.root, name)
            for name in ["Cargo.lock", "second/Cargo.lock"]
        }
        for limit in [3, 4]:
            with self.subTest(limit=limit):
                self.calls.clear()
                spec = {
                    **self.spec,
                    "directories": [".", "second"],
                    "cargo_max_attempts": limit,
                }
                if limit == 3:
                    with self.assertRaisesRegex(ValueError, "3-state bound"):
                        self.resolve(spec=spec)
                    self.assertEqual(
                        {
                            name: native.cargo_file_state(self.root, name)
                            for name in initial
                        },
                        initial,
                    )
                else:
                    self.resolve(spec=spec)
                self.assertEqual(
                    len([a for _, a, _ in self.calls if len(a) > 2]), limit
                )

    def test_graph_needing_65_repairs_requires_explicit_larger_budget(self):
        # Independent leaf updates each require one native repair. No conflicts
        # or peer heuristic can compress this fixture's 65 necessary transitions.
        names = [f"leaf{index:03}" for index in range(65)]
        for name in names:
            self.releases[name] = [
                self.release(name, "1.0.0", 90),
                self.release(name, "1.1.0", 1),
            ]
        self.write_lock(self.root, {"parent": "1.0.0", **dict.fromkeys(names, "1.0.0")})
        original = native.cargo_file_state(self.root, "Cargo.lock")
        for limit in [None, 64, 65]:
            with self.subTest(limit=limit):
                (self.root / "Cargo.lock").write_bytes(original[0])
                (self.root / "Cargo.lock").chmod(original[1])
                versions = {"parent": "1.2.0", **dict.fromkeys(names, "1.1.0")}
                repaired = []

                def execute(root, profile, argv, **kwargs):
                    if len(argv) > 2:
                        name = argv[3].split("#", 1)[1].split("@", 1)[0]
                        self.assertEqual(argv[-1], "1.0.0")
                        self.assertNotIn(name, repaired)
                        repaired.append(name)
                        versions[name] = "1.0.0"
                    self.write_lock(root, versions)
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="independent leaf repair\n"
                    )

                spec = {
                    **self.spec,
                    **({"cargo_max_attempts": limit} if limit is not None else {}),
                }
                if limit != 65:
                    with self.assertRaisesRegex(ValueError, "64-state bound"):
                        self.resolve(spec=spec, resolver=execute)
                    self.assertEqual(
                        native.cargo_file_state(self.root, "Cargo.lock"), original
                    )
                    self.assertEqual(len(repaired), 64)
                else:
                    self.resolve(spec=spec, resolver=execute)
                    self.assertEqual(set(repaired), set(names))

    def test_larger_budget_still_rejects_bad_identity_without_retry(self):
        def execute(root, profile, argv, **kwargs):
            result = self.resolver(root, profile, argv, **kwargs)
            self.write_lock(root, {"parent": "1.2.0", "leaf": "1.6.0"}, wrong="leaf")
            return result

        with self.assertRaisesRegex(ValueError, "absent from registry"):
            self.resolve(
                spec={**self.spec, "cargo_max_attempts": 256}, resolver=execute
            )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

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

    def prior_choice_resolver(self, *, move_kept=False):
        for name in ("a-chosen", "target", "peer"):
            self.releases[name] = [
                self.release(name, "1.0.0", 60),
                self.release(name, "1.1.0", 1),
            ]
        source = "registry+https://github.com/rust-lang/crates.io-index"
        self.choice_calls = []
        dependencies = {
            "parent": ["a-chosen", "target", "peer"],
            "a-chosen": ["leaf"],
            "target": ["leaf"],
            "peer": ["leaf"],
        }

        def resolver(root, profile, argv, **kwargs):
            directory = kwargs["cwd"]
            if len(argv) == 2:
                versions = {
                    "parent": "1.2.0",
                    "a-chosen": "1.1.0" if directory == self.root else "1.0.0",
                    "target": "1.1.0",
                    "peer": "1.0.0",
                    "leaf": "1.5.0",
                }
            else:
                versions = {
                    item["name"]: item["version"]
                    for item in native.tomllib.loads(
                        (directory / "Cargo.lock").read_text()
                    )["package"]
                }
                selectors = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-p"]
                self.choice_calls.append((directory, selectors))
                package = selectors[0].split("#")[1].split("@")[0]
                if package == "target" and len(selectors) == 1:
                    raise subprocess.CalledProcessError(
                        101,
                        argv,
                        output="error: failed to select a version for `leaf`\n",
                    )
                versions[package] = argv[-1]
                if package == "target":
                    self.assertIn(f"{source}#peer@1.0.0", selectors)
                    if directory == self.root and (
                        move_kept or f"{source}#a-chosen@1.0.0" in selectors
                    ):
                        versions["a-chosen"] = "1.1.0"
            self.write_lock(directory, versions, dependencies=dependencies)
            return subprocess.CompletedProcess(argv, 0, stdout="prior choice fixture\n")

        return resolver

    def test_coordinated_retry_keeps_previous_exact_choice_locked(self):
        result = self.resolve(resolver=self.prior_choice_resolver())
        selected = self.choice_calls[-1][1]
        self.assertEqual(len(self.choice_calls), 3)
        self.assertEqual(len(selected), 2)
        self.assertFalse(any("#a-chosen@" in item for item in selected))
        self.assertTrue(
            any(
                item[:3] == ["crates", "a-chosen", "1.0.0"]
                for item in result["cargo_identities"]["rust-0"]
            )
        )

    def test_previous_choice_does_not_lock_same_identity_in_other_workspace(self):
        second = self.put("second/Cargo.toml", self.manifest.read_text()).parent
        self.put("second/src/lib.rs", "// independent workspace\n")
        self.write_lock(second, {"parent": "1.0.0", "leaf": "1.0.0"})
        self.resolve(
            resolver=self.prior_choice_resolver(),
            spec={**self.spec, "directories": [".", "second"]},
        )
        cohorts = {directory: selectors for directory, selectors in self.choice_calls}
        self.assertEqual(len(cohorts[self.root]), 2)
        self.assertEqual(len(cohorts[second]), 3)
        self.assertTrue(any("#a-chosen@1.0.0" in item for item in cohorts[second]))

    def test_native_movement_of_kept_choice_still_rejects_candidate(self):
        with self.assertRaisesRegex(ValueError, "No eligible Cargo graph"):
            self.resolve(resolver=self.prior_choice_resolver(move_kept=True))
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

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

    def duplicate_resolver(
        self, transition, *, survivors=("1.0.0",), prior=False, peer=False
    ):
        # Synthetic native-output fixtures exercise the public resolution boundary.
        # Actual Cargo consolidation is qualified separately with native controls.
        self.releases["merged"] = [
            self.release("merged", version, days)
            for version, days in [
                ("1.0.0", 90),
                ("1.1.0", 1),
                ("1.5.0", 60),
                ("2.0.0", 60),
                ("2.1.0", 1),
            ]
        ]
        for name in ("a-chosen", "peer"):
            self.releases[name] = [
                self.release(name, "1.0.0", 60),
                self.release(name, "1.1.0", 1),
            ]
        source = "registry+https://github.com/rust-lang/crates.io-index"
        self.merge_calls = []

        def execute(root, profile, argv, **kwargs):
            self.assertEqual(root, self.root)
            self.assertEqual(profile, "native")
            self.assertEqual(kwargs["env"]["TOOLCHAIN_FRESH"], "1")
            directory = kwargs["cwd"]
            document = native.manifests.document(directory / "Cargo.toml")[0]
            self.assertEqual(document["dependencies"]["alias"]["version"], "=1.2.0")
            if len(argv) == 2:
                rows = [
                    {"name": "parent", "version": "1.2.0"},
                    {"name": "leaf", "version": "1.5.0"},
                    {"name": "merged", "version": "2.1.0"},
                    *({"name": "merged", "version": value} for value in survivors),
                ]
                if prior:
                    rows.append({"name": "a-chosen", "version": "1.1.0"})
                if peer:
                    rows.append({"name": "peer", "version": "1.0.0"})
            else:
                self.assertEqual(argv[:2], ["cargo", "update"])
                self.assertEqual(argv[-2], "--precise")
                self.assertTrue(argv[3].startswith(source + "#"))
                self.merge_calls.append(list(argv))
                rows = native.tomllib.loads((directory / "Cargo.lock").read_text())[
                    "package"
                ]
                rows = transition(argv, rows)
            # Recompute edges after the modeled native change. Version-qualified
            # references keep duplicate package identities distinct.
            identities = {(row["name"], row["version"]) for row in rows}
            body = "version=4\n"
            for row in rows:
                name, version = row["name"], row["version"]
                release = next(r for r in self.releases[name] if r.version == version)
                digest = row.get("checksum", release.artifacts[0].digest.split(":")[1])
                origin = row.get("source", source)
                dependencies = []
                if name == "parent":
                    dependencies = [
                        f"{other['name']} {other['version']}"
                        for other in rows
                        if other["name"] != "parent"
                    ]
                elif name in {"a-chosen", "peer"} or (name, version) in {
                    ("merged", "2.1.0"),
                    ("merged", "1.1.0"),
                }:
                    dependencies = ["leaf 1.5.0"]
                    if (name, version) == ("merged", "2.1.0") and (
                        "merged",
                        "1.1.0",
                    ) in identities:
                        dependencies.append("merged 1.1.0")
                body += (
                    f'[[package]]\nname="{name}"\nversion="{version}"\n'
                    f'source="{origin}"\nchecksum="{digest}"\n'
                    f"dependencies={dependencies!r}\n"
                )
            (directory / "Cargo.lock").write_text(body)
            return subprocess.CompletedProcess(argv, 0, stdout="duplicate fixture\n")

        return execute

    def collapse_duplicate(self, argv, rows):
        name, old = argv[3].split("#", 1)[1].rsplit("@", 1)
        if (name, old) == ("merged", "2.1.0"):
            return [row for row in rows if (row["name"], row["version"]) != (name, old)]
        return [
            {"name": name, "version": argv[-1]}
            if (row["name"], row["version"]) == (name, old)
            else row
            for row in rows
        ]

    def test_duplicate_merge_retains_only_preexisting_exact_subset(self):
        for survivors, retained in [
            (("1.0.0",), ("1.0.0",)),
            (("1.0.0", "1.5.0"), ("1.0.0",)),
            (("1.0.0", "1.5.0"), ("1.0.0", "1.5.0")),
        ]:
            with self.subTest(survivors=survivors, retained=retained):

                def collapse(argv, rows):
                    self.assertEqual(argv[-1], "2.0.0")
                    return [
                        row
                        for row in self.collapse_duplicate(argv, rows)
                        if row["name"] != "merged" or row["version"] in retained
                    ]

                result = self.resolve(
                    resolver=self.duplicate_resolver(collapse, survivors=survivors)
                )
                expected = [
                    [
                        "crates",
                        "merged",
                        release.version,
                        "",  # Cargo.lock identifies the canonical registry, not a URL.
                        release.artifacts[0].digest,
                    ]
                    for release in self.releases["merged"]
                    if release.version in retained
                ]
                self.assertEqual(
                    [
                        row
                        for row in result["cargo_identities"]["rust-0"]
                        if row[1] == "merged"
                    ],
                    expected,
                )
                self.assertEqual(len(self.merge_calls), 1)
                self.assertEqual(
                    stat.S_IMODE((self.root / "Cargo.lock").stat().st_mode), 0o640
                )
                self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o640)
                self.assertIn('version="1.2.0"', self.manifest.read_text())
                self.assertNotIn('version="=1.2.0"', self.manifest.read_text())

    def test_duplicate_graph_still_accepts_requested_exact_version(self):
        def exact(argv, rows):
            return [
                {"name": "merged", "version": argv[-1]}
                if (row["name"], row["version"]) == ("merged", "2.1.0")
                else row
                for row in rows
            ]

        result = self.resolve(resolver=self.duplicate_resolver(exact))
        self.assertEqual(
            {
                row[2]
                for row in result["cargo_identities"]["rust-0"]
                if row[1] == "merged"
            },
            {"1.0.0", "2.0.0"},
        )
        self.assertEqual(len(self.merge_calls), 1)

    def test_duplicate_merge_rejects_wrong_identity_absence_and_old_target(self):
        for defect in [
            "new-version",
            "checksum",
            "registry-url",
            "git-source",
            "old-target",
            "absent",
            "no-op",
        ]:
            with self.subTest(defect=defect):

                def corrupt(argv, rows):
                    if defect == "no-op":
                        return rows
                    merged = self.collapse_duplicate(argv, rows)
                    if defect == "absent":
                        return [row for row in merged if row["name"] != "merged"]
                    if defect == "old-target":
                        return [
                            row
                            for row in rows
                            if (row["name"], row["version"]) != ("merged", "1.0.0")
                        ]
                    for row in merged:
                        if row["name"] != "merged":
                            continue
                        if defect == "new-version":
                            row = {"name": "merged", "version": "1.5.0"}
                        else:
                            row = dict(row)
                            row.update(
                                {
                                    "checksum": {"checksum": "0" * 64},
                                    "registry-url": {
                                        "source": "registry+https://example.invalid/index"
                                    },
                                    "git-source": {
                                        "source": "git+https://example.invalid/merged#"
                                        + "1" * 40
                                    },
                                }[defect]
                            )
                        return [
                            other if other["name"] != "merged" else row
                            for other in merged
                        ]
                    self.fail("missing survivor in negative fixture")

                with self.assertRaises(ValueError):
                    self.resolve(resolver=self.duplicate_resolver(corrupt))
                self.assertEqual(len(self.merge_calls), 1)
                self.assertEqual(
                    native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
                )

    def test_duplicate_merge_cannot_borrow_survivor_from_another_workspace(self):
        second = self.put("second/Cargo.toml", self.manifest.read_text()).parent
        self.put("second/src/lib.rs", "// independent workspace\n")
        second_initial = []

        def borrow(argv, rows):
            self.assertEqual(len(second_initial), 1)
            self.assertEqual(
                native.cargo_file_state(self.root, "second/Cargo.lock"),
                second_initial[0],
            )
            other = native.tomllib.loads((second / "Cargo.lock").read_text())["package"]
            self.assertEqual(
                [(row["name"], row["version"]) for row in other],
                [("parent", "1.2.0"), ("merged", "1.0.0")],
            )
            self.assertNotIn(
                ("merged", "1.0.0"),
                {(row["name"], row["version"]) for row in rows},
            )
            return self.collapse_duplicate(argv, rows) + [
                {"name": "merged", "version": "1.0.0"}
            ]

        resolver = self.duplicate_resolver(borrow, survivors=())
        self.write_lock(second, {"parent": "1.0.0", "merged": "1.0.0"})
        (second / "Cargo.lock").chmod(0o600)
        original_second = native.cargo_file_state(self.root, "second/Cargo.lock")

        def separate(root, profile, argv, **kwargs):
            if kwargs["cwd"] != second:
                return resolver(root, profile, argv, **kwargs)
            self.assertEqual(argv, ["cargo", "update"])
            document = native.manifests.document(second / "Cargo.toml")[0]
            self.assertEqual(document["dependencies"]["alias"]["version"], "=1.2.0")
            self.write_lock(second, {"parent": "1.2.0", "merged": "1.0.0"})
            second_initial.append(
                native.cargo_file_state(self.root, "second/Cargo.lock")
            )
            return subprocess.CompletedProcess(
                argv, 0, stdout="other workspace retained\n"
            )

        with self.assertRaisesRegex(ValueError, "requested precise registry identity"):
            self.resolve(
                resolver=separate,
                spec={**self.spec, "directories": [".", "second"]},
            )
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )
        self.assertEqual(
            native.cargo_file_state(self.root, "second/Cargo.lock"), original_second
        )
        self.assertEqual(len(self.merge_calls), 1)

    def test_duplicate_merge_repairs_young_survivor_without_a_fictitious_choice(self):
        result = self.resolve(
            resolver=self.duplicate_resolver(
                self.collapse_duplicate, survivors=("1.1.0",)
            )
        )
        self.assertEqual(
            [(argv[3].split("#")[1], argv[-1]) for argv in self.merge_calls],
            [("merged@2.1.0", "2.0.0"), ("merged@1.1.0", "2.0.0")],
        )
        self.assertEqual(
            {
                row[2]
                for row in result["cargo_identities"]["rust-0"]
                if row[1] == "merged"
            },
            {"2.0.0"},
        )

    def test_duplicate_merge_does_not_waive_unresolved_young_survivor(self):
        def conflict(argv, rows):
            if "#merged@1.1.0" in argv[3]:
                raise subprocess.CalledProcessError(
                    101, argv, output="error: failed to select a version for `merged`\n"
                )
            return self.collapse_duplicate(argv, rows)

        with self.assertRaisesRegex(ValueError, "No eligible Cargo graph"):
            self.resolve(
                resolver=self.duplicate_resolver(conflict, survivors=("1.1.0",))
            )
        self.assertGreater(len(self.merge_calls), 1)
        self.assertTrue(
            any("#merged@1.1.0" in argv[3] for argv in self.merge_calls[1:])
        )
        self.assertEqual(
            native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
        )

    def test_duplicate_merge_keeps_prior_exact_choices(self):
        result = self.resolve(
            resolver=self.duplicate_resolver(self.collapse_duplicate, prior=True)
        )
        self.assertEqual(
            [argv[3].split("#")[1] for argv in self.merge_calls],
            ["a-chosen@1.1.0", "merged@2.1.0"],
        )
        self.assertTrue(
            any(
                row[:3] == ["crates", "a-chosen", "1.0.0"]
                for row in result["cargo_identities"]["rust-0"]
            )
        )

    def test_duplicate_merge_cannot_remove_or_change_prior_exact_choice(self):
        for defect in ["remove", "version", "checksum"]:
            with self.subTest(defect=defect):

                def move(argv, rows):
                    merged = self.collapse_duplicate(argv, rows)
                    if "#merged@" not in argv[3]:
                        return merged
                    if defect == "remove":
                        return [row for row in merged if row["name"] != "a-chosen"]
                    return [
                        {"name": "a-chosen", "version": "1.1.0"}
                        if row["name"] == "a-chosen" and defect == "version"
                        else {**row, "checksum": "0" * 64}
                        if row["name"] == "a-chosen"
                        else row
                        for row in merged
                    ]

                with self.assertRaises(ValueError):
                    self.resolve(resolver=self.duplicate_resolver(move, prior=True))
                self.assertEqual(
                    native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
                )

    def test_duplicate_merge_budget_counts_both_native_attempt_forms(self):
        for absent in [False, True]:
            for limit in [1, 2]:
                with self.subTest(absent=absent, limit=limit):
                    if absent:
                        (self.root / "Cargo.lock").unlink(missing_ok=True)
                    else:
                        (self.root / "Cargo.lock").write_bytes(self.original_lock[0])
                        (self.root / "Cargo.lock").chmod(self.original_lock[1])

                    def coordinated(argv, rows):
                        if len(argv) == 6:
                            raise subprocess.CalledProcessError(
                                101,
                                argv,
                                output="error: failed to select a version for `leaf`\n",
                            )
                        # Both parent and peer directly share leaf with merged.
                        self.assertEqual(
                            argv[4:-2],
                            [
                                "-p",
                                "registry+https://github.com/rust-lang/crates.io-index#parent@1.2.0",
                                "-p",
                                "registry+https://github.com/rust-lang/crates.io-index#peer@1.0.0",
                            ],
                        )
                        return self.collapse_duplicate(argv, rows)

                    resolver = self.duplicate_resolver(coordinated, peer=True)
                    spec = {**self.spec, "cargo_max_attempts": limit}
                    if limit == 1:
                        with self.assertRaisesRegex(ValueError, "1-state bound"):
                            self.resolve(resolver=resolver, spec=spec)
                        self.assertEqual(
                            native.cargo_file_state(self.root, "Cargo.lock"),
                            None if absent else self.original_lock,
                        )
                    else:
                        self.resolve(resolver=resolver, spec=spec)
                    self.assertEqual(len(self.merge_calls), limit)
                    self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o640)

    def test_duplicate_merge_requires_native_success_and_preserves_status(self):
        for status in [41, 101, -15]:
            with self.subTest(status=status):
                resolver = self.duplicate_resolver(self.collapse_duplicate)

                def fail(root, profile, argv, **kwargs):
                    if len(argv) > 2:
                        self.merge_calls.append(list(argv))
                        raise subprocess.CalledProcessError(
                            status, argv, output="error: download failed\n"
                        )
                    return resolver(root, profile, argv, **kwargs)

                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    self.resolve(resolver=fail)
                self.assertEqual(caught.exception.returncode, status)
                self.assertEqual(len(self.merge_calls), 1)
                self.assertEqual(
                    native.cargo_file_state(self.root, "Cargo.lock"), self.original_lock
                )

    def test_failed_duplicate_merge_preserves_changed_lock_and_native_diagnostic(self):
        resolver = self.duplicate_resolver(self.collapse_duplicate)
        changed = []

        def fail_after_write(root, profile, argv, **kwargs):
            result = resolver(root, profile, argv, **kwargs)
            if len(argv) > 2:
                changed.append(native.cargo_file_state(self.root, "Cargo.lock"))
                raise subprocess.CalledProcessError(
                    41, argv, output="error: download failed after lock write\n"
                )
            return result

        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.resolve(resolver=fail_after_write)
        self.assertEqual(caught.exception.returncode, 41)
        self.assertEqual(
            caught.exception.output, "error: download failed after lock write\n"
        )
        self.assertEqual(len(self.merge_calls), 1)
        self.assertEqual(len(changed), 1)
        self.assertNotEqual(changed[0], self.original_lock)
        self.assertEqual(native.cargo_file_state(self.root, "Cargo.lock"), changed[0])
        self.assertEqual(changed[0][1], self.original_lock[1])
        self.assertTrue(
            any(
                "Failed Cargo command changed lock; preserve and inspect" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )
        self.assertTrue(
            any(
                "Cargo restoration failed; changes preserved" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_duplicate_merge_preserves_unexpected_source_and_lock_mode_drift(self):
        for defect in ["source", "lock-mode"]:
            with self.subTest(defect=defect):
                source_before = self.source.read_bytes()

                def drift(argv, rows):
                    if defect == "source":
                        self.source.write_text("// concurrent merge-time change\n")
                    else:
                        (self.root / "Cargo.lock").chmod(0o600)
                    return self.collapse_duplicate(argv, rows)

                with self.assertRaises(ValueError):
                    self.resolve(resolver=self.duplicate_resolver(drift))
                self.assertEqual(len(self.merge_calls), 1)
                if defect == "source":
                    self.assertEqual(
                        self.source.read_text(), "// concurrent merge-time change\n"
                    )
                    self.source.write_bytes(source_before)
                else:
                    self.assertEqual(
                        stat.S_IMODE((self.root / "Cargo.lock").stat().st_mode), 0o600
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
