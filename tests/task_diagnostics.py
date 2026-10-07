"""Failure evidence limited to a disposable native task and its owned group."""

import json
import os
import select
import signal
import subprocess
import time


def task_process_snapshot(pid, group):
    snapshot = {"outer_pid": pid, "anchor_group": group, "processes": []}
    try:
        if pid <= 1 or group is None or group <= 1:
            raise ValueError("No disposable task identity")
        result = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,comm="],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        rows = {}
        for line in result.stdout.splitlines():
            fields = line.split(None, 4)
            if len(fields) != 5:
                continue
            try:
                process, parent, process_group = map(int, fields[:3])
            except ValueError:
                continue
            rows[process] = {
                "pid": process,
                "parent": parent,
                "group": process_group,
                "state": fields[3],
                "executable": os.path.basename(fields[4]),
            }
        owned = {pid}
        owned.update(row["pid"] for row in rows.values() if row["group"] == group)
        for _ in range(64):
            descendants = {
                row["pid"] for row in rows.values() if row["parent"] in owned
            }
            expanded = owned | descendants
            if expanded == owned:
                break
            owned = expanded
        selected = [rows[process] for process in sorted(owned) if process in rows]
        snapshot["processes"] = selected[:64]
        if len(selected) > 64:
            snapshot["truncated"] = True
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        snapshot["error"] = type(error).__name__
    return json.dumps(snapshot, sort_keys=True)


def task_failure_stacks(client, snapshot):
    """Opt-in fatal Go dumps after failure; finish only the verified fixture."""
    evidence = {"diagnostic_only": True}
    if os.environ.get("CHAINMAN_TEST_STOPPED_TASK_STACKS") != "1":
        evidence["skipped"] = "Not explicitly enabled"
        return json.dumps(evidence)
    owned = False
    try:
        group = snapshot["anchor_group"]
        rows = {row["pid"]: row for row in snapshot["processes"]}
        outer = rows[client.pid]
        anchor = rows[group]
        if (
            snapshot["outer_pid"] != client.pid
            or outer["parent"] != os.getpid()
            or anchor["parent"] != client.pid
            or group <= 1
            or anchor["group"] != group
            or not all(
                row["executable"].startswith("chainman-contro")
                and row["state"].startswith(("S", "R"))
                and os.getpgid(row["pid"]) == row["group"]
                for row in (outer, anchor)
            )
            or client.poll() is not None
            or client.stderr is None
        ):
            raise ValueError("Disposable controller ownership changed")
        owned = True
        fd = client.stderr.fileno()
        blocking = os.get_blocking(fd)
        output = bytearray()
        sections = []
        try:
            os.set_blocking(fd, False)
            # Both are running Go controllers observed after the original
            # three-second assertion failed. SIGQUIT is fatal, never repair.
            evidence["targets"] = [client.pid, group]
            deadline = time.monotonic() + 2
            for pid in (client.pid, group):
                if pid == group and os.getpgid(group) != group:
                    raise ValueError("Disposable anchor ownership changed")
                start = len(output)
                os.kill(pid, signal.SIGQUIT)
                until = min(deadline, time.monotonic() + 1)
                while len(output) < 65536 and time.monotonic() < until:
                    if select.select([fd], [], [], 0.05)[0]:
                        try:
                            chunk = os.read(fd, min(4096, 65536 - len(output)))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            break
                        output.extend(chunk)
                    elif pid == client.pid and client.poll() is not None:
                        break
                sections.append(
                    {"pid": pid, "start": start, "bytes": len(output) - start}
                )
        finally:
            os.set_blocking(fd, blocking)
        evidence["stack"] = output.decode(errors="replace")
        evidence["stack_bytes"] = len(output)
        evidence["sections"] = sections
    except (OSError, KeyError, ValueError) as error:
        evidence["error"] = type(error).__name__
    finally:
        if owned:
            # Fatal dumps can let the outer exit before the normal test cleanup
            # notices stopped descendants. The validated disposable group must
            # still be finished; no unrelated process or group is addressed.
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError as error:
                evidence["group_cleanup_error"] = type(error).__name__
            if client.poll() is None:
                try:
                    client.kill()
                except OSError as error:
                    evidence["outer_cleanup_error"] = type(error).__name__
    return json.dumps(evidence)
