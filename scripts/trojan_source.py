"""Check outgoing Git content, including intermediate commits and tag objects."""

import hashlib
import json
from pathlib import Path
import re
import sys

import chainman
import formatters
import toolchain as tc
from adapter_data import array, strings, table, text

DEFAULT_PATHS = [
    "*." + suffix
    for suffix in "astro bash c cc cjs cpp cs css cts dart go gql graphql h hpp html j2 java js json jsx just kt kts less lua m mdx mjs mts nix php pl py r rb rs scss sh sql svelte swift toml ts tsx vue xml yaml yml".split()
] + ["[Jj]ustfile", "**/[Jj]ustfile", "Dockerfile", "**/Dockerfile"]
SCANNER = "anti-trojan-source@1.12.1:high:v3"


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


def binary_asset(path: str, body: bytes) -> bool:
    """Recognize non-source media despite an accidental executable Git mode.

    Both extension and signature must agree. This only classifies a non-UTF-8
    executable fallback; explicit source patterns always require source scanning.
    It does not validate decoders or certify media as malware-free.
    """
    signatures = {
        ".png": (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",),
        ".jpg": (b"\xff\xd8\xff",),
        ".jpeg": (b"\xff\xd8\xff",),
        ".gif": (b"GIF87a", b"GIF89a"),
        ".ico": (b"\x00\x00\x01\x00",),
        ".woff": (b"wOFF",),
        ".woff2": (b"wOF2",),
        ".webm": (b"\x1a\x45\xdf\xa3",),
    }
    suffix = Path(path).suffix.lower()
    if suffix in signatures:
        return body.startswith(signatures[suffix])
    if suffix == ".mp3":
        start = 0
        if body.startswith(b"ID3"):
            if (
                len(body) < 10
                or body[3] not in (2, 3, 4)
                or any(b & 128 for b in body[6:10])
            ):
                return False
            start = 10 + sum(
                b << shift for b, shift in zip(body[6:10], (21, 14, 7, 0), strict=True)
            )
            if body[3] == 4 and body[5] & 16:
                start += 10
        # MPEG audio sync, nonreserved version/layer, bitrate and sample rate.
        frame = body[start : start + 4]
        return (
            len(frame) == 4
            and frame[0] == 255
            and frame[1] & 224 == 224
            and frame[1] & 24 != 8
            and frame[1] & 6 != 0
            and frame[2] >> 4 not in (0, 15)
            and frame[2] & 12 != 12
        )
    return False


def worker(root: Path, directory: Path, phase: str) -> int:
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
    inputs = table(
        json.loads(tc.regular_input(directory, "input.json")), "Outgoing Git inventory"
    )
    roots = strings(inputs["roots"], "Outgoing roots")
    blobs: dict[str, tuple[str, str, bool]] = {}
    for raw in array(inputs["entries"], "Outgoing entries"):
        entry = table(raw, "Outgoing entry")
        mode, blob, path, revision = (
            text(entry[key], key) for key in ("mode", "blob", "path", "revision")
        )
        source = path == "<blob tag>" or formatters.matches(path, patterns)
        if (
            mode not in {"100644", "100755"}
            or not (source or ("paths" not in spec and mode == "100755"))
            or (path, blob) in allowed
        ):
            continue
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", blob):
            raise ValueError("Invalid outgoing blob identity")
        if blob not in blobs or (source and not blobs[blob][2]):
            blobs[blob] = (revision, path, source)
    # Cache only clean results, scoped to scanner, classification and exact policy.
    policy = hashlib.sha256(
        (SCANNER + json.dumps(spec, sort_keys=True)).encode()
    ).hexdigest()
    cache = tc.contained(root, ".cache/toolchain/trojan-source/" + policy)
    cache.mkdir(parents=True, exist_ok=True)
    pending = [blob for blob in blobs if not (cache / blob).is_file()]
    if phase == "scan-select":
        tc.atomic_json(directory / "result" / "requested.json", pending)
        return 0
    requested = strings(
        json.loads(tc.regular_input(directory, "result/requested.json")),
        "Selected scanner blobs",
    )
    if any(blob not in blobs for blob in requested):
        raise ValueError("Scanner selection changed")
    pending = requested
    failures = 0
    for start in range(0, len(pending), 128):
        batch = pending[start : start + 128]
        bodies = [tc.regular_input(directory, "blobs/" + blob) for blob in batch]
        texts = []
        scanned = []
        for blob, body in zip(batch, bodies, strict=True):
            revision, path, source = blobs[blob]
            try:
                texts.append(body.decode("utf-8"))
            except UnicodeDecodeError:
                kind = (
                    "native executable"
                    if binary_executable(body)
                    else "binary asset"
                    if binary_asset(path, body)
                    else None
                )
                if not source and kind:
                    print(
                        f"Trojan Source: {kind} not scanned: {revision} {path!r}",
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
