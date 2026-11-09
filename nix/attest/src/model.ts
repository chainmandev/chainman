import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";
import { keys, object, parse, sha512, string } from "./common.js";

export interface Policy {
  readonly provider: "github-actions";
  readonly repository: string;
  readonly workflow: string;
  readonly ref: string;
  readonly requiredJobs: readonly string[];
  readonly artifactName: string;
  readonly tag?: "next";
}
export function policy(value: unknown): Policy {
  const data = object(value);
  keys(data, [
    "provider",
    "repository",
    "workflow",
    "ref",
    "requiredJobs",
    "artifactName",
    "tag",
  ]);
  if (
    data["provider"] !== "github-actions" ||
    (data["tag"] !== undefined && data["tag"] !== "next")
  )
    throw new Error(
      "Expected github-actions policy and an optional next publication tag",
    );
  const repository = string(data["repository"]),
    workflow = string(data["workflow"]),
    ref = string(data["ref"]);
  if (
    !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) ||
    !/^\.github\/workflows\/[A-Za-z0-9_.-]+\.ya?ml$/.test(workflow) ||
    !/^refs\/heads\/[A-Za-z0-9_./-]+$/.test(ref) ||
    ref.includes("..") ||
    ref.endsWith("/")
  )
    throw new Error("Invalid repository, direct workflow or branch ref policy");
  const jobs = data["requiredJobs"];
  if (!Array.isArray(jobs) || !jobs.length)
    throw new Error("Required qualification jobs must be explicit");
  const requiredJobs = jobs.map((job: unknown) => string(job));
  if (new Set(requiredJobs).size !== requiredJobs.length)
    throw new Error("Duplicate required qualification job");
  const artifactName = string(data["artifactName"]);
  if (
    !artifactName.includes("{runId}") ||
    !artifactName.includes("{runAttempt}") ||
    !/^[A-Za-z0-9_.{}-]+$/.test(artifactName) ||
    /[{}]/.test(
      artifactName.replaceAll("{runId}", "").replaceAll("{runAttempt}", ""),
    )
  )
    throw new Error(
      "Artifact name must contain runId and runAttempt placeholders",
    );
  return {
    provider: "github-actions",
    repository,
    workflow,
    ref,
    requiredJobs,
    artifactName,
    ...(data["tag"] === "next" ? { tag: "next" as const } : {}),
  };
}
export interface Artifact {
  readonly path: string;
  readonly subject: string;
  readonly digest: { readonly sha512: string };
  readonly attestation?: string;
}
export interface Manifest {
  readonly version: 1;
  readonly sourceCommit: string;
  readonly artifacts: readonly Artifact[];
}
export function relativePath(value: unknown): string {
  const path = string(value);
  if (
    isAbsolute(path) ||
    /[\\\x00-\x1f\x7f]/.test(path) ||
    path.split("/").some((part) => !part || part === "." || part === "..")
  )
    throw new Error(
      "Expected a contained manifest-relative path without traversal",
    );
  return path;
}
export function manifest(value: unknown): Manifest {
  const data = object(value);
  keys(data, ["version", "sourceCommit", "artifacts"]);
  if (
    data["version"] !== 1 ||
    !/^[a-f0-9]{40}$/.test(string(data["sourceCommit"]))
  )
    throw new Error("Invalid manifest version or source commit");
  const entries = data["artifacts"];
  if (!Array.isArray(entries) || !entries.length)
    throw new Error("Manifest must select artifacts");
  const paths = new Set<string>(),
    subjects = new Set<string>();
  const artifacts = entries.map((entry: unknown): Artifact => {
    const item = object(entry);
    keys(item, ["path", "subject", "digest", "attestation"]);
    const path = relativePath(item["path"]),
      subject = string(item["subject"]),
      digest = object(item["digest"]);
    keys(digest, ["sha512"]);
    if (!/^[a-f0-9]{128}$/.test(string(digest["sha512"])))
      throw new Error("Expected canonical SHA-512 artifact digest");
    if (paths.has(path) || subjects.has(subject))
      throw new Error("Duplicate manifest artifact path or subject");
    paths.add(path);
    subjects.add(subject);
    const attestation =
      item["attestation"] === undefined
        ? undefined
        : relativePath(item["attestation"]);
    return {
      path,
      subject,
      digest: { sha512: string(digest["sha512"]) },
      ...(attestation ? { attestation } : {}),
    };
  });
  for (const item of artifacts) {
    const bundlePath = item.attestation ?? `${item.path}.sigstore.json`;
    if (paths.has(bundlePath))
      throw new Error("Duplicate or overlapping artifact/bundle path");
    paths.add(bundlePath);
  }
  return { version: 1, sourceCommit: string(data["sourceCommit"]), artifacts };
}
/** Check every component; O_NOFOLLOW additionally protects the final read. */
export async function containedFile(
  root: string,
  relative: string,
): Promise<Buffer> {
  const components = relativePath(relative).split("/");
  let path = root;
  for (let index = 0; index < components.length; index++) {
    path = join(path, components[index] ?? "");
    const stat = await lstat(path);
    if (
      stat.isSymbolicLink() ||
      (index + 1 === components.length ? !stat.isFile() : !stat.isDirectory())
    )
      throw new Error(
        "Manifest paths must contain regular files and directories without symlinks",
      );
  }
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    if (!(await file.stat()).isFile())
      throw new Error("Expected a regular artifact file");
    return await file.readFile();
  } finally {
    await file.close();
  }
}
export interface LoadedArtifact {
  readonly artifact: Artifact;
  readonly bytes: Buffer;
  readonly path: string;
}
export interface LoadedManifest {
  readonly manifest: Manifest;
  readonly root: string;
  readonly path: string;
  readonly artifacts: readonly LoadedArtifact[];
}
export async function loadManifest(path: string): Promise<LoadedManifest> {
  const absolute = resolve(path),
    root = await realpath(dirname(absolute));
  const data = manifest(parse(await containedFile(root, basename(absolute))));
  const artifacts: LoadedArtifact[] = [];
  for (const artifact of data.artifacts) {
    const bytes = await containedFile(root, artifact.path);
    if (sha512(bytes) !== artifact.digest.sha512)
      throw new Error(`Artifact digest mismatch: ${artifact.path}`);
    artifacts.push({ artifact, bytes, path: join(root, artifact.path) });
  }
  return {
    manifest: data,
    root,
    path: join(root, basename(absolute)),
    artifacts,
  };
}
