import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { readFile } from "node:fs/promises";
import { object, parse, string } from "./common.js";

const require = createRequire(import.meta.url);
export const npmVersion = "12.2.0";
interface NpmAdapter {
  readonly cliPath: string;
  subject(name: string, version: string): string;
  package(path: string): Promise<Record<string, unknown>>;
  repository(value: unknown): string | undefined;
}
export async function npmAdapter(): Promise<NpmAdapter> {
  const packagePath = require.resolve("npm/package.json"),
    npmRequire = createRequire(packagePath);
  if (object(parse(await readFile(packagePath)))["version"] !== npmVersion)
    throw new Error("Installed npm does not match the exact publisher pin");
  const npa = npmRequire("npm-package-arg") as unknown,
    pacote = object(npmRequire("pacote") as unknown),
    hosted = npmRequire("hosted-git-info") as { fromUrl?: unknown };
  const functions = npa as { resolve?: unknown; toPurl?: unknown };
  if (
    typeof functions.resolve !== "function" ||
    typeof functions.toPurl !== "function" ||
    typeof pacote["manifest"] !== "function" ||
    typeof hosted.fromUrl !== "function"
  )
    throw new Error("Pinned npm metadata/PURL adapter contract changed");
  const resolve = functions.resolve as (
    name: string,
    version: string,
  ) => unknown;
  const toPurl = functions.toPurl as (spec: unknown) => unknown;
  const fromUrl = hosted.fromUrl as (url: string) => unknown;
  const manifest = pacote["manifest"] as (
    path: string,
    options: { ignoreScripts: true; offline: true },
  ) => Promise<unknown>;
  return {
    cliPath: join(dirname(packagePath), "bin/npm-cli.js"),
    subject(name, version) {
      return string(toPurl(resolve(name, version)));
    },
    async package(path) {
      return object(
        await manifest(path, { ignoreScripts: true, offline: true }),
      );
    },
    repository(value) {
      const url =
        typeof value === "object" && value !== null && !Array.isArray(value)
          ? object(value)["url"]
          : value;
      if (typeof url !== "string" || !url) return undefined;
      const parsed = fromUrl(url);
      if (!parsed) return undefined;
      const info = object(parsed),
        host = info["domain"],
        owner = info["user"],
        project = info["project"];
      if (
        typeof host !== "string" ||
        !host ||
        typeof owner !== "string" ||
        !owner ||
        typeof project !== "string" ||
        !project
      )
        return undefined;
      return `${host}/${owner}/${project}`;
    },
  };
}
