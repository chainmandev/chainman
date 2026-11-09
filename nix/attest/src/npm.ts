import { createRequire } from "node:module";
import { lstat, mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { absolute, normalizedObject, object, string } from "./protocol.js";
import type { JsonObject } from "./protocol.js";

const require = createRequire(import.meta.url);
const npmVersion = "12.2.0";
type NativeFunction = (...args: unknown[]) => unknown;
function nativeFunction(value: unknown, key: string): NativeFunction {
  if (
    (typeof value !== "object" && typeof value !== "function") ||
    value === null
  )
    throw new Error("Pinned npm adapter contract changed");
  const member: unknown = Reflect.get(value, key);
  if (typeof member !== "function")
    throw new Error("Pinned npm adapter contract changed");
  return member as NativeFunction;
}
function repository(
  manifest: JsonObject,
  fromUrl: NativeFunction,
): string | null {
  const value = manifest["repository"];
  const url =
    typeof value === "object" && value !== null && !Array.isArray(value)
      ? value["url"]
      : value;
  if (typeof url !== "string" || !url) return null;
  const parsed = fromUrl(url);
  if (!parsed) return null;
  const info = object(parsed);
  if (
    info["domain"] !== "github.com" ||
    typeof info["user"] !== "string" ||
    !info["user"] ||
    typeof info["project"] !== "string" ||
    !info["project"]
  )
    return null;
  return `github.com/${info["user"]}/${info["project"]}`;
}
export async function inspectPackage(tarball: string): Promise<{
  manifest: JsonObject;
  subject: string;
  repository: string | null;
}> {
  absolute(tarball);
  if (!(await lstat(tarball)).isFile())
    throw new Error("Expected a local npm tarball file");
  const packagePath = require.resolve("npm/package.json");
  if (
    object(JSON.parse(await readFile(packagePath, "utf8")) as unknown)[
      "version"
    ] !== npmVersion
  )
    throw new Error("Installed npm does not match the exact publisher pin");
  const npmRequire = createRequire(packagePath);
  const npa = npmRequire("npm-package-arg") as unknown;
  const pacote = npmRequire("pacote") as unknown;
  const hosted = npmRequire("hosted-git-info") as unknown;
  const resolve = nativeFunction(npa, "resolve");
  const toPurl = nativeFunction(npa, "toPurl");
  const fromUrl = nativeFunction(hosted, "fromUrl");
  const cache = await mkdtemp(join(tmpdir(), "chainman-npm-inspect-"));
  try {
    const manifest = normalizedObject(
      await nativeFunction(pacote, "manifest")(tarball, {
        fullReadJson: true,
        fullMetadata: true,
        ignoreScripts: true,
        offline: true,
        cache,
      }),
    );
    return {
      manifest,
      subject: string(
        toPurl(resolve(string(manifest["name"]), string(manifest["version"]))),
      ),
      repository: repository(manifest, fromUrl),
    };
  } finally {
    await rm(cache, { recursive: true, force: true });
  }
}
