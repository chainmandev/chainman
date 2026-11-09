"""Exact public GitHub workflow, job, and artifact qualification checks."""

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from attestation_common import Command, command, object_value, parse, records, string
from attestation_model import Policy
import toolchain as tc


@dataclass(frozen=True)
class Identity:
    source_commit: str
    run_id: str
    attempt: str


@dataclass(frozen=True)
class QualifiedIdentity(Identity):
    artifact_id: str


def numeric(value: object) -> str:
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", result):
        raise ValueError("Expected a positive numeric GitHub identity")
    return result


def environment_identity(
    policy: Policy, env: Mapping[str, str] | None = None
) -> Identity:
    environment = os.environ if env is None else env
    source_commit = environment.get("GITHUB_SHA", "")
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    workflow_ref = f"{policy.repository}/{policy.workflow}@{policy.ref}"
    if (
        environment.get("GITHUB_ACTIONS") != "true"
        or environment.get("GITHUB_SERVER_URL") != "https://github.com"
        or environment.get("GITHUB_REPOSITORY") != policy.repository
        or environment.get("GITHUB_REF") != policy.ref
        or environment.get("GITHUB_WORKFLOW_REF") != workflow_ref
        or environment.get("GITHUB_JOB_WORKFLOW_REF", workflow_ref) != workflow_ref
        or environment.get("GITHUB_WORKFLOW_SHA") != source_commit
        or environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or environment.get("GITHUB_EVENT_NAME") not in ("push", "workflow_dispatch")
        or not re.fullmatch(r"[a-f0-9]{40}", source_commit)
    ):
        raise ValueError(
            "Attestation requires the configured direct public github.com workflow and hosted source"
        )
    return Identity(source_commit, numeric(run_id), numeric(attempt))


def matches_job(name: str, required: str) -> bool:
    escaped = ".*".join(re.escape(part) for part in required.split("*"))
    return re.search(r"(?:^| / )" + escaped + r"$", name) is not None


def timestamp(value: object) -> datetime:
    try:
        result = datetime.fromisoformat(string(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Missing timestamp timezone")
        return result
    except ValueError as error:
        raise ValueError("Invalid qualification timestamp") from error


def verify_run(
    policy: Policy,
    value: object,
    jobs_value: object,
    artifacts_value: object,
    run_id: str,
) -> QualifiedIdentity:
    numeric(run_id)
    run = object_value(value)
    repository = object_value(run.get("repository"))
    head = object_value(run.get("head_repository"))
    source_commit = string(run.get("head_sha"))
    attempt = numeric(run.get("run_attempt"))
    if (
        numeric(run.get("id")) != run_id
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_branch") != policy.ref.removeprefix("refs/heads/")
        or string(run.get("event")) not in ("push", "workflow_dispatch")
        or run.get("path") != policy.workflow
        or run.get("html_url")
        != f"https://github.com/{policy.repository}/actions/runs/{run_id}"
        or repository.get("full_name") != policy.repository
        or repository.get("private") is not False
        or head.get("full_name") != policy.repository
        or head.get("private") is not False
        or not re.fullmatch(r"[a-f0-9]{40}", source_commit)
    ):
        raise ValueError(
            "Requested qualification run does not satisfy public repository/workflow/source policy"
        )
    jobs = records(jobs_value)
    if not jobs or any(
        job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or numeric(job.get("run_id")) != run_id
        or numeric(job.get("run_attempt")) != attempt
        or job.get("head_sha") != source_commit
        for job in jobs
    ):
        raise ValueError(
            "Qualification jobs must all succeed for the same source and attempt"
        )
    names = [string(job.get("name")) for job in jobs]
    for required in policy.required_jobs:
        if not any(matches_job(name, required) for name in names):
            raise ValueError(f"Missing required qualification job: {required}")
    expected_name = policy.artifact_name.replace("{runId}", run_id).replace(
        "{runAttempt}", attempt
    )
    artifacts = [
        item for item in records(artifacts_value) if item.get("name") == expected_name
    ]
    if len(artifacts) != 1:
        raise ValueError(
            "Expected exactly one qualified bundle artifact for this run attempt"
        )
    artifact = artifacts[0]
    artifact_run = object_value(artifact.get("workflow_run"))
    created = timestamp(artifact.get("created_at"))
    started = timestamp(run.get("run_started_at"))
    finished = timestamp(run.get("updated_at"))
    if (
        artifact.get("expired") is not False
        or numeric(artifact_run.get("id")) != run_id
        or artifact_run.get("head_sha") != source_commit
        or artifact_run.get("head_branch") != run.get("head_branch")
        or created < started
        or created > finished
    ):
        raise ValueError(
            "Qualified artifact does not belong to the requested source and run attempt"
        )
    return QualifiedIdentity(
        source_commit, run_id, attempt, numeric(artifact.get("id"))
    )


class Github:
    def __init__(
        self, policy: Policy, invoke: Command = command, executable: str | None = None
    ) -> None:
        self.policy = policy
        self.invoke = invoke
        self.executable = (
            os.environ.get("CHAINMAN_ATTEST_GH", "gh")
            if executable is None
            else executable
        )

    def json(self, args: Sequence[str]) -> object:
        pinned_args = (
            ["api", "--hostname", "github.com", *args[1:]]
            if args and args[0] == "api"
            else list(args)
        )
        result = self.invoke(
            self.executable,
            pinned_args,
            False,
            {**os.environ, "GH_HOST": "github.com"},
        )
        if result.status != 0:
            raise ValueError(f"GitHub verification command failed ({result.status})")
        return parse(result.stdout)

    def run(self, run_id: str) -> QualifiedIdentity:
        numeric(run_id)
        endpoint = f"repos/{self.policy.repository}/actions/runs/{run_id}"
        run = self.json(["api", endpoint])
        attempt = numeric(object_value(run).get("run_attempt"))
        jobs_pages = records(
            self.json(
                [
                    "api",
                    f"{endpoint}/attempts/{attempt}/jobs?per_page=100",
                    "--paginate",
                    "--slurp",
                ]
            )
        )
        artifact_pages = records(
            self.json(
                ["api", f"{endpoint}/artifacts?per_page=100", "--paginate", "--slurp"]
            )
        )
        return verify_run(
            self.policy,
            run,
            [job for page in jobs_pages for job in records(page.get("jobs"))],
            [
                item
                for page in artifact_pages
                for item in records(page.get("artifacts"))
            ],
            run_id,
        )

    def archive(self, artifact_id: str) -> bytes:
        numeric(artifact_id)
        options: tc.ProcessOptions = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            # Diagnostics can contain credential-bearing signed archive URLs.
            "stderr": subprocess.DEVNULL,
            "env": {**os.environ, "GH_HOST": "github.com"},
        }
        with subprocess.Popen(
            [
                self.executable,
                "api",
                "--hostname",
                "github.com",
                f"repos/{self.policy.repository}/actions/artifacts/{artifact_id}/zip",
            ],
            **tc.managed_options(options),
        ) as process:
            if process.stdout is None:
                raise ValueError("Missing qualified archive download stream")
            chunks: list[bytes] = []
            length = 0
            while chunk := process.stdout.read(1024 * 1024):
                length += len(chunk)
                if length > 64 * 1024 * 1024:
                    process.kill()
                    raise ValueError("Qualified bundle archive exceeds size limit")
                chunks.append(chunk)
            if process.wait() != 0:
                raise ValueError("Qualified bundle archive download failed")
            return b"".join(chunks)

    def signing_run(self, identity: Identity, runner_name: str) -> None:
        endpoint = f"repos/{self.policy.repository}/actions/runs/{identity.run_id}"
        run = object_value(self.json(["api", endpoint]))
        repository = object_value(run.get("repository"))
        head = object_value(run.get("head_repository"))
        if (
            numeric(run.get("id")) != identity.run_id
            or run.get("html_url")
            != f"https://github.com/{self.policy.repository}/actions/runs/{identity.run_id}"
            or numeric(run.get("run_attempt")) != identity.attempt
            or run.get("head_sha") != identity.source_commit
            or run.get("path") != self.policy.workflow
            or run.get("status") != "in_progress"
            or string(run.get("event")) not in ("push", "workflow_dispatch")
            or run.get("head_branch") != self.policy.ref.removeprefix("refs/heads/")
            or repository.get("full_name") != self.policy.repository
            or repository.get("private") is not False
            or head.get("full_name") != self.policy.repository
            or head.get("private") is not False
        ):
            raise ValueError("Signing run does not satisfy direct public source policy")
        pages = records(
            self.json(
                [
                    "api",
                    f"{endpoint}/attempts/{identity.attempt}/jobs?per_page=100",
                    "--paginate",
                    "--slurp",
                ]
            )
        )
        jobs = [job for page in pages for job in records(page.get("jobs"))]
        running = [job for job in jobs if job.get("status") == "in_progress"]
        if (
            not runner_name
            or len(running) != 1
            or running[0].get("runner_name") != runner_name
            or any(
                numeric(job.get("run_id")) != identity.run_id
                or numeric(job.get("run_attempt")) != identity.attempt
                or job.get("head_sha") != identity.source_commit
                or (
                    job is not running[0]
                    and (
                        job.get("status") != "completed"
                        or job.get("conclusion") != "success"
                    )
                )
                for job in jobs
            )
        ):
            raise ValueError(
                "Signing requires every preceding qualification job to succeed in this attempt"
            )
        for required in self.policy.required_jobs:
            if not any(matches_job(string(job.get("name")), required) for job in jobs):
                raise ValueError(f"Missing required qualification job: {required}")
