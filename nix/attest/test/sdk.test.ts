import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { createServer } from "node:http";
import { test } from "node:test";
import { buildSLSAProvenancePredicate } from "@actions/attest";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
const configured = {
  provider: "github-actions",
  repository: "example/package",
  workflow: ".github/workflows/qualification.yml",
  ref: "refs/heads/master",
  requiredJobs: ["Qualification", "Support (*)", "Sign bundles"],
  artifactName: "qualified-release-{runId}-{runAttempt}",
  tag: "next",
};
const identity = { sourceCommit: "a".repeat(40), runId: "12345", attempt: "2" };
const artifact = {
  path: "package.tgz",
  subject: "pkg:npm/%40example/package@1.0.0-rc.0",
  digest: { sha512: "b".repeat(128) },
};

await test("pinned SDK authenticates OIDC claims and emits the accepted generic workflow predicate", async () => {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", {
      modulusLength: 2048,
    }),
    jwk = {
      ...publicKey.export({ format: "jwk" }),
      kid: "fixture",
      alg: "RS256",
    };
  let issuer = "",
    token = "";
  const server = createServer((request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.url?.startsWith("/token"))
      response.end(JSON.stringify({ value: token }));
    else if (request.url === "/.well-known/openid-configuration")
      response.end(JSON.stringify({ jwks_uri: `${issuer}/jwks` }));
    else if (request.url === "/jwks")
      response.end(JSON.stringify({ keys: [jwk] }));
    else {
      response.statusCode = 404;
      response.end("{}");
    }
  });
  await new Promise<void>((done) => {
    server.listen(0, "127.0.0.1", done);
  });
  const address = server.address();
  if (!address || typeof address === "string")
    throw new Error("Missing fixture address");
  issuer = `http://127.0.0.1:${address.port}`;
  const workflow = `${configured.repository}/${configured.workflow}@${configured.ref}`;
  const claims = {
    iss: issuer,
    aud: "nobody",
    exp: Math.floor(Date.now() / 1000) + 60,
    ref: configured.ref,
    sha: identity.sourceCommit,
    repository: configured.repository,
    event_name: "push",
    job_workflow_ref: workflow,
    workflow_ref: workflow,
    repository_id: "123",
    repository_owner_id: "456",
    runner_environment: "github-hosted",
    run_id: identity.runId,
    run_attempt: identity.attempt,
  };
  const payload = `${Buffer.from(JSON.stringify({ alg: "RS256", kid: "fixture" })).toString("base64url")}.${Buffer.from(JSON.stringify(claims)).toString("base64url")}`;
  token = `${payload}.${sign("RSA-SHA256", Buffer.from(payload), privateKey).toString("base64url")}`;
  const environment = {
    ACTIONS_ID_TOKEN_REQUEST_URL: `${issuer}/token?fixture=1`,
    ACTIONS_ID_TOKEN_REQUEST_TOKEN: "synthetic-fixture-token",
    GITHUB_SERVER_URL: "https://github.com",
  };
  const previous = Object.fromEntries(
    Object.keys(environment).map((key) => [key, process.env[key]]),
  );
  Object.assign(process.env, environment);
  try {
    const predicate = await buildSLSAProvenancePredicate(issuer);
    assert.equal(predicate.type, "https://slsa.dev/provenance/v1");
    const statement = {
      _type: "https://in-toto.io/Statement/v1",
      predicateType: predicate.type,
      subject: [{ name: artifact.subject, digest: artifact.digest }],
      predicate: predicate.params,
    };
    const bundle = {
      dsseEnvelope: {
        payloadType: "application/vnd.in-toto+json",
        payload: Buffer.from(JSON.stringify(statement)).toString("base64"),
      },
    };
    // The native SDK's authenticated claims must satisfy the Python policy validator.
    const scripts = fileURLToPath(
      new URL("../../../../scripts/", import.meta.url),
    );
    const result = spawnSync(
      process.env["CHAINMAN_ATTEST_PYTHON"] ?? "python3",
      [
        "-I",
        "-B",
        "-c",
        `import json,sys
sys.path.insert(0,sys.argv[1])
from attestation_model import policy,manifest
from attestation_github import Identity
from attestation_provenance import verify_bundle
value=json.load(sys.stdin)
artifact=manifest({"version":1,"sourceCommit":value["identity"]["sourceCommit"],"artifacts":[value["artifact"]]}).artifacts[0]
identity=Identity(value["identity"]["sourceCommit"],value["identity"]["runId"],value["identity"]["attempt"])
verify_bundle(value["bundle"],artifact,identity,policy(value["policy"]))`,
        scripts,
      ],
      {
        input: JSON.stringify({
          bundle,
          artifact,
          identity,
          policy: configured,
        }),
        encoding: "utf8",
      },
    );
    assert.equal(result.status, 0, result.stderr || String(result.error));
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await new Promise<void>((done, reject) => {
      server.close((error) => {
        if (error) reject(error);
        else done();
      });
    });
  }
});
