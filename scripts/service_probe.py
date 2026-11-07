"""Application deadline after verified service profile activation."""

import subprocess
import sys

import toolchain as tc


def main() -> int:
    if len(sys.argv) < 3:
        raise ValueError("Internal probe requires its deadline and command")
    seconds = int(sys.argv[1])
    if not 1 <= seconds <= 600:
        raise ValueError("Invalid internal probe deadline")
    try:
        result = tc.managed_run(sys.argv[2:], timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        return 124
    return 128 - result.returncode if result.returncode < 0 else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
