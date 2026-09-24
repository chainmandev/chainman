"""The consumer recipe fetches exact Git objects before any runtime code executes."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import zlib


SOURCE = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/chainmandev/chainman.git"
ENTRY = """#!/bin/sh
set -eu
root=$1
shift 3
case "$1" in
    report) shift; printf '%s\\0' "$root" "$@"; cat ;;
    failure) exit 37 ;;
    wait) printf 'ready\\n'; exec sleep 60 ;;
esac
"""


class GitBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="chainman Git bootstrap ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.origin = self.root / "upstream"
        self.project = self.root / "existing project"
        self.binaries = self.root / "bin"
        self.home = self.root / "home"
        for directory in (self.origin, self.project, self.binaries, self.home):
            directory.mkdir()
        self.git = shutil.which("git")
        self.assertIsNotNone(self.git)
        self.git_run("init", "-q")
        self.git_run("config", "user.name", "Git bootstrap fixture")
        self.git_run("config", "user.email", "fixture@example.invalid")
        (self.origin / "bootstrap").mkdir()
        (self.origin / "bootstrap/git-entry.sh").write_text(ENTRY)
        self.git_run("add", ".")
        self.git_run("-c", "commit.gpgsign=false", "commit", "-qm", "First runtime")
        self.revision = self.git_run("rev-parse", "HEAD").stdout.strip()
        (self.project / "chainman.lock").write_text(self.revision + "\n")
        shutil.copyfile(SOURCE / "bootstrap/chainman.just", self.project / "justfile")
        self.wrapper = self.binaries / "git"
        self.wrapper.write_text(
            "#!/bin/sh\nset -eu\n"
            'case " $* " in *" fetch "*)\n'
            '    printf "fetch\\n" >> "$BOOTSTRAP_TEST_LOG"\n'
            '    test "${BOOTSTRAP_TEST_OFFLINE:-0}" = 0 || exit 71\n'
            '    test "${BOOTSTRAP_TEST_INTERRUPT:-0}" = 0 || exit 72\n'
            "esac\n"
            f'exec "{self.git}" -c "url.{self.origin.as_uri()}/.insteadOf={REPOSITORY}" '
            '-c protocol.file.allow=always "$@"\n'
        )
        self.wrapper.chmod(0o755)
        for command in (
            "just",
            "sh",
            "wc",
            "mkdir",
            "cat",
            "sleep",
            "mktemp",
            "ln",
            "rm",
            "tar",
        ):
            executable = shutil.which(command) or shutil.which(command, path=os.defpath)
            self.assertIsNotNone(executable, command)
            (self.binaries / command).symlink_to(executable)
        self.env = dict(
            os.environ,
            PATH=str(self.binaries),
            HOME=str(self.home),
            XDG_CACHE_HOME=str(self.home / "cache"),
            BOOTSTRAP_TEST_LOG=str(self.root / "fetches"),
        )
        self.cache = (
            self.home
            / "cache/chainman/git/github.com-chainmandev-chainman"
            / (self.revision + ".git")
        )

    def git_run(self, *args):
        return subprocess.run(
            [self.git, "-C", str(self.origin), *args],
            text=True,
            capture_output=True,
            check=True,
        )

    def run_entry(self, *args, input=b"", **environment):
        return subprocess.run(
            [str(self.binaries / "just"), "chainman", *args],
            cwd=self.project,
            env=dict(self.env, **environment),
            input=input,
            capture_output=True,
            timeout=30,
        )

    def test_small_recipe_cold_warm_offline_and_literal_streams(self):
        lines = (SOURCE / "bootstrap/chainman.just").read_text().splitlines()
        self.assertLessEqual(
            sum(bool(line.strip()) for line in lines if line.startswith("    ")), 25
        )
        args = ["a b", "$(touch unwanted)", "'quotes'", "", "line\nbreak"]
        result = self.run_entry("report", *args, input=b"caller input\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            b"\0".join(s.encode() for s in [str(self.project), *args])
            + b"\0caller input\n",
        )
        warm = self.run_entry("report", BOOTSTRAP_TEST_OFFLINE="1")
        self.assertEqual(warm.returncode, 0, warm.stderr)
        self.assertEqual((self.root / "fetches").read_text(), "fetch\n")
        self.assertFalse((self.project / "unwanted").exists())

    def test_malformed_and_unavailable_pins_do_not_execute(self):
        for pin in (
            "main\n",
            self.revision.upper() + "\n",
            self.revision,
            self.revision + "\n\n",
            '{"schema":1}\n',
            "0" * 40 + "\n",
        ):
            with self.subTest(pin=pin):
                (self.project / "chainman.lock").write_text(pin)
                result = self.run_entry("report")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")

    def test_caller_repository_environment_is_not_cache_authority(self):
        result = self.run_entry(
            "report",
            GIT_DIR=str(self.origin / ".git"),
            GIT_WORK_TREE=str(self.origin),
            GIT_INDEX_FILE=str(self.root / "other-index"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "other-index").exists())
        self.assertEqual(self.git_run("status", "--porcelain").stdout, "")

    def test_interrupted_fetch_can_be_retried(self):
        interrupted = self.run_entry("report", BOOTSTRAP_TEST_INTERRUPT="1")
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertEqual(interrupted.stdout, b"")
        retry = self.run_entry("report")
        self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_concurrent_first_use(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.run_entry("report"), range(8)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.cache.parent.glob(self.revision + ".git.*"))), 1)
        self.assertFalse(list(self.cache.glob(self.revision + ".git.*")))

    def test_mutable_checkout_and_replacement_refs_are_ignored(self):
        self.assertEqual(self.run_entry("report").returncode, 0)
        (self.cache / "bootstrap").mkdir()
        (self.cache / "bootstrap/git-entry.sh").write_text("exit 99\n")
        (self.origin / "bootstrap/git-entry.sh").write_text("exit 98\n")
        self.git_run("add", ".")
        self.git_run("-c", "commit.gpgsign=false", "commit", "-qm", "Other runtime")
        other = self.git_run("rev-parse", "HEAD").stdout.strip()
        subprocess.run(
            [
                self.git,
                "--git-dir=" + str(self.cache),
                "fetch",
                str(self.origin),
                other,
            ],
            check=True,
            capture_output=True,
        )
        replacements = self.cache / "refs/replace"
        replacements.mkdir(parents=True)
        (replacements / self.revision).write_text(other + "\n")
        result = self.run_entry("report", BOOTSTRAP_TEST_OFFLINE="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupted_objects_fail_before_execution(self):
        self.assertEqual(self.run_entry("report").returncode, 0)
        oid = self.git_run("rev-parse", "HEAD:bootstrap/git-entry.sh").stdout.strip()
        obj = self.cache / "objects" / oid[:2] / oid[2:]
        obj.parent.mkdir(exist_ok=True)
        body = b"exit 99\n"
        if obj.exists():
            obj.chmod(0o600)
        obj.write_bytes(
            zlib.compress(b"blob " + str(len(body)).encode() + b"\0" + body)
        )
        result = self.run_entry("report")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_git_configuration_is_scoped_to_cache_operations(self):
        # Cache commands must not inherit configuration injections, but the
        # selected runtime still sees the caller's ordinary global Git policy.
        policy = self.home / "git policy"
        policy.write_text("[user]\n    name = Preserved identity\n")
        (self.origin / "bootstrap/git-entry.sh").write_text(
            "git config --global --get user.name\n"
        )
        self.git_run("add", ".")
        self.git_run("-c", "commit.gpgsign=false", "commit", "-qm", "Identity fixture")
        revision = self.git_run("rev-parse", "HEAD").stdout.strip()
        (self.project / "chainman.lock").write_text(revision + "\n")
        result = self.run_entry("report", GIT_CONFIG_GLOBAL=str(policy))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"Preserved identity\n")

    def test_entry_materializes_only_the_verified_tree(self):
        shutil.copyfile(
            SOURCE / "bootstrap/git-entry.sh", self.origin / "bootstrap/git-entry.sh"
        )
        shutil.copyfile(
            SOURCE / "bootstrap/lifetime.sh", self.origin / "bootstrap/lifetime.sh"
        )
        runtime = self.origin / "bootstrap/chainman.sh"
        runtime.write_text(
            "#!/bin/sh\nset -eu\n"
            'case "$1" in\n'
            ' stdin) shift; printf "%s\\0" "$@"; cat; exit 37 ;;\n'
            'esac\ncat "$(dirname "$0")/../VERSION"\n'
        )
        runtime.chmod(0o755)
        (self.origin / "VERSION").write_text("verified\n")
        self.git_run("add", ".")
        self.git_run(
            "-c", "commit.gpgsign=false", "commit", "-qm", "Real Git entry fixture"
        )
        revision = self.git_run("rev-parse", "HEAD").stdout.strip()
        (self.project / "chainman.lock").write_text(revision + "\n")
        (self.origin / "VERSION").write_text("uncommitted\n")
        (self.binaries / "dirname").symlink_to(shutil.which("dirname"))
        result = self.run_entry("report", GIT_DIR=str(self.origin / ".git"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"verified\n")
        result = self.run_entry("stdin", "literal $() spaces", input=b"piped input\n")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertEqual(result.stdout, b"literal $() spaces\0piped input\n")

    def test_real_entry_forwards_signals_directed_only_at_entrypoint(self):
        for name in ("git-entry.sh", "lifetime.sh"):
            shutil.copyfile(
                SOURCE / "bootstrap" / name, self.origin / "bootstrap" / name
            )
        runtime = self.origin / "bootstrap/chainman.sh"
        runtime.write_text(
            "#!/bin/sh\nset -eu\n"
            'trap \'sleep "${FIXTURE_SHUTDOWN_DELAY:-0}"; printf stopped > "$CHAINMAN_PROJECT_ROOT/stopped"; exit 0\' TERM HUP\n'
            'printf ready > "$CHAINMAN_PROJECT_ROOT/ready"\n'
            "while :; do sleep 1; done\n"
        )
        runtime.chmod(0o755)
        self.git_run("add", ".")
        self.git_run("-c", "commit.gpgsign=false", "commit", "-qm", "Signal fixture")
        revision = self.git_run("rev-parse", "HEAD").stdout.strip()
        for sent, expected in [
            (signal.SIGTERM, 143),
            (signal.SIGHUP, 129),
            (signal.SIGINT, 130),
        ]:
            with self.subTest(signal=sent):
                for name in ("ready", "stopped"):
                    (self.project / name).unlink(missing_ok=True)
                process = subprocess.Popen(
                    [
                        "sh",
                        str(SOURCE / "bootstrap/git-entry.sh"),
                        str(self.project),
                        str(self.origin / ".git"),
                        revision,
                    ],
                    env=dict(
                        self.env,
                        FIXTURE_SHUTDOWN_DELAY="6" if sent == signal.SIGTERM else "0",
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not (self.project / "ready").exists():
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    process.send_signal(sent)
                    output = process.communicate(timeout=12)
                    self.assertEqual(process.returncode, expected, output)
                    self.assertTrue((self.project / "stopped").exists())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=5)

    def test_exit_status_and_signal(self):
        self.assertEqual(self.run_entry("failure").returncode, 37)
        process = subprocess.Popen(
            [str(self.binaries / "just"), "chainman", "wait"],
            cwd=self.project,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(
            lambda: process.poll() is None and os.killpg(process.pid, signal.SIGKILL)
        )
        self.assertEqual(process.stdout.readline(), b"ready\n")
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)


if __name__ == "__main__":
    unittest.main()
