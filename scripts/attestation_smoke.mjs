#!/usr/bin/env node
// Disposable branch smoke fixture. It never contacts an npm registry.
import assert from "node:assert/strict";
import {
  chmod,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { basename, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  command,
  object,
  sha512,
  string,
} from "../nix/attest/dist/src/common.js";
import { npmAdapter } from "../nix/attest/dist/src/npm.js";

const repository = "chainmandev/chainman";
const workflow = ".github/workflows/attestation-smoke.yml";
const smokeRef = "refs/heads/codex/attestation-smoke-20261109";
const name = "@chainman/attestation-smoke";
const version = "0.0.0-smoke.0";

async function main(args) {
  const [action, destination, ...rest] = args;
  if (!["pack", "create"].includes(action) || !destination || rest.length % 2)
    throw new Error(
      "Usage: attestation_smoke.mjs pack|create NEW_DIRECTORY [--commit SHA --run ID --attempt N --ref REF]",
    );
  const options = new Map();
  for (let index = 0; index < rest.length; index += 2) {
    const flag = rest[index];
    if (
      !["--commit", "--run", "--attempt", "--ref"].includes(flag) ||
      options.has(flag)
    )
      throw new Error(`Unexpected or repeated fixture option: ${flag}`);
    options.set(flag, rest[index + 1]);
  }
  const sourceCommit = options.get("--commit") ?? process.env.GITHUB_SHA ?? "";
  const runId = options.get("--run") ?? process.env.GITHUB_RUN_ID ?? "";
  const attempt =
    options.get("--attempt") ?? process.env.GITHUB_RUN_ATTEMPT ?? "";
  const ref = options.get("--ref") ?? process.env.GITHUB_REF ?? "";
  if (
    !/^[a-f0-9]{40}$/.test(sourceCommit) ||
    !/^[1-9]\d*$/.test(runId) ||
    !/^[1-9]\d*$/.test(attempt) ||
    ref !== smokeRef
  )
    throw new Error(
      "The fixture requires a full source commit, numeric run/attempt and the exact disposable smoke ref",
    );
  if (
    action === "create" &&
    (process.env.GITHUB_SHA !== sourceCommit ||
      process.env.GITHUB_RUN_ID !== runId ||
      process.env.GITHUB_RUN_ATTEMPT !== attempt ||
      process.env.GITHUB_REF !== ref)
  )
    throw new Error(
      "Signing fixture arguments must equal the actual Actions environment",
    );
  const output = resolve(destination),
    npm = await npmAdapter();
  await mkdir(output, { mode: 0o700 });
  const fixture = await mkdtemp(join(output, ".fixture-"));
  let tarball;
  try {
    const disabled =
      "node -e \"throw new Error('Smoke fixture lifecycle must remain disabled')\"";
    await writeFile(
      join(fixture, "package.json"),
      `${JSON.stringify(
        {
          name,
          version,
          description: "Disposable Chainman attestation smoke fixture",
          type: "module",
          license: "MIT",
          repository: {
            type: "git",
            url: "git+https://github.com/chainmandev/chainman.git",
          },
          files: ["index.js", "package.json"],
          scripts: {
            prepack: disabled,
            prepare: disabled,
            prepublishOnly: disabled,
            publish: disabled,
          },
          publishConfig: { access: "public", tag: "next" },
        },
        null,
        2,
      )}\n`,
      { mode: 0o644 },
    );
    await writeFile(
      join(fixture, "index.js"),
      "export const smoke = 'chainman-attestation-smoke';\n",
      { mode: 0o644 },
    );
    await chmod(join(fixture, "package.json"), 0o644);
    await chmod(join(fixture, "index.js"), 0o644);
    const packed = await command(process.execPath, [
      npm.cliPath,
      "pack",
      fixture,
      "--ignore-scripts",
      "--pack-destination",
      output,
      "--json",
    ]);
    if (packed.status !== 0)
      throw new Error(`Pinned npm fixture pack failed: ${packed.stderr}`);
    const data = JSON.parse(packed.stdout);
    const record = Array.isArray(data)
      ? object(data[0])
      : object(object(data)[name]);
    const filename = string(record.filename);
    if (basename(filename) !== filename || !filename.endsWith(".tgz"))
      throw new Error("Unexpected pinned npm pack filename");
    tarball = join(output, filename);
    const metadata = await npm.package(tarball);
    assert.equal(metadata.name, name);
    assert.equal(metadata.version, version);
  } finally {
    await rm(fixture, { recursive: true, force: true });
  }
  const bytes = await readFile(tarball),
    filename = basename(tarball);
  const manifestPath = join(output, "manifest.json"),
    policyPath = join(output, "policy.json");
  await writeFile(
    manifestPath,
    `${JSON.stringify({ version: 1, sourceCommit, artifacts: [{ path: filename, subject: npm.subject(name, version), digest: { sha512: sha512(bytes) } }] }, null, 2)}\n`,
    { flag: "wx", mode: 0o600 },
  );
  await writeFile(
    policyPath,
    `${JSON.stringify({ provider: "github-actions", repository, workflow, ref, requiredJobs: ["SDK smoke"], artifactName: "qualified-release-{runId}-{runAttempt}", tag: "next" }, null, 2)}\n`,
    { flag: "wx", mode: 0o600 },
  );
  if (action === "create") {
    const cli = fileURLToPath(
      new URL("../nix/attest/dist/src/cli.js", import.meta.url),
    );
    const signed = await command(
      process.execPath,
      [
        cli,
        "attest",
        "create",
        manifestPath,
        "--run",
        runId,
        "--policy",
        policyPath,
      ],
      true,
    );
    if (signed.status !== 0) throw new Error("Shared SDK smoke signing failed");
  }
  console.log(`tarball=${tarball}`);
  console.log(`sha512=${sha512(bytes)}`);
}

try {
  await main(process.argv.slice(2));
} catch (error) {
  console.error(
    error instanceof Error ? error.message : "Attestation smoke failed",
  );
  process.exitCode = 1;
}
