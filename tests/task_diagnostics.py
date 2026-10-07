"""Read-only failure evidence for a disposable native task and its owned group."""

import json
import os
import subprocess


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
