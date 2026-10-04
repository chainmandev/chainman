"""Launch runtime modules with Python -I, without candidate import discovery."""

from pathlib import Path
import runpy
import sys


def main() -> None:
    if not sys.flags.isolated:
        raise ValueError("Runtime launcher requires Python -I")
    directory = Path(__file__).resolve().parent
    name = sys.argv.pop(1)
    if name not in {
        "source_workflow.py",
        "updates.py",
        "chainman.py",
        "candidate_export.py",
    }:
        raise ValueError("Unknown isolated runtime entry")
    sys.path.insert(0, str(directory))
    runpy.run_path(str(directory / name), run_name="__main__")


if __name__ == "__main__":
    main()
