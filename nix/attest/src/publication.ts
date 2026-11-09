import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { isDeepStrictEqual } from "node:util";
import { command, object, sha512, string } from "./common.js";
import type { Command, CommandResult } from "./common.js";
import { npmAdapter } from "./npm.js";
import { predicateType, verifyManifest } from "./provenance.js";
import type { VerifiedManifest } from "./provenance.js";
import type { Policy } from "./model.js";

export interface Package {
  readonly name: string;
  readonly version: string;
  readonly path: string;
  readonly bundlePath: string;
  readonly bytes: Buffer;
  readonly bundleBytes: Buffer;
  readonly bundle: unknown;
  readonly dependencies: readonly string[];
}
export interface Registry {
  lookup(pkg: Package, wait?: boolean): Promise<string | undefined>;
  publish(pkg: Package): Promise<void>;
  verify(pkg: Package): Promise<void>;
}
export function dependencyOrder(packages: readonly Package[]): Package[] {
  const map = new Map(packages.map((pkg) => [pkg.name, pkg]));
  if (map.size !== packages.length)
    throw new Error("Duplicate npm publication package");
  const visiting = new Set<string>(),
    complete = new Set<string>(),
    ordered: Package[] = [];
  function visit(pkg: Package): void {
    if (complete.has(pkg.name)) return;
    if (visiting.has(pkg.name))
      throw new Error(`Publication dependency cycle: ${pkg.name}`);
    visiting.add(pkg.name);
    for (const name of pkg.dependencies) {
      const dependency = map.get(name);
      if (dependency) visit(dependency);
    }
    visiting.delete(pkg.name);
    complete.add(pkg.name);
    ordered.push(pkg);
  }
  for (const pkg of packages) visit(pkg);
  return ordered;
}
/** Every immutable version is reconciled before the first upload; no local receipts. */
export async function publish(
  packages: readonly Package[],
  registry: Registry,
): Promise<void> {
  const ordered = dependencyOrder(packages),
    observed = new Map<string, string | undefined>();
  for (const pkg of ordered) {
    const digest = await registry.lookup(pkg);
    if (digest !== undefined) {
      if (digest !== sha512(pkg.bytes))
        throw new Error(`Registry digest mismatch: ${pkg.name}@${pkg.version}`);
      await registry.verify(pkg);
    }
    observed.set(pkg.name, digest);
  }
  for (const pkg of ordered) {
    if (observed.get(pkg.name) !== undefined) continue;
    let digest: string | undefined;
    try {
      await registry.publish(pkg);
    } catch (cause) {
      try {
        digest = await registry.lookup(pkg, true);
      } catch {
        throw cause;
      }
      if (digest === undefined) throw cause;
    }
    digest ??= await registry.lookup(pkg, true);
    if (digest !== sha512(pkg.bytes))
      throw new Error(`Registry digest mismatch: ${pkg.name}@${pkg.version}`);
    await registry.verify(pkg);
  }
}
function missing(result: CommandResult): boolean {
  const streams = [result.stdout.trim(), result.stderr.trim()].filter(Boolean);
  return (
    streams.length > 0 &&
    streams.every((stream) => {
      try {
        return (
          object(object(JSON.parse(stream) as unknown)["error"])["code"] ===
          "E404"
        );
      } catch {
        return false;
      }
    })
  );
}
export interface RegistryOptions {
  readonly command?: Command;
  readonly fetch?: typeof fetch;
  readonly attempts?: number;
  readonly delayMs?: number;
  /** Isolated local tests only; the public CLI always constructs the public adapter. */
  readonly registry?: URL;
}
export async function npmRegistry(
  options: RegistryOptions = {},
): Promise<Registry> {
  const npm = await npmAdapter(),
    invoke = options.command ?? command,
    request = options.fetch ?? fetch;
  const registry = options.registry ?? new URL("https://registry.npmjs.org/");
  if (
    registry.href !== "https://registry.npmjs.org/" &&
    !["127.0.0.1", "localhost", "[::1]"].includes(registry.hostname)
  )
    throw new Error("Publication requires the public npm registry");
  const attempts = options.attempts ?? 5,
    delayMs = options.delayMs ?? 1000;
  if (
    !Number.isSafeInteger(attempts) ||
    attempts < 1 ||
    !Number.isSafeInteger(delayMs) ||
    delayMs < 0
  )
    throw new Error("Invalid registry retry options");
  const flags = (name: string, bundlePath: string): string[] => [
    "--registry",
    registry.href,
    "--scope=",
    ...(name.startsWith("@")
      ? [`--${name.slice(0, name.indexOf("/"))}:registry=${registry.href}`]
      : []),
    "--ignore-scripts",
    "--access",
    "public",
    "--tag",
    "next",
    "--provenance-file",
    bundlePath,
    "--update-notifier=false",
  ];
  const configured = new Set<string>();
  async function configuration(pkg: Package): Promise<void> {
    if (configured.has(pkg.name)) return;
    const args = flags(pkg.name, pkg.bundlePath);
    const result = await invoke(process.execPath, [
      npm.cliPath,
      "config",
      "get",
      "registry",
      ...args,
    ]);
    if (result.status !== 0 || result.stdout.trim() !== registry.href)
      throw new Error(
        "npm publication configuration check failed before upload",
      );
    configured.add(pkg.name);
  }
  async function view(
    pkg: Package,
    field: string,
  ): Promise<unknown | undefined> {
    const result = await invoke(process.execPath, [
      npm.cliPath,
      "view",
      `${pkg.name}@${pkg.version}`,
      field,
      "--json",
      "--loglevel=silent",
      ...flags(pkg.name, pkg.bundlePath),
    ]);
    if (result.status !== 0) {
      if (missing(result)) return undefined;
      throw new Error(
        `Registry lookup failed (${result.status}): ${pkg.name}@${pkg.version}`,
      );
    }
    const parsed: unknown = JSON.parse(result.stdout);
    if (Array.isArray(parsed)) {
      if (parsed.length !== 1)
        throw new Error("Expected exactly one npm registry version result");
      return parsed[0] as unknown;
    }
    return parsed;
  }
  async function retry<T>(read: () => Promise<T>): Promise<T> {
    let failure: unknown;
    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        return await read();
      } catch (error) {
        failure = error;
      }
      if (attempt + 1 < attempts)
        await new Promise<void>((done) => {
          setTimeout(done, delayMs * (attempt + 1));
        });
    }
    throw failure;
  }
  async function download(value: unknown): Promise<Response> {
    const url = new URL(string(value));
    if (url.origin !== registry.origin || url.username || url.password)
      throw new Error("Registry download has an unexpected origin");
    const response = await request(url, {
      redirect: "error",
      signal: AbortSignal.timeout(30000),
    });
    if (!response.ok)
      throw new Error(`Registry download failed (${response.status})`);
    return response;
  }
  return {
    async lookup(pkg, wait = false) {
      await configuration(pkg);
      return await retry(async () => {
        const value = await view(pkg, "dist");
        if (value === undefined) {
          if (wait) throw new Error("Registry upload is not visible yet");
          return undefined;
        }
        const dist = object(value),
          digest = sha512(
            new Uint8Array(
              await (await download(dist["tarball"])).arrayBuffer(),
            ),
          );
        if (
          `sha512-${Buffer.from(digest, "hex").toString("base64")}` !==
          dist["integrity"]
        )
          throw new Error("Registry metadata disagrees with downloaded bytes");
        return digest;
      });
    },
    async publish(pkg) {
      await configuration(pkg);
      if (
        sha512(await readFile(pkg.path)) !== sha512(pkg.bytes) ||
        !(await readFile(pkg.bundlePath)).equals(pkg.bundleBytes)
      )
        throw new Error("Frozen publication files changed before upload");
      const result = await invoke(
        process.execPath,
        [npm.cliPath, "publish", pkg.path, ...flags(pkg.name, pkg.bundlePath)],
        true,
      );
      if (result.status !== 0)
        throw new Error(
          `Package upload failed (${result.status}): ${pkg.name}`,
        );
    },
    async verify(pkg) {
      await retry(async () => {
        const dist = object(await view(pkg, "dist")),
          attestations = object(dist["attestations"]);
        if (
          object(attestations["provenance"])["predicateType"] !== predicateType
        )
          throw new Error("Registry provenance metadata missing");
        const entries = object(
          (await (await download(attestations["url"])).json()) as unknown,
        )["attestations"];
        if (
          !Array.isArray(entries) ||
          !entries.some((entry: unknown) => {
            const attestation = object(entry);
            return (
              attestation["predicateType"] === predicateType &&
              isDeepStrictEqual(attestation["bundle"], pkg.bundle)
            );
          })
        )
          throw new Error(
            "Registry provenance differs from the exact verified bundle",
          );
        if (object(await view(pkg, "dist-tags"))["next"] !== pkg.version)
          throw new Error(
            `Registry next tag does not select ${pkg.name}@${pkg.version}`,
          );
      });
    },
  };
}
export async function snapshotPackages(
  verified: VerifiedManifest,
  directory: string,
  policy: Policy,
): Promise<Package[]> {
  const npm = await npmAdapter(),
    packages: Package[] = [];
  for (const [index, selected] of verified.artifacts.entries()) {
    const path = join(directory, `${index}.tgz`),
      bundlePath = `${path}.sigstore.json`;
    await writeFile(path, selected.bytes, { flag: "wx", mode: 0o400 });
    await writeFile(bundlePath, selected.bundleBytes, {
      flag: "wx",
      mode: 0o400,
    });
    const data = await npm.package(path),
      name = string(data["name"]),
      version = string(data["version"]);
    if (
      npm.repository(data["repository"]) !== `github.com/${policy.repository}`
    )
      throw new Error(
        `Local npm package ${name}@${version} repository must match signing repository https://github.com/${policy.repository}`,
      );
    if (
      Boolean(data["private"]) ||
      data["packageExtensions"] !== undefined ||
      (data["tag"] !== undefined && data["tag"] !== "next") ||
      npm.subject(name, version) !== selected.artifact.subject
    )
      throw new Error(
        "Local npm tarball metadata does not match its manifest PURL or next publication policy",
      );
    const dependencies = Object.keys(object(data["dependencies"] ?? {}));
    packages.push({
      name,
      version,
      path,
      bundlePath,
      bytes: selected.bytes,
      bundleBytes: selected.bundleBytes,
      bundle: selected.bundle,
      dependencies,
    });
  }
  dependencyOrder(packages);
  return packages;
}
export async function npmPublish(
  path: string,
  runId: string,
  policy: Policy,
): Promise<void> {
  if (process.env["CI"] || process.env["GITHUB_ACTIONS"])
    throw new Error("npm publication is manual only; CI never publishes");
  if (policy.tag !== "next")
    throw new Error("npm publication requires explicit next tag policy");
  const verified = await verifyManifest(path, runId, policy),
    directory = await mkdtemp(join(tmpdir(), "chainman-npm-"));
  await chmod(directory, 0o700);
  try {
    await publish(
      await snapshotPackages(verified, directory, policy),
      await npmRegistry(),
    );
  } finally {
    await verified.dispose();
    await rm(directory, { recursive: true, force: true });
  }
}
