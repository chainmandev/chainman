"""Enter the optional, pinned attestation runtime with an explicit provider policy."""

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import re
import sys
import tempfile

from adapter_data import Table, strings, table, text
import chainman
import toolchain as tc


def policy(cfg: Mapping[str, object]) -> Table:
    if "attest" not in cfg:
        raise ValueError("Attestation requires an explicit [attest] provider policy")
    declared = table(cfg["attest"], "Attestation policy")
    fields = {
        "provider",
        "repository",
        "workflow",
        "ref",
        "required_jobs",
        "artifact_name",
        "tag",
    }
    unknown = set(declared) - fields
    if unknown:
        raise ValueError(
            "Unknown attestation policy fields: " + ", ".join(sorted(unknown))
        )
    if declared.get("provider") != "github-actions":
        raise ValueError(
            "Unsupported attestation provider; initially only github-actions is supported"
        )
    required = fields - {"tag"}
    if required - declared.keys():
        raise ValueError(
            "Attestation policy requires: "
            + ", ".join(sorted(required - declared.keys()))
        )
    result: Table = {}
    for name in required - {"required_jobs"}:
        value = text(declared[name], "Attestation " + name)
        if not value.strip() or any(c in value for c in "\0\r\n"):
            raise ValueError("Attestation policy strings must be nonempty single lines")
        result[name] = value
    jobs = strings(declared["required_jobs"], "Attestation required_jobs")
    if not jobs or len(jobs) != len(set(jobs)) or any(not job.strip() for job in jobs):
        raise ValueError(
            "Attestation required_jobs must contain distinct nonempty job names"
        )
    result["requiredJobs"] = jobs
    result["artifactName"] = result.pop("artifact_name")
    if "tag" in declared and declared["tag"] != "next":
        raise ValueError("Initially npm publication supports only the next tag")
    if "tag" in declared:
        result["tag"] = "next"
    return result


def arguments(action: str, rest: Sequence[str]) -> list[str]:
    argv = list(rest)
    if action == "attest":
        if not argv or argv[0] not in {"create", "verify"}:
            raise ValueError(
                "Use attest create MANIFEST or attest verify MANIFEST --run ID"
            )
        operation = argv.pop(0)
        prefix = ["attest", operation]
    else:
        operation = "publish"
        prefix = ["npm-publish"]
    allowed_lengths = {1, 3} if operation == "create" else {3}
    if len(argv) not in allowed_lengths or (
        len(argv) == 3
        and (argv[1] != "--run" or not re.fullmatch(r"[1-9][0-9]*", argv[2]))
    ):
        raise ValueError(
            "Use attest create MANIFEST [--run ID], attest verify MANIFEST --run ID, or npm-publish MANIFEST --run ID"
        )
    return [*prefix, *argv]


def run(root: Path, action: str, rest: Sequence[str]) -> int:
    argv = arguments(action, rest)
    cfg = chainman.configuration(root)
    if "attest" in table(cfg.get("profiles", {}), "Profiles"):
        raise ValueError(
            "The attest profile is reserved for Chainman's pinned helper runtime"
        )
    selected = policy(cfg)
    if action == "npm-publish" and selected.get("tag") != "next":
        raise ValueError("npm-publish requires an explicit attest.tag = 'next' policy")
    manifest_index = 2 if action == "attest" else 1
    relative = argv[manifest_index]
    tc.regular_input(root, relative)
    argv[manifest_index] = str(tc.contained(root, relative))
    # This private file only transports already validated configuration to Node.
    # It is created inside the current execution mode, including container mode.
    with tempfile.TemporaryDirectory(prefix="chainman-attest-policy-") as temporary:
        path = Path(temporary) / "policy.json"
        path.write_text(json.dumps(selected) + "\n")
        path.chmod(0o600)
        env = tc.environment(root)
        # Reenter the immutable shell so project environment/PATH overrides
        # cannot impersonate its private helper or its pinned provider tools.
        env.pop("CHAINMAN_ACTIVE_PROFILE", None)
        return chainman.execute(
            root,
            "attest",
            [
                sys.executable,
                "-c",
                'import os,sys; executable=os.environ["CHAINMAN_ATTEST_BIN"]; os.execv(executable,[executable,*sys.argv[1:]])',
                *argv,
                "--policy",
                str(path),
            ],
            env=env,
            check=False,
        ).returncode
