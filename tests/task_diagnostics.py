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
    """Opt-in fatal Go dump after failure; finish only the verified fixture."""
    evidence = {"diagnostic_only": True}
    if os.environ.get("CHAINMAN_TEST_STOPPED_TASK_STACKS") != "1":
        evidence["skipped"] = "Not explicitly enabled"
        return json.dumps(evidence)
    owned = False
    output = bytearray()
    try:
        target = os.environ.get("CHAINMAN_TEST_TASK_STACK_TARGET", "anchor")
        if target not in ("anchor", "outer"):
            raise ValueError("Unknown disposable controller target")
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
        try:
            os.set_blocking(fd, False)
            # One fatal dump can end the other controller. Independent failed
            # fixtures select the anchor and outer; never assume both survive.
            pid = group if target == "anchor" else client.pid
            evidence.update(target=target, targets=[pid])
            os.kill(pid, signal.SIGQUIT)
            deadline = time.monotonic() + 2
            while len(output) < 65536:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if select.select([fd], [], [], min(0.05, remaining))[0]:
                    try:
                        chunk = os.read(fd, min(4096, 65536 - len(output)))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        break
                    output.extend(chunk)
                elif target == "outer" and client.poll() is not None:
                    break
        finally:
            os.set_blocking(fd, blocking)
    except (OSError, KeyError, ValueError) as error:
        evidence["error"] = type(error).__name__
    finally:
        if owned:
            # Retain bytes even if a target or descriptor disappeared. Observer
            # failures must not erase the already-failed fixture's evidence.
            evidence["stack"] = output.decode(errors="replace")
            evidence["stack_bytes"] = len(output)
            evidence["outer_exit_before_cleanup"] = client.poll()
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError as error:
                evidence["group_cleanup_error"] = type(error).__name__
            if client.poll() is None:
                try:
                    client.kill()
                except OSError as error:
                    evidence["outer_cleanup_error"] = type(error).__name__
            try:
                evidence["outer_exit_after_cleanup"] = client.wait(timeout=1)
            except (OSError, subprocess.SubprocessError) as error:
                evidence["outer_wait_error"] = type(error).__name__
            else:
                for stream in (client.stdout, client.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError as error:
                            evidence.setdefault("pipe_close_errors", []).append(
                                type(error).__name__
                            )
    return json.dumps(evidence)
