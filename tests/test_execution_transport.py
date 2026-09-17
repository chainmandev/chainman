"""Project-neutral contracts for scoped container transport and X11 projection."""

import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "scripts"))
import bootstrap_plan
import config_inspection
import display_transport as display
import execution_transport as transport
import project_environment
import reentry


def record(
    family, address, screen, cookie=b"0123456789abcdef", protocol=b"MIT-MAGIC-COOKIE-1"
):
    result = struct.pack("!H", family)
    for field in (address, screen, protocol, cookie):
        result += struct.pack("!H", len(field)) + field
    return result


class TransportTests(unittest.TestCase):
    def test_scope_composition_deduplication_and_nested_mounts(self):
        parent = {"source_env": "FIXTURE_CREDENTIALS", "target": "/credentials"}
        child = {
            "source_env": "FIXTURE_STATE",
            "target": "/credentials/state",
            "read_only": False,
        }
        cfg = {"profiles": {"private": {"transport": {"mounts": [parent]}}}}
        scoped = transport.effective(
            cfg, {"profile": "private", "transport": {"mounts": [parent, child]}}
        )
        self.assertEqual(len(scoped["mounts"]), 2)
        self.assertEqual(transport.effective(cfg, {}), {"mounts": [], "ports": []})
        for conflicting in (
            {"mounts": [{**parent, "read_only": False}]},
            {"ports": ["127.0.0.1:80:82"]},
        ):
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                transport.compose(
                    [
                        ("base", {"mounts": [parent], "ports": ["127.0.0.1:80:81"]}),
                        ("other", conflicting),
                    ]
                )

    def test_equivalence_preserves_only_meaningful_transport_differences(self):
        first = {"source": "/source-a", "target": "/a"}
        second = {"source": "/source-b", "target": "/b"}
        left = {"mounts": [first, second], "ports": ["127.0.0.1:08080:00080"]}
        right = {"mounts": [second, first], "ports": ["127.0.0.1:8080:80/tcp"]}
        self.assertTrue(transport.equivalent(left, right))
        self.assertEqual(
            transport.compose([("one", left), ("two", right)])["ports"],
            ["127.0.0.1:8080:80/tcp"],
        )
        for changed in (
            {**first, "source": "/elsewhere"},
            {**first, "read_only": False},
            {**first, "optional": True},
        ):
            self.assertFalse(
                transport.equivalent(left, {**left, "mounts": [changed, second]})
            )
        self.assertFalse(
            transport.equivalent(left, {**left, "ports": ["127.0.0.1:8080:80/udp"]})
        )
        child = {**second, "target": "/a/child"}
        self.assertFalse(
            transport.equivalent({"mounts": [first, child]}, {"mounts": [child, first]})
        )
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            transport.compose(
                [
                    (
                        "conflict",
                        {"ports": ["127.0.0.1:08080:80", "127.0.0.1:8080:81/tcp"]},
                    )
                ]
            )

    def test_nested_entry_accepts_equivalent_transport(self):
        first = {"source": "/one", "target": "/one"}
        second = {"source": "/two", "target": "/two"}
        selected = {"mounts": [first, second], "ports": ["127.0.0.1:08080:80"]}
        active = {"mounts": [second, first], "ports": ["127.0.0.1:8080:80/tcp"]}
        with (
            patch.object(reentry, "validate"),
            patch.object(
                reentry.workflows,
                "configuration",
                return_value={"profiles": {"private": {"transport": selected}}},
            ),
            patch.object(reentry.chainman, "main", return_value=0),
            patch.dict(
                os.environ,
                CHAINMAN_ACTIVE_MODE="container-nix",
                CHAINMAN_ACTIVE_TRANSPORT=json.dumps(active),
            ),
        ):
            self.assertEqual(
                reentry.main(
                    ["/fixture", "--entry", "exec", "--profile", "private", "true"]
                ),
                0,
            )

    def test_profile_entry_and_task_projection_exclude_installers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "flake.nix").write_text("{}")
            (root / "flake.lock").write_text("{}")
            (root / "chainman.toml").write_text("""schema=3
[project]
default_profile="host"
[profiles.private]
flake="flake.nix#private"
transport={mounts=[{source_env="FIXTURE_KEY",target="/key",optional=true}]}
[tasks.work]
profile="private"
commands=[["true"]]
""")
            with patch.dict(os.environ, CHAINMAN_REQUEST_PROFILE="private"):
                for action, name in (
                    ("exec", "--profile"),
                    ("shell", "--profile"),
                    ("run", "work"),
                    ("_workflow-task", "work"),
                ):
                    _, options = bootstrap_plan.plan(root, action, name)
                    self.assertIn("--transport-readiness", options)
                    self.assertIn("--mount-env-optional", options)
                    self.assertIn("FIXTURE_KEY:/key:ro", options)
                for action, name in (
                    ("setup", ""),
                    ("_transport-prepare", ""),
                    ("_service-prepare", "work"),
                    ("exec", "--reuse-operation"),
                ):
                    self.assertNotIn(
                        "--mount-env-optional",
                        bootstrap_plan.plan(root, action, name)[1],
                    )
                before = set(root.iterdir())
                doc = config_inspection.document(
                    root, "explain", ["--profile", "private", "--json"]
                )
                self.assertEqual(
                    doc["transport"]["layers"][0]["mounts"][0]["status"], "unresolved"
                )
                self.assertEqual(before, set(root.iterdir()))
                self.assertIn("profiles.private.transport", json.dumps(doc))

    def test_graph_display_preflight_and_conflicting_dependency_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = 'schema=3\n[project]\ndefault_profile="host"\n[tasks.work]\ncommands=[["true"]]\ntransport={display="x11"}\n'
            (root / "chainman.toml").write_text(config)
            self.assertIn(
                "--display-check", bootstrap_plan.plan(root, "preflight", "work")[1]
            )
            (root / "chainman.toml").write_text(
                config + '[tasks.first]\ncommands=[["true"]]\n'
            )
            with patch.dict(os.environ, CHAINMAN_PREFLIGHT_TASKS="first\nwork"):
                self.assertIn(
                    "--display-check",
                    bootstrap_plan.plan(root, "preflight", "first")[1],
                )
            (root / "chainman.toml").write_text(
                config.replace(
                    'commands=[["true"]]', 'depends_on=["other"]\ncommands=[["true"]]'
                )
                + '[tasks.other]\ncommands=[["true"]]\n'
            )
            with self.assertRaisesRegex(ValueError, "identical transport"):
                bootstrap_plan.plan(root, "run", "work")

    def test_nested_entry_cannot_change_mount_scope(self):
        cfg = {
            "profiles": {
                "private": {
                    "transport": {
                        "mounts": [{"source_env": "FIXTURE_KEY", "target": "/key"}]
                    }
                }
            }
        }
        with (
            patch.object(reentry, "validate"),
            patch.object(reentry.workflows, "configuration", return_value=cfg),
            patch.object(reentry.chainman, "main", return_value=0),
            patch.dict(
                os.environ,
                CHAINMAN_ACTIVE_MODE="container-nix",
                CHAINMAN_ACTIVE_TRANSPORT='{"mounts": [], "ports": []}',
            ),
        ):
            with self.assertRaisesRegex(ValueError, "different container transport"):
                reentry.main(
                    ["/fixture", "--entry", "exec", "--profile", "private", "true"]
                )
            self.assertEqual(reentry.main(["/fixture", "--entry", "exec", "true"]), 0)

    def test_inspection_does_not_open_credential_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "credential"
            secret.write_text("do-not-read-or-print")
            cfg = {
                "profiles": {
                    "private": {
                        "transport": {
                            "mounts": [{"source_env": "FIXTURE_KEY", "target": "/key"}]
                        }
                    }
                }
            }
            with (
                patch.dict(os.environ, FIXTURE_KEY=str(secret)),
                patch.object(
                    Path, "read_bytes", side_effect=AssertionError("credential read")
                ),
            ):
                doc = transport.inspect(root, cfg, {}, profile="private")
                self.assertEqual(doc["layers"][0]["mounts"][0]["status"], "present")
                self.assertNotIn("do-not-read", json.dumps(doc))

    def test_optional_is_explicit_and_display_kind_is_bounded(self):
        for value in (
            {"display": "wayland"},
            {"mounts": [{"source": "/a", "target": "/b", "optional": "yes"}]},
        ):
            with self.assertRaises(ValueError):
                project_environment.transport(value)

    def test_ports_validate_literal_and_interpolated_fields_before_execution(self):
        invalid = [
            "127.0.0.1:99999:80",
            "127.0.0.1:not-a-port:80",
            "127.0.0.1:80:0",
            "127.0.0.1:80:65536",
            "127.0.0.1:{env:PORT}:99999",
            "127.0.0.1:0:{env:PORT}",
            "127.0.0.1:80:80/sctp",
            "0.0.0.0:80:80",
            "127.0.0.1:80-90:80",
            "127.0.0.1:{PORT}:80",
            "127.0.0.1:80:80\n",
        ]
        for port in invalid:
            with (
                self.subTest(port=port),
                self.assertRaisesRegex(ValueError, "Transport"),
            ):
                bootstrap_plan.transport(
                    Path("/fixture"), {"ports": [port]}, {"PORT": "80"}
                )
        for value in ("", "99999", "0", "-1", "80;false"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "PORT"):
                bootstrap_plan.transport(
                    Path("/fixture"),
                    {"ports": ["127.0.0.1:{env:PORT}:80"]},
                    {"PORT": value},
                )
        self.assertEqual(
            bootstrap_plan.transport(
                Path("/fixture"),
                {"ports": ["127.0.0.1:00080:{env:PORT}/udp"]},
                {"PORT": "65535"},
            ),
            ["--publish", "127.0.0.1:80:65535/udp"],
        )

    def test_container_inspection_never_reports_host_presence(self):
        cfg = {
            "container": {
                "mounts": [
                    {"source": "/missing", "target": "/required"},
                    {
                        "source_env": "FIXTURE_ABSENT",
                        "target": "/optional",
                        "optional": True,
                    },
                ]
            }
        }
        with (
            patch.dict(os.environ, CHAINMAN_ACTIVE_MODE="container-nix"),
            patch.object(
                Path, "exists", side_effect=AssertionError("host lookup in container")
            ),
        ):
            doc = transport.inspect(Path("/fixture"), cfg, {})
        self.assertEqual(doc["mount_status_context"], "host filesystem unavailable")
        self.assertEqual(
            [m["status"] for m in doc["layers"][0]["mounts"]],
            ["not checked on host"] * 2,
        )

    def test_private_authority_is_scoped_and_original_unchanged(self):
        original = (
            record(256, b"fixture-desktop", b"9")
            + record(256, b"other-host", b"9", b"xxxxxxxxxxxxxxxx")
            + record(256, b"fixture-desktop", b"10")
        )
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "original", Path(directory) / "scoped"
            source.write_bytes(original)
            display.prepare(source, output, "unix/:9.0", "fixture-desktop")
            self.assertEqual(output.read_bytes(), record(65535, b"", b"9"))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(source.read_bytes(), original)
            with self.assertRaises(FileExistsError):
                display.prepare(source, output, ":9", "fixture-desktop")
            output.unlink()
            output.symlink_to(source)
            with self.assertRaises(FileExistsError):
                display.prepare(source, output, ":9", "fixture-desktop")
            self.assertEqual(source.read_bytes(), original)

    def test_malformed_missing_foreign_and_oversized_authority_fail(self):
        valid = record(256, b"fixture-desktop", b"9")
        for data in (
            b"",
            valid[:-1],
            valid + b"x",
            b"x" * (display.LIMIT + 1),
            record(256, b"elsewhere", b"9"),
            record(256, b"fixture-desktop", b"10"),
        ):
            with self.assertRaises(ValueError):
                display.authentication(data, ":9", "fixture-desktop")
        for selector in ("host:9", "tcp/host:9", ":9;anything", ":"):
            with self.assertRaises(ValueError):
                display.authentication(valid, selector, "fixture-desktop")

    def test_supervisor_preserves_arguments_input_status_and_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "private files"
            temporary.mkdir()
            program = "import sys; print(repr(sys.argv[1:])); print(sys.stdin.read()); sys.exit(23)"
            result = subprocess.run(
                [
                    "sh",
                    str(SOURCE / "bootstrap/transport-run.sh"),
                    str(temporary),
                    sys.executable,
                    "-c",
                    program,
                    "two words",
                    "$(false)",
                    "",
                ],
                input="payload\n",
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 23)
            self.assertIn("['two words', '$(false)', '']", result.stdout)
            self.assertIn("payload", result.stdout)
            self.assertFalse(temporary.exists())

    def test_supervisor_forwards_termination_and_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / "private"
            temporary.mkdir()
            ready, stopped = Path(directory) / "ready", Path(directory) / "stopped"
            code = "import pathlib,signal,time,sys; signal.signal(signal.SIGTERM,lambda *a:(pathlib.Path(sys.argv[2]).touch(),sys.exit(0))); pathlib.Path(sys.argv[1]).touch(); time.sleep(20)"
            child = subprocess.Popen(
                [
                    "sh",
                    str(SOURCE / "bootstrap/transport-run.sh"),
                    str(temporary),
                    sys.executable,
                    "-c",
                    code,
                    str(ready),
                    str(stopped),
                ]
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                child.send_signal(signal.SIGTERM)
                self.assertEqual(child.wait(timeout=5), 143)
                self.assertTrue(stopped.exists())
                self.assertFalse(temporary.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_supervisor_bounds_uncooperative_clients_for_terminal_signals(self):
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                private = root / "private"
                private.mkdir()
                ready = root / "ready"
                received = root / "received"
                code = "import os,pathlib,signal,sys,time; [signal.signal(s,lambda n,f:pathlib.Path(sys.argv[2]).write_text(str(n))) for s in (signal.SIGHUP,signal.SIGINT,signal.SIGTERM)]; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
                process = subprocess.Popen(
                    [
                        "sh",
                        str(SOURCE / "bootstrap/transport-run.sh"),
                        str(private),
                        sys.executable,
                        "-c",
                        code,
                        str(ready),
                        str(received),
                    ],
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    process.send_signal(sig)
                    _, error = process.communicate(timeout=6)
                    self.assertEqual(process.returncode, 128 + sig)
                    self.assertEqual(received.read_text(), str(int(sig)))
                    self.assertIn("forcing client exit", error)
                    self.assertFalse(private.exists())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(ready.read_text()), 0)
                finally:
                    if ready.exists():
                        try:
                            os.kill(int(ready.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.kill()
                    process.communicate()

    def test_bootstrap_cancellation_stops_before_later_work(self):
        for mode, executable in (("host-nix", "nix"), ("container-nix", "docker")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                ready = root / "ready"
                calls = root / "calls"
                binary = root / executable
                binary.write_text(f"""#!{sys.executable}
import os, signal, time
from pathlib import Path
with Path({str(calls)!r}).open('a') as stream: stream.write('called\\n')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(ready)!r}).write_text(str(os.getpid()))
time.sleep(30)
""")
                binary.chmod(0o755)
                (root / "chainman.lock").write_text("a" * 40 + "\n")
                env = dict(
                    os.environ,
                    CHAINMAN_MODE=mode,
                    CHAINMAN_PROJECT_ROOT=str(root),
                    CHAINMAN_SOURCE_REVISION="a" * 40,
                    CHAINMAN_CONTAINER_ENGINE="docker",
                    CHAINMAN_NIX_BIN=str(binary),
                    PATH=str(root) + os.pathsep + os.environ["PATH"],
                    TMPDIR=str(root),
                )
                for key in (
                    "CHAINMAN_ACTIVE_PROFILE",
                    "CHAINMAN_ENTRY_AUTHORITY",
                    "CHAINMAN_BOOTSTRAP_CONTAINER",
                ):
                    env.pop(key, None)
                child = subprocess.Popen(
                    [
                        "sh",
                        str(SOURCE / "bootstrap/chainman.sh"),
                        "preflight",
                        "fixture",
                    ],
                    env=env,
                    stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    child.send_signal(signal.SIGTERM)
                    _, error = child.communicate(timeout=6)
                    self.assertEqual(child.returncode, 143, error)
                    self.assertEqual(calls.read_text().splitlines(), ["called"])
                    self.assertEqual(list(root.glob("chainman-*-bootstrap.*")), [])
                    self.assertEqual(list(root.glob("chainman-bootstrap.*")), [])
                    with self.assertRaises(ProcessLookupError):
                        os.kill(int(ready.read_text()), 0)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate()
                    if ready.exists():
                        try:
                            os.kill(int(ready.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_helper_failure_does_not_continue_to_dispatch(self):
        result = subprocess.run(
            [
                "sh",
                "-eu",
                "-c",
                '. "$1"; lifetime_helper sh -c "exit 23"; echo unexpected',
                "fixture",
                str(SOURCE / "bootstrap/lifetime.sh"),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 23)
        self.assertNotIn("unexpected", result.stdout)

    def test_nested_helper_interrupt_allows_inner_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            ready = root / "ready"
            program = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            process = subprocess.Popen(
                [
                    "sh",
                    "-eu",
                    "-c",
                    '. "$1"; shift; lifetime_helper "$@"; echo unexpected',
                    "fixture",
                    str(SOURCE / "bootstrap/lifetime.sh"),
                    "sh",
                    str(SOURCE / "bootstrap/transport-run.sh"),
                    str(private),
                    sys.executable,
                    "-c",
                    program,
                    str(ready),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                process.send_signal(signal.SIGINT)
                output, error = process.communicate(timeout=7)
                self.assertEqual(process.returncode, 130, error)
                self.assertNotIn(b"unexpected", output)
                self.assertFalse(private.exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
                if ready.exists():
                    try:
                        os.kill(int(ready.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
