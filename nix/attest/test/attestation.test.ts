import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";
import { Github, environmentIdentity, verifyRun } from "../src/github.js";
import { loadManifest, manifest, policy } from "../src/model.js";
import {
  buildType,
  verificationArguments,
  verifyBundle,
  verifyCertificate,
  verifyManifest,
} from "../src/provenance.js";
import {
  artifact,
  artifacts,
  bundle,
  bytes,
  certificate,
  configured,
  identity,
  jobs,
  repository,
  run,
  statement,
} from "./fixtures.js";
import { bundleArchive } from "../src/archive.js";
import { zip } from "./zip.js";

await test("strict explicit policy, manifest duplicate and traversal rejection", () => {
  assert.throws(() => policy({ ...configured, provider: "other" }));
  assert.throws(() => policy({ ...configured, unknown: true }));
  assert.throws(() =>
    policy({ ...configured, artifactName: "qualified-release-{runId}" }),
  );
  const valid = {
    version: 1,
    sourceCommit: identity.sourceCommit,
    artifacts: [artifact],
  };
  assert.deepEqual(manifest(valid), valid);
  for (const path of ["../x", "/x", "a/../x", "a//x", "a\\x", "./x"])
    assert.throws(() =>
      manifest({ ...valid, artifacts: [{ ...artifact, path }] }),
    );
  assert.throws(() => manifest({ ...valid, artifacts: [artifact, artifact] }));
  assert.throws(() =>
    manifest({
      ...valid,
      artifacts: [
        artifact,
        {
          ...artifact,
          subject: "other",
          path: `${artifact.path}.sigstore.json`,
        },
      ],
    }),
  );
});
await test("manifest reads reject symlink files, parent symlinks and changed bytes", async () => {
  const directory = await mkdtemp(join(tmpdir(), "chainman-attest-"));
  try {
    const path = join(directory, "manifest.json");
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        sourceCommit: identity.sourceCommit,
        artifacts: [artifact],
      }),
    );
    await writeFile(join(directory, artifact.path), bytes);
    assert.equal((await loadManifest(path)).artifacts.length, 1);
    await writeFile(join(directory, artifact.path), "changed");
    await assert.rejects(loadManifest(path), /digest mismatch/);
    await rm(join(directory, artifact.path));
    await symlink(path, join(directory, artifact.path));
    await assert.rejects(loadManifest(path), /without symlinks/);
    await mkdir(join(directory, "real"));
    await writeFile(join(directory, "real/package.tgz"), bytes);
    await symlink(join(directory, "real"), join(directory, "link"));
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        sourceCommit: identity.sourceCommit,
        artifacts: [{ ...artifact, path: "link/package.tgz" }],
      }),
    );
    await assert.rejects(loadManifest(path), /without symlinks/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
await test("qualification requires every job, same attempt/source, correct public run and named artifact", () => {
  assert.deepEqual(
    verifyRun(configured, run, jobs, artifacts, identity.runId),
    { ...identity, artifactId: "98765" },
  );
  for (const patch of [
    { run_attempt: 1 },
    { head_sha: "b".repeat(40) },
    { event: "pull_request" },
    { repository: { ...repository, private: true } },
    { head_repository: { ...repository, full_name: "fork/package" } },
    { path: ".github/workflows/other.yml" },
    { status: "in_progress" },
  ]) {
    assert.throws(() =>
      verifyRun(
        configured,
        { ...run, ...patch },
        jobs,
        artifacts,
        identity.runId,
      ),
    );
  }
  for (const patch of [
    { conclusion: "skipped" },
    { run_attempt: 1 },
    { head_sha: "b".repeat(40) },
    { run_id: 99 },
  ]) {
    assert.throws(() =>
      verifyRun(
        configured,
        run,
        jobs.map((job, index) => (index ? job : { ...job, ...patch })),
        artifacts,
        identity.runId,
      ),
    );
  }
  for (const entries of [
    [],
    [...artifacts, ...artifacts],
    artifacts.map((entry) => ({ ...entry, name: "qualified-release-12345-1" })),
    artifacts.map((entry) => ({
      ...entry,
      created_at: "2026-11-08T11:59:00Z",
    })),
  ])
    assert.throws(() =>
      verifyRun(configured, run, jobs, entries, identity.runId),
    );
});
await test("creation environment requires direct same-repo public hosted source and workflow", () => {
  const environment = {
    GITHUB_ACTIONS: "true",
    GITHUB_SERVER_URL: "https://github.com",
    GITHUB_REPOSITORY: configured.repository,
    GITHUB_REF: configured.ref,
    GITHUB_WORKFLOW_REF: `${configured.repository}/${configured.workflow}@${configured.ref}`,
    GITHUB_SHA: identity.sourceCommit,
    GITHUB_WORKFLOW_SHA: identity.sourceCommit,
    GITHUB_RUN_ID: identity.runId,
    GITHUB_RUN_ATTEMPT: identity.attempt,
    RUNNER_ENVIRONMENT: "github-hosted",
    GITHUB_EVENT_NAME: "push",
  };
  assert.deepEqual(environmentIdentity(configured, environment), identity);
  for (const patch of [
    { GITHUB_WORKFLOW_SHA: "b".repeat(40) },
    { GITHUB_EVENT_NAME: "pull_request" },
    { RUNNER_ENVIRONMENT: "self-hosted" },
    { GITHUB_REPOSITORY: "fork/package" },
    { GITHUB_JOB_WORKFLOW_REF: "other/workflow" },
    { GITHUB_SERVER_URL: "https://private.example" },
  ]) {
    assert.throws(() =>
      environmentIdentity(configured, { ...environment, ...patch }),
    );
  }
});
await test("creation permits only its own running signer after successful qualification", async () => {
  const signingJobs = jobs.map((job, index) =>
    index === 2
      ? {
          ...job,
          status: "in_progress",
          conclusion: null,
          runner_name: "hosted-runner",
        }
      : job,
  );
  const github = new Github(configured, async (_program, args) => ({
    status: 0,
    stderr: "",
    stdout: JSON.stringify(
      args.some((arg) => arg.includes("/jobs"))
        ? [{ jobs: signingJobs }]
        : { ...run, status: "in_progress" },
    ),
  }));
  await github.signingRun(identity, "hosted-runner");
  await assert.rejects(
    github.signingRun(identity, "other"),
    /preceding qualification/,
  );
  const skipped = new Github(configured, async (_program, args) => ({
    status: 0,
    stderr: "",
    stdout: JSON.stringify(
      args.some((arg) => arg.includes("/jobs"))
        ? [
            {
              jobs: signingJobs.map((job, index) =>
                index ? job : { ...job, conclusion: "skipped" },
              ),
            },
          ]
        : { ...run, status: "in_progress" },
    ),
  }));
  await assert.rejects(
    skipped.signingRun(identity, "hosted-runner"),
    /preceding qualification/,
  );
});
await test("generic SDK predicate shape and exact single PURL/SHA512 bundle and certificate identity", () => {
  assert.equal(buildType, "https://actions.github.io/buildtypes/workflow/v1");
  verifyBundle(bundle(), artifact, identity, configured);
  assert.throws(() =>
    verifyBundle(
      bundle(),
      { ...artifact, subject: "pkg:npm/wrong@1.0.0" },
      identity,
      configured,
    ),
  );
  assert.throws(() =>
    verifyBundle(
      bundle({
        ...statement(),
        subject: [
          { name: artifact.subject, digest: artifact.digest },
          { name: artifact.subject, digest: artifact.digest },
        ],
      }),
      artifact,
      identity,
      configured,
    ),
  );
  assert.throws(() =>
    verifyBundle(bundle(), artifact, { ...identity, attempt: "1" }, configured),
  );
  const verified = [{ verificationResult: { signature: { certificate } } }];
  verifyCertificate(verified, identity, configured);
  for (const patch of [
    { sourceRepositoryVisibilityAtSigning: "private" },
    { runnerEnvironment: "self-hosted" },
    { buildSignerDigest: "b".repeat(40) },
    {
      runInvocationURI: certificate.runInvocationURI.replace(
        "/attempts/2",
        "/attempts/1",
      ),
    },
  ])
    assert.throws(() =>
      verifyCertificate(
        [
          {
            verificationResult: {
              signature: { certificate: { ...certificate, ...patch } },
            },
          },
        ],
        identity,
        configured,
      ),
    );
  const args = verificationArguments(
    "/local/package.tgz",
    "/local/package.sigstore.json",
    identity,
    configured,
  );
  for (const [flag, value] of [
    ["--bundle", "/local/package.sigstore.json"],
    ["--source-ref", configured.ref],
    ["--signer-digest", identity.sourceCommit],
    ["--digest-alg", "sha512"],
  ])
    assert.equal(args[args.indexOf(flag ?? "") + 1], value);
  assert.ok(args.includes("--deny-self-hosted-runners"));
});
await test("verification uses local bytes and exact saved bundles, never downloaded archive contents", async () => {
  const directory = await mkdtemp(join(tmpdir(), "chainman-verify-"));
  try {
    const path = join(directory, "manifest.json");
    await writeFile(
      path,
      JSON.stringify({
        version: 1,
        sourceCommit: identity.sourceCommit,
        artifacts: [artifact],
      }),
    );
    await writeFile(join(directory, artifact.path), bytes);
    await writeFile(
      join(directory, `${artifact.path}.sigstore.json`),
      JSON.stringify(bundle()),
    );
    const calls: readonly string[][] = [];
    const github = new (class extends Github {
      override async archive(): Promise<Buffer> {
        return zip([
          {
            name: `${artifact.path}.sigstore.json`,
            bytes: Buffer.from(JSON.stringify(bundle())),
          },
          { name: "manifest.json", bytes: Buffer.from("untrusted garbage") },
        ]);
      }
    })(configured, async (_program, args, _interactive, env) => {
      (calls as string[][]).push([...args]);
      assert.equal(env?.["GH_HOST"], "github.com");
      const data =
        args[0] === "attestation"
          ? [{ verificationResult: { signature: { certificate } } }]
          : args.some((arg) => arg.includes("/jobs"))
            ? [{ jobs }]
            : args.some((arg) => arg.includes("/artifacts"))
              ? [{ artifacts }]
              : run;
      return { status: 0, stderr: "", stdout: JSON.stringify(data) };
    });
    const result = await verifyManifest(
      path,
      identity.runId,
      configured,
      github,
    );
    assert.ok(
      result.artifacts[0]?.bundlePath.endsWith(
        `/${artifact.path}.sigstore.json`,
      ),
    );
    assert.ok(!result.artifacts[0]?.bundlePath.startsWith(directory));
    assert.ok(
      calls.some((args) =>
        args.some((arg) => arg.includes("/attempts/2/jobs")),
      ),
    );
    assert.ok(!calls.some((args) => args.includes("download")));
    await result.dispose();
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
await test("bundle archive rejects traversal, symlinks, duplicates and ambiguous basenames; ignores downloaded manifest", async () => {
  const expected = "package.tgz.sigstore.json",
    bytes = Buffer.from("bundle");
  assert.deepEqual(
    await bundleArchive(
      zip([
        { name: expected, bytes },
        { name: "manifest.json", bytes: Buffer.from("not JSON") },
      ]),
      [expected],
    ),
    new Map([[expected, bytes]]),
  );
  for (const entries of [
    [{ name: "../escape", bytes }],
    [{ name: expected, bytes, mode: 0xa000 }],
    [
      { name: expected, bytes },
      { name: expected, bytes },
    ],
    [
      { name: `a/${expected}`, bytes },
      { name: `b/${expected}`, bytes },
    ],
  ])
    await assert.rejects(bundleArchive(zip(entries), [expected]));
  await assert.rejects(
    bundleArchive(zip([{ name: expected, bytes }]), [expected, expected]),
    /Ambiguous/,
  );
});
