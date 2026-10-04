"""Detached, versioned dependency candidates; never an application authority.

The coordinator writes accepted bytes before project verification. Only the
separate result changes afterward. Callers own custody after the command exits.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import uuid

import adapter_data as ad
import toolchain as tc

MAX_MANIFEST = 16 * 1024 * 1024
MAX_BLOB = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MAX_FILES = 100000
RESULTS = {
    "verified_success",
    "complete_no_change",
    "accepted_verification_failed",
    "failure_before_acceptance",
    "unsupported",
    "interrupted_or_unknown",
}
STAGES = {"preparation", "resolution", "inspection", "verification", "complete"}


class Unsupported(ValueError):
    """This resolver cannot establish the public candidate contract."""


def split_arguments(args: list[str]) -> tuple[str | None, list[str]]:
    target = None
    rest: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--export-candidate" or value.startswith("--export-candidate="):
            if target is not None:
                raise ValueError("Specify --export-candidate only once")
            if value == "--export-candidate":
                index += 1
                if index == len(args):
                    raise ValueError("--export-candidate requires a destination")
                target = args[index]
            else:
                target = value.split("=", 1)[1]
            if not target or any(c in target for c in "\0\r\n,"):
                raise ValueError("Export requires a nonempty, single-line mount path")
        else:
            rest.append(value)
        index += 1
    if target is not None:
        from chainman_updates import options

        if any(value.startswith("resume=") for value in rest):
            raise ValueError("Export cannot resume a retained transaction")
        opts = options(rest)
        if opts.format or opts.preview or opts.no_commit or opts.staged:
            raise ValueError(
                "Export cannot combine with format, preview or commit controls"
            )
        if any(value.startswith("commit=") for value in rest):
            raise ValueError("Export cannot combine with commit controls")
    return target, rest


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def encoded(value: object) -> bytes:
    from datetime import date, datetime

    def toml_value(item: object) -> str:
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise TypeError("Unsupported configuration value")

    body = (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=toml_value,
        )
        + "\n"
    ).encode()
    if len(body) > MAX_MANIFEST:
        raise Unsupported("Export manifest exceeds 16 MiB")
    return body


def duplicate_free(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate export JSON key")
        result[key] = value
    return result


def manifest_bytes(root: Path, name: str) -> bytes:
    path = tc.contained(root, name)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Export manifest must be a regular file")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Export manifest must be a regular file")
        if info.st_size > MAX_MANIFEST:
            raise ValueError("Export manifest exceeds 16 MiB")
        # The file can grow after fstat; never allocate its unbounded contents.
        body = stream.read(MAX_MANIFEST + 1)
    if len(body) > MAX_MANIFEST:
        raise ValueError("Export manifest exceeds 16 MiB")
    return body


def read_json(root: Path, name: str) -> ad.Table:
    return ad.table(
        json.loads(manifest_bytes(root, name), object_pairs_hook=duplicate_free),
        "Export JSON",
    )


def directory(path: Path) -> Path:
    # Reject symlink aliases, rather than legitimizing them with resolve().
    absolute = path.absolute()
    if absolute.resolve() != absolute:
        raise ValueError("Export paths must not contain symlinks or traversal")
    return absolute


def destination(root: Path, target: str, transaction: Path) -> Path:
    path = directory(Path(target) if Path(target).is_absolute() else root / target)
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "chainman/updates"
    )
    for forbidden in (root, tc.RUNTIME, cache.resolve(), transaction):
        if path.is_relative_to(forbidden) or forbidden.is_relative_to(path):
            raise ValueError("Export must be outside project, runtime and update cache")
    if not path.parent.is_dir():
        raise ValueError("Export parent must already exist")
    return path


def start(
    root: Path,
    transaction: Path,
    target: str,
    *,
    source: bool,
    created: bool = False,
    operation: str | None = None,
) -> None:
    import updates

    path = destination(root, target, transaction)
    operation = (
        str(uuid.UUID(operation)) if operation is not None else str(uuid.uuid4())
    )
    updates.repository(root, clean=True)
    if created:
        if not path.is_dir() or any(path.iterdir()):
            raise ValueError("Export requires a new empty directory")
    else:
        path.mkdir(mode=0o700)
    request = {
        "directory": str(path),
        "operation": operation,
        "source": source,
        "stage": "preparation",
    }
    tc.atomic_json(transaction / "control/export.json", request)
    # An interrupted launch has an explicit non-success result, even if no
    # Python exception handler gets to run. snapshot.json is published later.
    write_result(transaction, "interrupted_or_unknown", None)


def request(transaction: Path) -> ad.Table | None:
    if not (transaction / "control/export.json").exists():
        return None
    return read_json(transaction / "control", "export.json")


def stage(transaction: Path, value: str) -> None:
    if value not in STAGES:
        raise ValueError("Unknown export stage")
    data = request(transaction)
    if data is not None:
        data["stage"] = value
        tc.atomic_json(transaction / "control/export.json", data)
        write_result(transaction, "interrupted_or_unknown", None)


def identity(root: Path, revision: str | None = None) -> dict[str, str]:
    import updates

    revision = revision or updates.git(root, "rev-parse", "HEAD")
    tree = updates.git(root, "rev-parse", revision + "^{tree}")
    if not all(re.fullmatch(r"[0-9a-f]{40}", oid) for oid in (revision, tree)):
        raise Unsupported("Candidate export currently supports SHA-1 Git objects")
    return {"commit": revision, "tree": tree, "object_format": "sha1"}


def runtime_identity(revision: str, runtime: Path) -> dict[str, str]:
    import git_runtime

    tree = git_runtime.tree_identity(runtime)
    return {"commit": revision, "tree": tree, "object_format": "sha1"}


def entry(root: Path, name: str) -> dict[str, object]:
    path_name(name)
    path = tc.contained(root, name)
    if not path.exists():
        return {"path": name, "sha256": None, "mode": None, "size": 0}
    if not stat.S_ISREG(path.lstat().st_mode) or path.stat().st_size > MAX_BLOB:
        raise Unsupported("Export supports regular files up to 256 MiB")
    body = tc.regular_input(root, name)
    return {
        "path": name,
        "sha256": digest(body),
        "mode": "100755" if path.stat().st_mode & stat.S_IXUSR else "100644",
        "size": len(body),
    }


def capture(root: Path, transaction: Path) -> None:
    import configuration_files
    import dependency_api
    import dependency_reports
    import git_runtime
    import update_staging as staging
    import updates

    data = request(transaction)
    if data is None:
        return
    state, candidate = staging.read_state(root, transaction)
    inspected = state.require_inspection()
    staging.unchanged(root, state)
    staging.candidate_unchanged(candidate, state)
    if updates.snapshot(candidate) != inspected.updated:
        raise ValueError("Candidate changed before export capture")
    policy = dependency_api.inspection_policy(root)
    adapters: dict[str, tuple[ad.Table, ad.Table]] = {}
    if not state.options.only_chainman:
        if policy.get("resolver"):
            raise Unsupported("Opaque resolver has no supported policy evidence")
        selected, _, adapters = dependency_api.plan_steps(
            root, policy, state.options.extra
        )
        if selected - adapters.keys():
            raise Unsupported(
                "Custom hook targets have no adapter-backed policy evidence"
            )
    config = configuration_files.read(
        root, "toolchain.toml" if state.source else "chainman.toml"
    )
    inputs = set(config.documents)
    if state.source:
        inputs.add("dependencies.toml")
        inputs.update(
            "modules/" + name + ".toml"
            for name in ad.strings(tc.config(root)["modules"], "Modules")
        )
    if policy.get("policy_file"):
        inputs.add(ad.text(policy["policy_file"], "Policy file"))
    for spec, _ in adapters.values():
        inputs.update(dependency_reports.inputs(root, spec))
    inputs.update(state.runtime_files)
    # A complete base-file inventory is conservative comparison data. It is
    # deliberately not advertised as complete semantic dependency discovery.
    if len(state.before) > MAX_FILES:
        raise Unsupported("Export base inventory exceeds 100000 files")
    baseline = [entry(root, name) for name in sorted(state.before)]
    outputs = [entry(candidate, name) for name in sorted(inspected.paths)]
    path = directory(Path(ad.text(data["directory"], "Export directory")))
    blobs = path / "blobs"
    blobs.mkdir(mode=0o700)
    total = 0
    for row in outputs:
        if row["sha256"] is None:
            continue
        name = ad.text(row["path"], "Output path")
        body = tc.regular_input(candidate, name)
        total += len(body)
        if total > MAX_TOTAL or digest(body) != row["sha256"]:
            raise ValueError("Export output changed or exceeds 1 GiB")
        target = blobs / ad.text(row["sha256"], "Output digest")
        if not target.exists():
            tc.atomic_bytes(target, body, 0o400)
    if state.source:
        initial = identity(root, state.identity[1])
        resolution = verification = initial
    else:
        initial = runtime_identity(
            git_runtime.pin(tc.regular_input(root, "chainman.lock")),
            transaction / "original-bootstrap/source",
        )
        resolution = (
            runtime_identity(
                state.runtime_revision, transaction / "resolution-bootstrap/source"
            )
            if state.runtime_revision
            else initial
        )
        verification = (
            runtime_identity(
                git_runtime.pin(tc.regular_input(candidate, "chainman.lock")),
                transaction / "candidate-bootstrap/source",
            )
            if inspected.paths
            else resolution
        )
    manifest = {
        "schema": 1,
        "kind": "chainman.dependency-candidate",
        "operation": data["operation"],
        "base": identity(root, state.identity[1]),
        "runtime_kind": "source" if state.source else "consumer",
        "runtimes": {
            "entry": initial,
            "resolution": resolution,
            "verification": verification,
        },
        "selection": {
            "at": state.at.isoformat(),
            "arguments": state.options.extra,
            "adapters": sorted(adapters),
            "runtime": "not_applicable"
            if state.source
            else state.options.runtime.value,
        },
        "configuration_sha256": digest(encoded(tc.config(root))),
        "policy_sha256": digest(encoded(policy)),
        "input_coverage": "declared_inputs_only",
        "inputs": [entry(root, name) for name in sorted(inputs)],
        "base_inventory": baseline,
        "outputs": outputs,
    }
    # Detect modifications during capture, including modes and Git metadata.
    staging.unchanged(root, state)
    staging.candidate_unchanged(candidate, state)
    if updates.snapshot(candidate) != inspected.updated:
        raise ValueError("Candidate changed during export capture")
    body = encoded(manifest)
    tc.atomic_bytes(path / "snapshot.json", body, 0o400)
    tc.atomic_bytes(
        transaction / "control/export-snapshot", (digest(body) + "\n").encode()
    )


def write_result(
    transaction: Path, outcome: str, exit_code: int | None
) -> dict[str, object]:
    data = request(transaction)
    if data is None:
        raise ValueError("No candidate export requested")
    path = directory(Path(ad.text(data["directory"], "Export directory")))
    marker = transaction / "control/export-snapshot"
    snapshot = marker.read_text().strip() if marker.exists() else None
    if snapshot is not None:
        if digest(manifest_bytes(path, "snapshot.json")) != snapshot:
            raise ValueError("Accepted snapshot changed")
        validate_snapshot(path)
    result: dict[str, object] = {
        "schema": 1,
        "kind": "chainman.update-result",
        "operation": data["operation"],
        "outcome": outcome,
        "stage": data["stage"],
        "exit_code": exit_code,
        "snapshot_sha256": snapshot,
    }
    tc.atomic_bytes(path / "result.json", encoded(result))
    return result


def finish(
    root: Path, transaction: Path, code: int, *, unsupported: bool = False
) -> int:
    import update_staging as staging
    import updates

    if code < 0:
        code = 128 - code
    if code > 255:
        code = 1

    data = request(transaction)
    if data is None:
        return code
    accepted = (transaction / "control/export-snapshot").exists()
    outcome = "unsupported" if unsupported else "failure_before_acceptance"
    if code in (130, 137, 143):
        outcome = "interrupted_or_unknown"
    elif accepted:
        outcome = "interrupted_or_unknown"
        if data["stage"] == "verification":
            outcome = "accepted_verification_failed"
            if code == 0:
                try:
                    state, candidate = staging.read_state(root, transaction)
                    staging.unchanged(root, state)
                    staging.candidate_unchanged(candidate, state)
                    if (
                        updates.snapshot(candidate)
                        != state.require_inspection().updated
                    ):
                        raise ValueError("verification changed candidate sources")
                except (OSError, ValueError, subprocess.CalledProcessError) as error:
                    code = 1
                    print(f"Chainman: verification rejected: {error}", file=sys.stderr)
                else:
                    outcome = (
                        "verified_success"
                        if state.require_inspection().paths
                        else "complete_no_change"
                    )
                    data["stage"] = "complete"
                    tc.atomic_json(transaction / "control/export.json", data)
    if code == 0 and outcome not in {"verified_success", "complete_no_change"}:
        code = 1
    print(json.dumps(write_result(transaction, outcome, code), sort_keys=True))
    return code


def path_name(value: object) -> str:
    name = ad.text(value, "Export path")
    path = PurePosixPath(name)
    if (
        not name
        or path.as_posix() != name
        or path.is_absolute()
        or any(p in {".", "..", ".git"} for p in path.parts)
        or "\\" in name
        or any(c in name for c in "\0\r\n")
    ):
        raise ValueError("Invalid export relative path")
    return name


def keys(data: ad.Table, expected: set[str]) -> None:
    if set(data) != expected:
        raise ValueError("Unknown or missing export fields")


def hash_value(value: object) -> str:
    value = ad.text(value, "SHA-256")
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid export SHA-256")
    return value


def validate_identity(value: object) -> None:
    data = ad.table(value, "Git identity")
    keys(data, {"commit", "tree", "object_format"})
    if data["object_format"] != "sha1" or any(
        re.fullmatch(r"[0-9a-f]{40}", ad.text(data[k], k)) is None
        for k in ("commit", "tree")
    ):
        raise ValueError("Unsupported export Git identity")


def validate_snapshot(path: Path) -> ad.Table:
    data = read_json(path, "snapshot.json")
    keys(
        data,
        {
            "schema",
            "kind",
            "operation",
            "base",
            "runtime_kind",
            "runtimes",
            "selection",
            "configuration_sha256",
            "policy_sha256",
            "input_coverage",
            "inputs",
            "base_inventory",
            "outputs",
        },
    )
    if (
        type(data["schema"]) is not int
        or data["schema"] != 1
        or data["kind"] != "chainman.dependency-candidate"
        or data["runtime_kind"] not in ("source", "consumer")
        or data["input_coverage"] != "declared_inputs_only"
    ):
        raise ValueError("Unsupported candidate schema or coverage")
    uuid.UUID(ad.text(data["operation"], "Operation"))
    validate_identity(data["base"])
    runtimes = ad.table(data["runtimes"], "Runtimes")
    keys(runtimes, {"entry", "resolution", "verification"})
    for value in runtimes.values():
        validate_identity(value)
    for key in ("configuration_sha256", "policy_sha256"):
        hash_value(data[key])
    selection = ad.table(data["selection"], "Selection")
    keys(selection, {"at", "arguments", "adapters", "runtime"})
    from datetime import datetime

    if datetime.fromisoformat(
        ad.text(selection["at"], "Selection time")
    ).utcoffset() is None or selection["runtime"] not in (
        "not_applicable",
        "include",
        "exclude",
        "only",
    ):
        raise ValueError("Invalid selection time/runtime")
    ad.strings(selection["arguments"], "Arguments")
    ad.strings(selection["adapters"], "Adapters")
    total = 0
    for key in ("inputs", "base_inventory", "outputs"):
        rows = ad.array(data[key], key)
        if len(rows) > MAX_FILES:
            raise ValueError("Export inventory too large")
        previous = ""
        for raw in rows:
            row = ad.table(raw, "File entry")
            keys(row, {"path", "sha256", "mode", "size"})
            name = path_name(row["path"])
            if name <= previous:
                raise ValueError("Export inventory must be sorted and unique")
            previous = name
            size = row["size"]
            if type(size) is not int or not 0 <= size <= MAX_BLOB:
                raise ValueError("Invalid export size")
            if row["sha256"] is None:
                if row["mode"] is not None or size != 0:
                    raise ValueError("Invalid deletion entry")
                continue
            sha = hash_value(row["sha256"])
            if row["mode"] not in ("100644", "100755"):
                raise ValueError("Unsupported export file mode")
            if key == "outputs":
                blob = tc.contained(path, "blobs/" + sha)
                total += size
                if total > MAX_TOTAL or blob.stat().st_size != size:
                    raise ValueError("Export blob size mismatch or limit exceeded")
                if digest(tc.regular_input(path, "blobs/" + sha)) != sha:
                    raise ValueError("Export blob digest mismatch")
    return data


def check(path: Path) -> ad.Table:
    path = directory(path)
    data = read_json(path, "result.json")
    keys(
        data,
        {
            "schema",
            "kind",
            "operation",
            "outcome",
            "stage",
            "exit_code",
            "snapshot_sha256",
        },
    )
    if (
        type(data["schema"]) is not int
        or data["schema"] != 1
        or data["kind"] != "chainman.update-result"
        or ad.text(data["outcome"], "Outcome") not in RESULTS
        or ad.text(data["stage"], "Stage") not in STAGES
    ):
        raise ValueError("Unsupported update result")
    uuid.UUID(ad.text(data["operation"], "Operation"))
    code = data["exit_code"]
    if code is not None and (type(code) is not int or not 0 <= code <= 255):
        raise ValueError("Invalid exit code")
    snapshot = data["snapshot_sha256"]
    if snapshot is not None:
        if digest(manifest_bytes(path, "snapshot.json")) != hash_value(snapshot):
            raise ValueError("Snapshot digest mismatch")
        manifest = validate_snapshot(path)
        if manifest["operation"] != data["operation"]:
            raise ValueError("Snapshot operation mismatch")
    if (
        data["outcome"]
        in {"verified_success", "complete_no_change", "accepted_verification_failed"}
        and snapshot is None
    ):
        raise ValueError("Accepted result requires a snapshot")
    if data["outcome"] in {"verified_success", "complete_no_change"} and (
        code != 0 or data["stage"] != "complete"
    ):
        raise ValueError("Successful result requires completed verification")
    if data["outcome"] == "accepted_verification_failed" and (
        code in (None, 0) or data["stage"] != "verification"
    ):
        raise ValueError("Failed verification requires a nonzero verification result")
    if data["outcome"] in {"failure_before_acceptance", "unsupported"} and (
        snapshot is not None or code in (None, 0)
    ):
        raise ValueError("Unaccepted results cannot contain accepted snapshots")
    if snapshot is not None:
        changed = bool(manifest["outputs"])
        if data["outcome"] == "complete_no_change" and changed:
            raise ValueError("No-change result contains changes")
        if data["outcome"] == "verified_success" and not changed:
            raise ValueError("Changed result has an empty output inventory")
    return data


def main(args: list[str]) -> int:
    if len(args) != 1:
        raise ValueError("usage: candidate-check /path/to/export")
    print(json.dumps(check(Path(args[0])), sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError) as error:
        print(f"Chainman candidate: {error}", file=sys.stderr)
        raise SystemExit(1) from None
