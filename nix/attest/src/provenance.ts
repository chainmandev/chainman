import { attestProvenance } from "@actions/attest";
import { constants } from "node:fs";
import { mkdtemp, open, readFile, rm, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { object, parse, records, string } from "./common.js";
import { Github, environmentIdentity } from "./github.js";
import type { Identity } from "./github.js";
import { containedFile, loadManifest } from "./model.js";
import type { Artifact, LoadedManifest, Policy } from "./model.js";
import { bundleArchive } from "./archive.js";

export const predicateType = "https://slsa.dev/provenance/v1";
export const buildType = "https://actions.github.io/buildtypes/workflow/v1";
export function verifyBundle(
  bundle: unknown,
  artifact: Artifact,
  identity: Identity,
  policy: Policy,
): void {
  const envelope = object(object(bundle)["dsseEnvelope"]);
  if (envelope["payloadType"] !== "application/vnd.in-toto+json")
    throw new Error("Unexpected attestation payload type");
  const statement = object(
      parse(Buffer.from(string(envelope["payload"]), "base64")),
    ),
    subjects = records(statement["subject"]);
  if (
    statement["_type"] !== "https://in-toto.io/Statement/v1" ||
    statement["predicateType"] !== predicateType ||
    subjects.length !== 1 ||
    subjects[0]?.["name"] !== artifact.subject ||
    object(subjects[0]?.["digest"])["sha512"] !== artifact.digest.sha512 ||
    Object.keys(object(subjects[0]?.["digest"])).length !== 1
  )
    throw new Error(
      "Attestation must identify exactly the selected subject and SHA-512",
    );
  const predicate = object(statement["predicate"]),
    definition = object(predicate["buildDefinition"]);
  const workflow = object(object(definition["externalParameters"])["workflow"]),
    dependencies = records(definition["resolvedDependencies"]);
  const internal = object(object(definition["internalParameters"])["github"]),
    details = object(predicate["runDetails"]);
  if (
    definition["buildType"] !== buildType ||
    workflow["repository"] !== `https://github.com/${policy.repository}` ||
    workflow["path"] !== policy.workflow ||
    workflow["ref"] !== policy.ref ||
    !["push", "workflow_dispatch"].includes(string(internal["event_name"])) ||
    internal["runner_environment"] !== "github-hosted" ||
    dependencies.length !== 1 ||
    dependencies[0]?.["uri"] !==
      `git+https://github.com/${policy.repository}@${policy.ref}` ||
    object(dependencies[0]?.["digest"])["gitCommit"] !==
      identity.sourceCommit ||
    object(details["builder"])["id"] !==
      `https://github.com/${policy.repository}/${policy.workflow}@${policy.ref}` ||
    object(details["metadata"])["invocationId"] !==
      `https://github.com/${policy.repository}/actions/runs/${identity.runId}/attempts/${identity.attempt}`
  )
    throw new Error(
      "Attestation source, direct workflow, hosted runner or run attempt mismatch",
    );
}
export function verificationArguments(
  artifactPath: string,
  bundlePath: string,
  identity: Identity,
  policy: Policy,
): string[] {
  return [
    "attestation",
    "verify",
    artifactPath,
    "--repo",
    policy.repository,
    "--bundle",
    bundlePath,
    "--digest-alg",
    "sha512",
    "--signer-workflow",
    `${policy.repository}/${policy.workflow}`,
    "--signer-digest",
    identity.sourceCommit,
    "--source-digest",
    identity.sourceCommit,
    "--source-ref",
    policy.ref,
    "--deny-self-hosted-runners",
    "--cert-oidc-issuer",
    "https://token.actions.githubusercontent.com",
    "--predicate-type",
    predicateType,
    "--format",
    "json",
  ];
}
export function verifyCertificate(
  value: unknown,
  identity: Identity,
  policy: Policy,
): void {
  const results = records(value);
  if (results.length !== 1)
    throw new Error("Expected exactly one verified saved bundle");
  const certificate = object(
    object(object(results[0]?.["verificationResult"])["signature"])[
      "certificate"
    ],
  );
  if (
    certificate["runInvocationURI"] !==
      `https://github.com/${policy.repository}/actions/runs/${identity.runId}/attempts/${identity.attempt}` ||
    certificate["sourceRepositoryVisibilityAtSigning"] !== "public" ||
    certificate["runnerEnvironment"] !== "github-hosted" ||
    certificate["sourceRepositoryDigest"] !== identity.sourceCommit ||
    certificate["sourceRepositoryRef"] !== policy.ref ||
    certificate["buildSignerDigest"] !== identity.sourceCommit ||
    certificate["buildSignerURI"] !==
      `https://github.com/${policy.repository}/${policy.workflow}@${policy.ref}`
  )
    throw new Error(
      "Verified certificate does not identify the requested public hosted direct signing workflow/source/run attempt",
    );
}
export interface VerifiedArtifact {
  readonly artifact: Artifact;
  readonly bytes: Buffer;
  readonly path: string;
  readonly bundle: unknown;
  readonly bundleBytes: Buffer;
  readonly bundlePath: string;
}
export interface VerifiedManifest {
  readonly loaded: LoadedManifest;
  readonly identity: Identity;
  readonly artifacts: readonly VerifiedArtifact[];
  dispose(): Promise<void>;
}
export async function verifyManifest(
  path: string,
  runId: string,
  policy: Policy,
  github = new Github(policy),
): Promise<VerifiedManifest> {
  const loaded = await loadManifest(path),
    identity = await github.run(runId);
  if (loaded.manifest.sourceCommit !== identity.sourceCommit)
    throw new Error("Local manifest differs from qualified source commit");
  const names = loaded.artifacts.map((selected) =>
    basename(
      selected.artifact.attestation ??
        `${selected.artifact.path}.sigstore.json`,
    ),
  );
  const bundles = await bundleArchive(
    await github.archive(identity.artifactId),
    names,
  );
  const directory = await mkdtemp(join(tmpdir(), "chainman-attest-"));
  const dispose = async (): Promise<void> => {
    await rm(directory, { recursive: true, force: true });
  };
  const artifacts: VerifiedArtifact[] = [];
  try {
    for (const selected of loaded.artifacts) {
      const relative =
        selected.artifact.attestation ??
        `${selected.artifact.path}.sigstore.json`;
      const bundleBytes = bundles.get(basename(relative));
      if (!bundleBytes) throw new Error("Missing selected signed bundle");
      const bundle = parse(bundleBytes),
        bundlePath = join(directory, basename(relative));
      await writeFile(bundlePath, bundleBytes, { flag: "wx", mode: 0o400 });
      verifyBundle(bundle, selected.artifact, identity, policy);
      verifyCertificate(
        await github.json(
          verificationArguments(selected.path, bundlePath, identity, policy),
        ),
        identity,
        policy,
      );
      // Detect replacement while the verifier ran; publication snapshots these bytes.
      if (
        !(await containedFile(loaded.root, selected.artifact.path)).equals(
          selected.bytes,
        ) ||
        !(await readFile(bundlePath)).equals(bundleBytes)
      )
        throw new Error("Artifact or saved bundle changed during verification");
      artifacts.push({ ...selected, bundle, bundleBytes, bundlePath });
    }
  } catch (error) {
    await dispose();
    throw error;
  }
  return { loaded, identity, artifacts, dispose };
}
export async function createAttestations(
  path: string,
  policy: Policy,
  requestedRunId?: string,
): Promise<void> {
  const identity = environmentIdentity(policy),
    loaded = await loadManifest(path);
  if (requestedRunId !== undefined && requestedRunId !== identity.runId)
    throw new Error("Requested run differs from the signing environment");
  if (loaded.manifest.sourceCommit !== identity.sourceCommit)
    throw new Error("Manifest source commit differs from signing workflow");
  const event = process.env["GITHUB_EVENT_PATH"];
  if (
    !event ||
    object(object(parse(await readFile(event)))["repository"])["private"] !==
      false
  )
    throw new Error("Signing requires a public repository event");
  const token = process.env["GITHUB_TOKEN"] ?? process.env["GH_TOKEN"];
  if (!token) throw new Error("Signing requires the workflow token");
  await new Github(policy).signingRun(
    identity,
    process.env["RUNNER_NAME"] ?? "",
  );
  const artifacts: Artifact[] = [];
  for (const selected of loaded.artifacts) {
    const relative =
      selected.artifact.attestation ??
      `${selected.artifact.path}.sigstore.json`;
    // Only create next to the existing tarball; custom destination parents are checked without following links.
    if (dirname(relative) !== dirname(selected.artifact.path))
      throw new Error("Saved attestation must be beside its artifact");
    const destination = join(loaded.root, relative),
      file = await open(
        destination,
        constants.O_WRONLY |
          constants.O_CREAT |
          constants.O_EXCL |
          constants.O_NOFOLLOW,
        0o600,
      );
    let complete = false;
    try {
      const result = await attestProvenance({
        subjects: [
          { name: selected.artifact.subject, digest: selected.artifact.digest },
        ],
        token,
        sigstore: "public-good",
      });
      verifyBundle(result.bundle, selected.artifact, identity, policy);
      if (
        !(await containedFile(loaded.root, selected.artifact.path)).equals(
          selected.bytes,
        )
      )
        throw new Error("Artifact changed during attestation signing");
      await file.writeFile(`${JSON.stringify(result.bundle, null, 2)}\n`);
      complete = true;
    } finally {
      await file.close();
      if (!complete) await rm(destination, { force: true });
    }
    artifacts.push({ ...selected.artifact, attestation: relative });
  }
  for (const selected of loaded.artifacts)
    if (
      !(await containedFile(loaded.root, selected.artifact.path)).equals(
        selected.bytes,
      )
    )
      throw new Error("Artifact changed before saving the signed manifest");
  const file = await open(
    loaded.path,
    constants.O_WRONLY | constants.O_TRUNC | constants.O_NOFOLLOW,
  );
  try {
    await file.writeFile(
      `${JSON.stringify({ ...loaded.manifest, artifacts }, null, 2)}\n`,
    );
  } finally {
    await file.close();
  }
}
