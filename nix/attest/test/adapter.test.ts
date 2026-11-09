import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  mkdtemp,
  chmod,
  lstat,
  readFile,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { request } from "../src/protocol.js";

const digest = { sha512: "a".repeat(128) };
const signing = {
  protocol: 1,
  operation: "attest-provenance",
  subject: { name: "pkg:npm/example@1.0.0", digest },
};
const inspection = {
  protocol: 1,
  operation: "npm-inspect",
  tarball: "/tmp/package.tgz",
};
const entry = fileURLToPath(new URL("../src/adapter.js", import.meta.url));

test("adapter protocol accepts only the two exact request shapes", () => {
  assert.deepEqual(request(signing), signing);
  assert.deepEqual(request(inspection), inspection);
  const invalid: unknown[] = [
    null,
    [],
    {},
    { ...inspection, protocol: true },
    { ...inspection, protocol: 2 },
    { ...inspection, operation: "other" },
    { ...inspection, tarball: "relative.tgz" },
    { ...inspection, token: "secret" },
    { ...signing, issuer: "https://untrusted.invalid" },
    { ...signing, subject: { ...signing.subject, extra: true } },
    {
      ...signing,
      subject: {
        ...signing.subject,
        digest: { ...digest, sha256: "a".repeat(64) },
      },
    },
    {
      ...signing,
      subject: { ...signing.subject, digest: { sha512: "A".repeat(128) } },
    },
    { ...signing, subject: { ...signing.subject, name: "" } },
  ];
  for (const value of invalid) assert.throws(() => request(value));
});

test("adapter failures never replace existing files or leave an accepted response", async () => {
  const directory = await mkdtemp(join(tmpdir(), "chainman-adapter-test-"));
  const destination = join(directory, "response.json");
  const invoke = (value: unknown, response = destination) =>
    spawnSync(process.execPath, [entry, "--response-file", response], {
      input: JSON.stringify(value),
      encoding: "utf8",
      env: { ...process.env, GITHUB_TOKEN: "", GH_TOKEN: "" },
    });
  try {
    await chmod(directory, 0o700);
    assert.notEqual(invoke({ ...inspection, token: "secret" }).status, 0);
    await assert.rejects(lstat(destination), { code: "ENOENT" });
    assert.notEqual(invoke(signing).status, 0);
    await assert.rejects(lstat(destination), { code: "ENOENT" });
    assert.notEqual(invoke(inspection).status, 0);
    await assert.rejects(lstat(destination), { code: "ENOENT" });
    await writeFile(destination, "sentinel", { mode: 0o600 });
    assert.notEqual(invoke(inspection).status, 0);
    assert.equal(await readFile(destination, "utf8"), "sentinel");
    const link = join(directory, "response-link.json");
    await symlink(destination, link);
    assert.notEqual(invoke(inspection, link).status, 0);
    assert.equal(await readFile(destination, "utf8"), "sentinel");
    await chmod(directory, 0o755);
    assert.notEqual(
      invoke(inspection, join(directory, "public-response.json")).status,
      0,
    );
    await assert.rejects(lstat(join(directory, "public-response.json")), {
      code: "ENOENT",
    });
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
