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
    for suffix in "astro bash c cc cjs cpp cs css cts dart go gql graphql h hpp html j2 java js json jsx just kt kts less lua m mdx mjs mts nix php pl py r rb rs scss sh sql svelte swift toml ts tsx vue xml yaml yml".split()
] + ["[Jj]ustfile", "**/[Jj]ustfile", "Dockerfile", "**/Dockerfile"]
SCANNER = "anti-trojan-source@1.12.1:high:v2"


def outgoing(root: Path, records: bytes) -> list[str]:
    commits: dict[str, None] = {}
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
                dict.fromkeys(
                    staged_format.git(
                        root, "rev-list", "--topo-order", "--reverse", *arguments
                    )
                    .stdout.decode()
                    .splitlines()
                )
            )
        elif kind in {b"tree", b"blob"}:
            direct.add(peeled)
        else:
            raise ValueError(f"Unsupported outgoing Git object {local}")
    return list(dict.fromkeys([*commits, *sorted(direct)]))


def tree_changes(
    root: Path, previous: str | None, revision: str
) -> list[tuple[str, str, str]]:
    """Enumerate the first tree, then only destination entries changed between trees.

    The traversal visits every selected tree, including merge resolutions. A new
    path is evaluated even when its blob already appeared at an excepted path.
    """
    if previous is None:
        result = []
        for record in staged_format.git(
            root, "ls-tree", "-r", "-z", revision
        ).stdout.split(b"\0"):
            if record:
                header, path = record.split(b"\t", 1)
                mode, kind, blob = header.decode().split()
                if kind == "blob":
                    result.append((mode, blob, os.fsdecode(path)))
        return result
    records = staged_format.git(
        root,
        "diff-tree",
        "--no-commit-id",
        "--raw",
        "--no-abbrev",
        "-r",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        previous,
        revision,
    ).stdout.split(b"\0")
    return [
        (header.split()[1].decode(), header.split()[3].decode(), os.fsdecode(path))
        for header, path in zip(records[0:-1:2], records[1::2], strict=True)
    ]


def binary_executable(body: bytes) -> bool:
    # Only the implicit executable fallback permits recognized native binaries.
    # An explicit source pattern must never be bypassed by a NUL or magic bytes.
    if body.startswith(b"MZ") and len(body) >= 64:
        offset = int.from_bytes(body[60:64], "little")
        return offset >= 64 and body[offset : offset + 4] == b"PE\0\0"
    return body.startswith(
        (
            b"\x7fELF",
            b"\xfe\xed\xfa\xce",
            b"\xce\xfa\xed\xfe",
            b"\xfe\xed\xfa\xcf",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
            b"\xca\xfe\xba\xbf",
            b"\xbf\xba\xfe\xca",
        )
    )


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
    blobs: dict[str, tuple[str, str, bool]] = {}
    previous = None
    for number, revision in enumerate(roots):
        kind = staged_format.git(root, "cat-file", "-t", revision).stdout.strip()
        if kind == b"blob":
            blobs[revision] = (revision, "<blob tag>", True)
            continue
        for mode, blob, path in tree_changes(root, previous, revision):
            source = formatters.matches(path, patterns)
            if (
                mode not in {"100644", "100755"}
                or not (source or ("paths" not in spec and mode == "100755"))
                or (path, blob) in allowed
            ):
                continue
            if blob not in blobs or (source and not blobs[blob][2]):
                blobs[blob] = (revision, path, source)
        previous = revision
        if number and number % 500 == 0:
            print(
                f"Trojan Source: inspected {number + 1} outgoing trees", file=sys.stderr
            )
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
        texts = []
        scanned = []
        for blob, body in zip(batch, bodies, strict=True):
            revision, path, source = blobs[blob]
            try:
                texts.append(body.decode("utf-8"))
            except UnicodeDecodeError:
                if not source and binary_executable(body):
                    print(
                        f"Trojan Source: native executable not scanned: {revision} {path!r}",
                        file=sys.stderr,
                    )
                    continue
                raise ValueError(
                    f"Trojan Source: unsupported source encoding: {revision} {path!r}; expected UTF-8"
                ) from None
            scanned.append(blob)
        if not scanned:
            continue
        result = chainman.execute(
            root,
            "hooks",
            ["node", str(chainman.RUNTIME / "scripts/trojan-source.mjs")],
            input=json.dumps(texts).encode(),
            capture_output=True,
        )
        findings = json.loads(result.stdout)
        if not isinstance(findings, list) or len(findings) != len(scanned):
            raise ValueError("Invalid Trojan Source scanner response")
        for blob, issues in zip(scanned, findings, strict=True):
            if not isinstance(issues, list):
                raise ValueError("Invalid Trojan Source findings")
            if issues:
                failures += 1
                revision, path, _ = blobs[blob]
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
