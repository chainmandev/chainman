#!/usr/bin/env node
import { attestProvenance } from "@actions/attest";
import { constants } from "node:fs";
import { lstat, open, rm } from "node:fs/promises";
import { dirname } from "node:path";
import { inspectPackage } from "./npm.js";
import { absolute, normalizedObject, request } from "./protocol.js";
import type { Request, Response } from "./protocol.js";

const maxRequest = 1024 * 1024;
const maxResponse = 16 * 1024 * 1024;

async function input(): Promise<Request> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk));
    length += bytes.length;
    if (length > maxRequest)
      throw new Error("Adapter request exceeds size limit");
    chunks.push(bytes);
  }
  return request(JSON.parse(Buffer.concat(chunks).toString("utf8")) as unknown);
}
async function operate(value: Request): Promise<Response> {
  if (value.operation === "npm-inspect") {
    const metadata = await inspectPackage(value.tarball);
    return { protocol: 1, operation: value.operation, ...metadata };
  }
  const token = process.env["GITHUB_TOKEN"] ?? process.env["GH_TOKEN"];
  if (!token) throw new Error("Signing requires the workflow token");
  // Keep SDK stdout inherited: its OIDC client emits GitHub add-mask commands.
  const result = await attestProvenance({
    subjects: [value.subject],
    token,
    sigstore: "public-good",
  });
  return {
    protocol: 1,
    operation: value.operation,
    bundle: normalizedObject(result.bundle),
  };
}
async function main(args: readonly string[]): Promise<void> {
  if (args.length !== 2 || args[0] !== "--response-file")
    throw new Error(
      "Usage: chainman-attest-adapter --response-file ABSOLUTE_PATH",
    );
  const destination = absolute(args[1]);
  const parent = await lstat(dirname(destination));
  if (
    !parent.isDirectory() ||
    parent.uid !== process.getuid?.() ||
    (parent.mode & 0o777) !== 0o700
  )
    throw new Error(
      "Adapter response requires a private parent-owned directory",
    );
  const value = await input();
  const file = await open(
    destination,
    constants.O_WRONLY |
      constants.O_CREAT |
      constants.O_EXCL |
      constants.O_NOFOLLOW,
    0o600,
  );
  let complete = false;
  try {
    await file.chmod(0o600);
    const response = `${JSON.stringify(await operate(value))}\n`;
    if (Buffer.byteLength(response) > maxResponse)
      throw new Error("Adapter response exceeds size limit");
    await file.writeFile(response);
    complete = true;
  } finally {
    await file.close();
    if (!complete) await rm(destination, { force: true });
  }
}
try {
  await main(process.argv.slice(2));
} catch {
  // Native diagnostics can contain authentication material or signed URLs.
  console.error("Attestation adapter failed");
  process.exitCode = 1;
}
