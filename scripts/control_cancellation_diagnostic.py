"""Keep native ownership and cancellation probes in one pinned profile lifetime."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def run_phase(
    command: list[str],
    log: Path,
    environment: dict[str, str],
    timeout: float,
    cleanup_timeout: float = 5,
) -> dict[str, int | float | bool]:
    start = time.monotonic()
    timed_out = False
    cleanup_incomplete = False
    with log.open("ab") as output:
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            code = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Address only the phase process we created. unittest can finish
            # its disposable fixture through its normal interruption cleanup.
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=cleanup_timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.wait(timeout=cleanup_timeout)
                except subprocess.TimeoutExpired:
                    cleanup_incomplete = True
            code = 124
        finally:
            if child.poll() is None and not cleanup_incomplete:
                child.kill()
                try:
                    child.wait(timeout=cleanup_timeout)
                except subprocess.TimeoutExpired:
                    cleanup_incomplete = True
    return {
        "exit": code,
        "observation_timeout": timed_out,
        "cleanup_incomplete": cleanup_incomplete,
        "elapsed_seconds": time.monotonic() - start,
    }


def main() -> int:
    directory = ROOT / "cancellation-diagnostic"
    directory.mkdir(exist_ok=True)
    source = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    receipt: dict[str, object] = {"source": source, "diagnostic_only": True}
    outcomes = []
    for name, script, target, timeout in (
        ("ownership", "control_test.py", "anchor", 18 * 60),
        ("repetitions", "task_cancellation_diagnostic.py", "outer", 4 * 60),
    ):
        log = directory / (name + ".log")
        log.write_text(f"Source: {source}\nPhase: {name} in owning control profile\n")
        print(f"Cancellation diagnostic phase: {name}", flush=True)
        result = run_phase(
            [sys.executable, "-B", str(ROOT / "scripts" / script)],
            log,
            dict(os.environ, CHAINMAN_TEST_TASK_STACK_TARGET=target),
            timeout,
        )
        receipt[name] = result
        outcomes.append(int(result["exit"]))
        (directory / "phases.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"Cancellation diagnostic phase complete: {name}: {result}", flush=True)
        if result.get("cleanup_incomplete"):
            break
    # A positive repetition never hides a failed full gate or observer limit.
    return next((code for code in outcomes if code != 0), 0)


if __name__ == "__main__":
    raise SystemExit(main())
