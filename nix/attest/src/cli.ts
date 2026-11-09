#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { parse } from "./common.js";
import { policy } from "./model.js";
import { createAttestations, verifyManifest } from "./provenance.js";
import { npmPublish } from "./publication.js";

async function main(args: string[]): Promise<void> {
  const policyIndex = args.indexOf("--policy"),
    policyPath = args[policyIndex + 1];
  if (
    policyIndex < 0 ||
    !policyPath ||
    args.filter((arg) => arg === "--policy").length !== 1
  )
    throw new Error("An explicit --policy JSON file is required");
  args.splice(policyIndex, 2);
  const configured = policy(parse(await readFile(policyPath)));
  if (
    args[0] === "attest" &&
    args[1] === "create" &&
    args[2] &&
    (args.length === 3 || (args.length === 5 && args[3] === "--run" && args[4]))
  ) {
    await createAttestations(args[2], configured, args[4]);
    return;
  }
  const publication = args[0] === "npm-publish",
    offset = publication ? 1 : 2;
  if (
    (!publication && (args[0] !== "attest" || args[1] !== "verify")) ||
    args.length !== offset + 3 ||
    args[offset + 1] !== "--run"
  )
    throw new Error(
      "Usage: attest create MANIFEST | attest verify MANIFEST --run ID | npm-publish MANIFEST --run ID",
    );
  const path = args[offset],
    runId = args[offset + 2];
  if (!path || !runId) throw new Error("A manifest and run ID are required");
  if (publication) await npmPublish(path, runId, configured);
  else {
    const verified = await verifyManifest(path, runId, configured);
    await verified.dispose();
  }
}
try {
  await main(process.argv.slice(2));
} catch (error) {
  console.error(
    error instanceof Error ? error.message : "Attestation command failed",
  );
  process.exitCode = 1;
}
