import { policy } from "../src/model.js";
import { buildType, predicateType } from "../src/provenance.js";
import { sha512 } from "../src/common.js";

export const configured = policy({
  provider: "github-actions",
  repository: "example/package",
  workflow: ".github/workflows/qualification.yml",
  ref: "refs/heads/master",
  requiredJobs: ["Qualification", "Support (*)", "Sign bundles"],
  artifactName: "qualified-release-{runId}-{runAttempt}",
  tag: "next",
});
export const identity = {
  sourceCommit: "a".repeat(40),
  runId: "12345",
  attempt: "2",
};
export const bytes = Buffer.from("fixture artifact bytes");
export const artifact = {
  path: "package.tgz",
  subject: "pkg:npm/%40example/package@1.0.0-rc.0",
  digest: { sha512: sha512(bytes) },
};
export const repository = { full_name: configured.repository, private: false };
export const run = {
  id: 12345,
  run_attempt: 2,
  status: "completed",
  conclusion: "success",
  head_sha: identity.sourceCommit,
  head_branch: "master",
  event: "push",
  repository,
  head_repository: repository,
  path: configured.workflow,
  html_url: `https://github.com/${configured.repository}/actions/runs/${identity.runId}`,
  run_started_at: "2026-11-08T12:00:00Z",
  updated_at: "2026-11-08T12:30:00Z",
};
export const jobs = ["Qualification", "Support (node)", "Sign bundles"].map(
  (name) => ({
    name: `qualification / ${name}`,
    run_id: 12345,
    run_attempt: 2,
    head_sha: identity.sourceCommit,
    status: "completed",
    conclusion: "success",
  }),
);
export const artifacts = [
  {
    id: 98765,
    name: "qualified-release-12345-2",
    expired: false,
    created_at: "2026-11-08T12:20:00Z",
    workflow_run: {
      id: 12345,
      head_sha: identity.sourceCommit,
      head_branch: "master",
    },
  },
];
export const certificate = {
  runInvocationURI: `https://github.com/${configured.repository}/actions/runs/${identity.runId}/attempts/${identity.attempt}`,
  sourceRepositoryVisibilityAtSigning: "public",
  runnerEnvironment: "github-hosted",
  sourceRepositoryDigest: identity.sourceCommit,
  sourceRepositoryRef: configured.ref,
  buildSignerDigest: identity.sourceCommit,
  buildSignerURI: `https://github.com/${configured.repository}/${configured.workflow}@${configured.ref}`,
};
export function statement(subject = artifact): Record<string, unknown> {
  return {
    _type: "https://in-toto.io/Statement/v1",
    predicateType,
    subject: [{ name: subject.subject, digest: subject.digest }],
    predicate: {
      buildDefinition: {
        buildType,
        externalParameters: {
          workflow: {
            repository: `https://github.com/${configured.repository}`,
            path: configured.workflow,
            ref: configured.ref,
          },
        },
        internalParameters: {
          github: { event_name: "push", runner_environment: "github-hosted" },
        },
        resolvedDependencies: [
          {
            uri: `git+https://github.com/${configured.repository}@${configured.ref}`,
            digest: { gitCommit: identity.sourceCommit },
          },
        ],
      },
      runDetails: {
        builder: {
          id: `https://github.com/${configured.repository}/${configured.workflow}@${configured.ref}`,
        },
        metadata: {
          invocationId: `https://github.com/${configured.repository}/actions/runs/${identity.runId}/attempts/${identity.attempt}`,
        },
      },
    },
  };
}
export function bundle(payload = statement()): Record<string, unknown> {
  return {
    mediaType: "application/vnd.dev.sigstore.bundle.v0.3+json",
    dsseEnvelope: {
      payloadType: "application/vnd.in-toto+json",
      payload: Buffer.from(JSON.stringify(payload)).toString("base64"),
    },
  };
}
