"""Exercise coordinator-only options against older candidate entrypoints."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class VerificationBootstrapTests(unittest.TestCase):
    def worker(self, arguments, *, result=0):
        temporary = tempfile.TemporaryDirectory(prefix="chainman verification wiring ")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        root = base / "project"
        root.mkdir()
        output = base / "cache/chainman/updates/v1/candidate.fixture"
        output.mkdir(parents=True)
        entry = base / "entry"
        entry.write_text("""#!/bin/sh
set -eu
case "$1" in
_update-prepare)
  output=$2
  printf '%s\\n' "$@" > "$TEST_BASE/arguments"
  mkdir "$output/candidate/.chainman-workspace-transactions"
  printf '2026-10-12T00:00:00Z\\n' > "$output/control/at"
  mkdir "$output/original-bootstrap" "$output/candidate-bootstrap"
  cp "$0" "$output/original-bootstrap/chainman.sh"
  cp "$0" "$output/candidate-bootstrap/chainman.sh" ;;
_update-runtime|_update-resolve|_update-reaudit) ;;
_update-tasks) ;;
_update-inspect)
  printf 'yes\\n' > "$2/control/changed"
  printf 'run\\nfirst\\nrun\\nsecond\\n' > "$2/control/verify" ;;
_verification-update-plan) printf '{}\\n' ;;
verification) test -z "$(cat)"; printf 'coordinated\\n' >> "$TEST_BASE/events"; exit "$TEST_RESULT" ;;
run) printf '%s\\n' "$2" >> "$TEST_BASE/events"; exit "$TEST_RESULT" ;;
_update-finalize) touch "$TEST_BASE/finalized" ;;
*) exit 2 ;;
esac
""")
        entry.chmod(0o700)
        bootstrap = (SOURCE / "bootstrap/chainman.sh").read_text()
        worker = bootstrap[
            bootstrap.index("update_worker() {") : bootstrap.index(
                "\nupdate_export_stage() {"
            )
        ]
        candidate_entry = bootstrap[
            bootstrap.index("update_candidate() (") : bootstrap.index("\nexpression=")
        ]
        script = (
            'set -eu\nfail() { echo "$*" >&2; exit 2; }\nsingle_line() { :; }\n'
            + worker
            + "\n"
            + candidate_entry
            + '\nupdate_worker "$@"\n'
        )
        env = dict(
            os.environ,
            TEST_BASE=str(base),
            TEST_RESULT=str(result),
            XDG_CACHE_HOME=str(base / "cache"),
            CHAINMAN_UPDATE_TRANSACTION=str(output),
            CHAINMAN_PROJECT_ROOT=str(root),
            CHAINMAN_UPDATE_HELPER=str(entry),
        )
        prefix = (
            "root="
            + shlex.quote(str(root))
            + "\nsource_root="
            + shlex.quote(str(SOURCE))
            + "\nself="
            + shlex.quote(str(entry))
            + "\nmode=host-nix\n"
        )
        completed = subprocess.run(
            ["sh", "-c", prefix + script, "fixture", *arguments],
            env=env,
            text=True,
            capture_output=True,
            input="unexpected candidate stdin",
        )
        return completed, base

    def test_keep_going_is_consumed_before_older_candidate_and_success_finalizes(self):
        result, base = self.worker(["--only-chainman", "--keep-going"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--keep-going", (base / "arguments").read_text())
        self.assertEqual((base / "events").read_text(), "coordinated\n")
        self.assertTrue((base / "finalized").exists())

    def test_failed_aggregate_does_not_finalize(self):
        result, base = self.worker(["--keep-going"], result=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((base / "finalized").exists())

    def test_default_is_fail_fast_and_argument_values_are_not_options(self):
        result, base = self.worker(["--message", "--keep-going"], result=7)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("--keep-going", (base / "arguments").read_text())
        self.assertEqual((base / "events").read_text(), "first\n")
        self.assertFalse((base / "finalized").exists())

    def test_resolver_passthrough_is_not_consumed(self):
        result, base = self.worker(["--", "--keep-going"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--keep-going", (base / "arguments").read_text())
        self.assertEqual((base / "events").read_text(), "first\nsecond\n")
