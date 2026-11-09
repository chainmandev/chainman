import { command, object, parse, records, string } from "./common.js";
import type { Command } from "./common.js";
import type { Policy } from "./model.js";
import { spawn } from "node:child_process";

export interface Identity {
  readonly sourceCommit: string;
  readonly runId: string;
  readonly attempt: string;
}
export interface QualifiedIdentity extends Identity {
  readonly artifactId: string;
}
function numeric(value: unknown): string {
  const result = String(value);
  if (!/^[1-9]\d*$/.test(result))
    throw new Error("Expected a positive numeric GitHub identity");
  return result;
}
export function environmentIdentity(
  policy: Policy,
  env: NodeJS.ProcessEnv = process.env,
): Identity {
  const sourceCommit = env["GITHUB_SHA"] ?? "",
    runId = env["GITHUB_RUN_ID"] ?? "",
    attempt = env["GITHUB_RUN_ATTEMPT"] ?? "";
  if (
    env["GITHUB_ACTIONS"] !== "true" ||
    env["GITHUB_SERVER_URL"] !== "https://github.com" ||
    env["GITHUB_REPOSITORY"] !== policy.repository ||
    env["GITHUB_REF"] !== policy.ref ||
    env["GITHUB_WORKFLOW_REF"] !==
      `${policy.repository}/${policy.workflow}@${policy.ref}` ||
    (env["GITHUB_JOB_WORKFLOW_REF"] !== undefined &&
      env["GITHUB_JOB_WORKFLOW_REF"] !== env["GITHUB_WORKFLOW_REF"]) ||
    env["GITHUB_WORKFLOW_SHA"] !== sourceCommit ||
    env["RUNNER_ENVIRONMENT"] !== "github-hosted" ||
    !["push", "workflow_dispatch"].includes(env["GITHUB_EVENT_NAME"] ?? "") ||
    !/^[a-f0-9]{40}$/.test(sourceCommit)
  )
    throw new Error(
      "Attestation requires the configured direct public github.com workflow and hosted source",
    );
  numeric(runId);
  numeric(attempt);
  return { sourceCommit, runId, attempt };
}
function matchesJob(name: string, required: string): boolean {
  const escaped = required
    .split("*")
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`(?:^| / )${escaped}$`).test(name);
}
export function verifyRun(
  policy: Policy,
  value: unknown,
  jobsValue: unknown,
  artifactsValue: unknown,
  runId: string,
): QualifiedIdentity {
  numeric(runId);
  const run = object(value),
    repository = object(run["repository"]),
    head = object(run["head_repository"]);
  const sourceCommit = string(run["head_sha"]),
    attempt = numeric(run["run_attempt"]);
  if (
    numeric(run["id"]) !== runId ||
    run["status"] !== "completed" ||
    run["conclusion"] !== "success" ||
    run["head_branch"] !== policy.ref.slice("refs/heads/".length) ||
    !["push", "workflow_dispatch"].includes(string(run["event"])) ||
    run["path"] !== policy.workflow ||
    run["html_url"] !==
      `https://github.com/${policy.repository}/actions/runs/${runId}` ||
    repository["full_name"] !== policy.repository ||
    repository["private"] !== false ||
    head["full_name"] !== policy.repository ||
    head["private"] !== false ||
    !/^[a-f0-9]{40}$/.test(sourceCommit)
  )
    throw new Error(
      "Requested qualification run does not satisfy public repository/workflow/source policy",
    );
  const jobs = records(jobsValue);
  if (
    !jobs.length ||
    jobs.some(
      (job) =>
        job["status"] !== "completed" ||
        job["conclusion"] !== "success" ||
        numeric(job["run_id"]) !== runId ||
        numeric(job["run_attempt"]) !== attempt ||
        job["head_sha"] !== sourceCommit,
    )
  )
    throw new Error(
      "Qualification jobs must all succeed for the same source and attempt",
    );
  const names = jobs.map((job) => string(job["name"]));
  for (const required of policy.requiredJobs)
    if (!names.some((name) => matchesJob(name, required)))
      throw new Error(`Missing required qualification job: ${required}`);
  const expectedName = policy.artifactName
    .replaceAll("{runId}", runId)
    .replaceAll("{runAttempt}", attempt);
  const artifacts = records(artifactsValue).filter(
    (artifact) => artifact["name"] === expectedName,
  );
  if (artifacts.length !== 1)
    throw new Error(
      "Expected exactly one qualified bundle artifact for this run attempt",
    );
  const artifact = artifacts[0];
  if (!artifact) throw new Error("Missing qualified bundle artifact");
  const artifactRun = object(artifact["workflow_run"]);
  const created = Date.parse(string(artifact["created_at"])),
    started = Date.parse(string(run["run_started_at"])),
    finished = Date.parse(string(run["updated_at"]));
  if (
    artifact["expired"] !== false ||
    numeric(artifactRun["id"]) !== runId ||
    artifactRun["head_sha"] !== sourceCommit ||
    artifactRun["head_branch"] !== run["head_branch"] ||
    !Number.isFinite(created) ||
    !Number.isFinite(started) ||
    !Number.isFinite(finished) ||
    created < started ||
    created > finished
  )
    throw new Error(
      "Qualified artifact does not belong to the requested source and run attempt",
    );
  return { sourceCommit, runId, attempt, artifactId: numeric(artifact["id"]) };
}
export class Github {
  constructor(
    readonly policy: Policy,
    readonly invoke: Command = command,
    readonly executable = process.env["CHAINMAN_ATTEST_GH"] ?? "gh",
  ) {}
  async json(args: readonly string[]): Promise<unknown> {
    const pinnedArgs =
      args[0] === "api"
        ? ["api", "--hostname", "github.com", ...args.slice(1)]
        : [...args];
    const result = await this.invoke(this.executable, pinnedArgs, false, {
      ...process.env,
      GH_HOST: "github.com",
    });
    if (result.status !== 0)
      throw new Error(`GitHub verification command failed (${result.status})`);
    return parse(Buffer.from(result.stdout));
  }
  async run(runId: string): Promise<QualifiedIdentity> {
    numeric(runId);
    const endpoint = `repos/${this.policy.repository}/actions/runs/${runId}`;
    const run = await this.json(["api", endpoint]);
    const attempt = numeric(object(run)["run_attempt"]);
    const jobsPages = records(
      await this.json([
        "api",
        `${endpoint}/attempts/${attempt}/jobs?per_page=100`,
        "--paginate",
        "--slurp",
      ]),
    );
    const artifactPages = records(
      await this.json([
        "api",
        `${endpoint}/artifacts?per_page=100`,
        "--paginate",
        "--slurp",
      ]),
    );
    return verifyRun(
      this.policy,
      run,
      jobsPages.flatMap((page) => records(page["jobs"])),
      artifactPages.flatMap((page) => records(page["artifacts"])),
      runId,
    );
  }
  async archive(artifactId: string): Promise<Buffer> {
    numeric(artifactId);
    return await new Promise((done, reject) => {
      const child = spawn(
        this.executable,
        [
          "api",
          "--hostname",
          "github.com",
          `repos/${this.policy.repository}/actions/artifacts/${artifactId}/zip`,
        ],
        {
          stdio: ["ignore", "pipe", "pipe"],
          env: { ...process.env, GH_HOST: "github.com" },
        },
      );
      const chunks: Buffer[] = [];
      let length = 0;
      child.stdout.on("data", (chunk: Buffer) => {
        length += chunk.length;
        if (length > 64 * 1024 * 1024) {
          child.kill();
          reject(new Error("Qualified bundle archive exceeds size limit"));
        } else chunks.push(chunk);
      });
      // Do not print diagnostics containing credential-bearing signed URLs.
      child.stderr.resume();
      child.on("error", reject);
      child.on("close", (status) => {
        if (status === 0) done(Buffer.concat(chunks));
        else reject(new Error("Qualified bundle archive download failed"));
      });
    });
  }
  async signingRun(identity: Identity, runnerName: string): Promise<void> {
    const endpoint = `repos/${this.policy.repository}/actions/runs/${identity.runId}`;
    const run = object(await this.json(["api", endpoint]));
    const repository = object(run["repository"]),
      head = object(run["head_repository"]);
    if (
      numeric(run["id"]) !== identity.runId ||
      run["html_url"] !==
        `https://github.com/${this.policy.repository}/actions/runs/${identity.runId}` ||
      numeric(run["run_attempt"]) !== identity.attempt ||
      run["head_sha"] !== identity.sourceCommit ||
      run["path"] !== this.policy.workflow ||
      run["status"] !== "in_progress" ||
      !["push", "workflow_dispatch"].includes(string(run["event"])) ||
      run["head_branch"] !== this.policy.ref.slice("refs/heads/".length) ||
      repository["full_name"] !== this.policy.repository ||
      repository["private"] !== false ||
      head["full_name"] !== this.policy.repository ||
      head["private"] !== false
    )
      throw new Error(
        "Signing run does not satisfy direct public source policy",
      );
    const pages = records(
      await this.json([
        "api",
        `${endpoint}/attempts/${identity.attempt}/jobs?per_page=100`,
        "--paginate",
        "--slurp",
      ]),
    );
    const jobs = pages.flatMap((page) => records(page["jobs"])),
      running = jobs.filter((job) => job["status"] === "in_progress");
    if (
      !runnerName ||
      running.length !== 1 ||
      running[0]?.["runner_name"] !== runnerName ||
      jobs.some(
        (job) =>
          numeric(job["run_id"]) !== identity.runId ||
          numeric(job["run_attempt"]) !== identity.attempt ||
          job["head_sha"] !== identity.sourceCommit ||
          (job !== running[0] &&
            (job["status"] !== "completed" || job["conclusion"] !== "success")),
      )
    )
      throw new Error(
        "Signing requires every preceding qualification job to succeed in this attempt",
      );
    for (const required of this.policy.requiredJobs)
      if (!jobs.some((job) => matchesJob(string(job["name"]), required)))
        throw new Error(`Missing required qualification job: ${required}`);
  }
}
