"""Run the actual shell supervisor with occupied inherited descriptors."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1]


class LifetimeTests(unittest.TestCase):
    def check_stdin_and_descriptor_ownership(self, descriptors):
        with tempfile.TemporaryDirectory(prefix="chainman lifetime ") as directory:
            (Path(directory) / "child.sh").write_text(
                r"""cat
for descriptor in "$@"; do
    eval 'printf "%s\n" child >&'"$descriptor"
done
exit 23
"""
            )
            result = subprocess.run(
                [
                    shutil.which("bash"),
                    "-eu",
                    "-c",
                    r"""
directory=$1
source=$2
shift 2
for descriptor in "$@"; do
    eval 'exec '"$descriptor"'>"$directory/fd-'"$descriptor"'"'
done
. "$source/bootstrap/lifetime.sh"
result=0
lifetime_run sh "$directory/child.sh" "$@" || result=$?
for descriptor in "$@"; do
    eval 'printf "%s\n" parent >&'"$descriptor"
done
exit "$result"
""",
                    "lifetime-test",
                    directory,
                    str(SOURCE),
                    *(str(descriptor) for descriptor in descriptors),
                ],
                input="literal stdin $() `command`\nsecond line\n",
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(
                result.stdout, "literal stdin $() `command`\nsecond line\n"
            )
            for descriptor in descriptors:
                self.assertEqual(
                    (Path(directory) / f"fd-{descriptor}").read_text(),
                    "child\nparent\n",
                )

    def test_stdin_and_caller_descriptor_survive(self):
        self.check_stdin_and_descriptor_ownership([9])

    def test_full_posix_descriptor_range_preserves_stdin_and_ownership(self):
        self.check_stdin_and_descriptor_ownership(range(3, 10))
