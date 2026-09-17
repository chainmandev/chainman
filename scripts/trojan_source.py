"""Check outgoing Git content, including intermediate commits and tag objects."""

import hashlib
import json
import os
from pathlib import Path
import re
import sys

import chainman
import formatters
import staged_format
import toolchain as tc
from adapter_data import array, strings, table, text

DEFAULT_PATHS = [
    "*." + suffix
    for suffix in "c h cc cpp hpp cs css go html java js jsx mjs cjs json kt kts lua m mts cts php pl py r rb rs sh bash sql swift toml ts tsx vue xml yaml yml".split()
]
SCANNER = "anti-trojan-source@1.12.1:high:v1"


def outgoing(root: Path, records: bytes) -> list[str]:
    commits: set[str] = set()
    direct: set[str] = set()
    for line in records.splitlines():
        fields = line.split()
        if len(fields) != 4 or any(
            not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", value)
            for value in (fields[1], fields[3])
        ):
            raise ValueError("Malformed Git pre-push record")
        local, remote = fields[1].decode(), fields[3].decode()
        if not local.strip("0"):
            continue
        peeled = (
            staged_format.git(root, "rev-parse", "--verify", local + "^{}")
            .stdout.strip()
            .decode()
        )
        kind = staged_format.git(root, "cat-file", "-t", peeled).stdout.strip()
        if kind == b"commit":
            arguments = [local]
            if remote.strip("0"):
                base = staged_format.git(
                    root, "rev-parse", "--verify", remote + "^{commit}", check=False
                )
                if base.returncode == 0:
                    arguments.append("^" + base.stdout.strip().decode())
                else:
                    print(
                        f"Trojan Source: remote base {remote} is unavailable; scanning all locally reachable history for {local}",
                        file=sys.stderr,
                    )
            commits.update(
                staged_format.git(root, "rev-list", *arguments)
                .stdout.decode()
                .splitlines()
            )
        elif kind in {b"tree", b"blob"}:
            direct.add(peeled)
        else:
            raise ValueError(f"Unsupported outgoing Git object {local}")
    return sorted(commits | direct)


def run(root: Path, arguments: list[str]) -> int:
    spec = table(
        table(tc.config(root).get("hooks", {}), "Hooks").get("trojan_source", {}),
        "Trojan Source",
    )
    if set(spec) - {"paths", "exceptions"}:
        raise ValueError("Unknown hooks.trojan_source setting")
    patterns = strings(spec.get("paths", DEFAULT_PATHS), "Trojan Source paths")
    exceptions = array(spec.get("exceptions", []), "Trojan Source exceptions")
    allowed: set[tuple[str, str]] = set()
    for raw in exceptions:
        item = table(raw, "Trojan Source exception")
        if (
            set(item) != {"path", "blob", "reason"}
            or not text(item["reason"], "Exception reason").strip()
        ):
            raise ValueError(
                "Trojan Source exceptions require exact path, blob and reason"
            )
        allowed.add(
            (text(item["path"], "Exception path"), text(item["blob"], "Exception blob"))
        )
    if arguments:
        if len(arguments) != 1 or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", arguments[0]
        ):
            raise ValueError(
                "Use trojan-source [full-commit-SHA]; without a SHA provide Git pre-push records on stdin"
            )
        roots = [arguments[0]]
    else:
        roots = outgoing(root, sys.stdin.buffer.read())
    blobs: dict[str, list[tuple[str, str]]] = {}
    for revision in roots:
        kind = staged_format.git(root, "cat-file", "-t", revision).stdout.strip()
        if kind == b"blob":
            blobs.setdefault(revision, []).append((revision, "<blob tag>"))
            continue
        for record in staged_format.git(
            root, "ls-tree", "-r", "-z", revision
        ).stdout.split(b"\0"):
            if not record:
                continue
            header, path_bytes = record.split(b"\t", 1)
            mode, object_type, blob = header.decode().split()
            path = os.fsdecode(path_bytes)
            if (
                mode not in {"100644", "100755"}
                or object_type != "blob"
                or not formatters.matches(path, patterns)
                or (path, blob) in allowed
            ):
                continue
            blobs.setdefault(blob, []).append((revision, path))
    # Cache only clean results, scoped to scanner, classification and exact policy.
    policy = hashlib.sha256(
        (SCANNER + json.dumps(spec, sort_keys=True)).encode()
    ).hexdigest()
    cache = tc.contained(root, ".cache/toolchain/trojan-source/" + policy)
    cache.mkdir(parents=True, exist_ok=True)
    pending = [blob for blob in blobs if not (cache / blob).is_file()]
    failures = 0
    for start in range(0, len(pending), 128):
        batch = pending[start : start + 128]
        bodies = [
            staged_format.git(root, "cat-file", "blob", blob).stdout for blob in batch
        ]
        # Binary data has no source text; malformed UTF-8 in classified source
        # fails instead of silently changing the scanner's input.
        texts = [body.decode("utf-8") if b"\0" not in body else "" for body in bodies]
        result = chainman.execute(
            root,
            "hooks",
            ["node", str(chainman.RUNTIME / "scripts/trojan-source.mjs")],
            input=json.dumps(texts).encode(),
            capture_output=True,
        )
        findings = json.loads(result.stdout)
        if not isinstance(findings, list) or len(findings) != len(batch):
            raise ValueError("Invalid Trojan Source scanner response")
        for blob, issues in zip(batch, findings, strict=True):
            if not isinstance(issues, list):
                raise ValueError("Invalid Trojan Source findings")
            if issues:
                failures += 1
                for revision, path in blobs[blob]:
                    for raw in issues:
                        issue = table(raw, "Scanner finding")
                        print(
                            f"Trojan Source: {revision} {path!r}:{issue['line']}:{issue['column']} {issue['codePoint']} {issue['name']}",
                            file=sys.stderr,
                        )
            else:
                tc.atomic_bytes(cache / blob, b"clean\n")
    if failures:
        raise ValueError(
            f"Trojan Source found suspicious characters in {failures} outgoing blob(s)"
        )
    print(
        f"Trojan Source: checked {len(blobs)} distinct source blobs across {len(roots)} Git objects"
    )
    return 0
