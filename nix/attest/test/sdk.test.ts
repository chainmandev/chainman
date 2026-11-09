import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import { createServer } from "node:http";
import { test } from "node:test";
import { buildSLSAProvenancePredicate } from "@actions/attest";
import { verifyBundle } from "../src/provenance.js";
import {
  artifact,
  bundle,
  configured,
  identity,
  statement,
} from "./fixtures.js";

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
    verifyBundle(
      bundle({
        ...statement(),
        predicateType: predicate.type,
        predicate: predicate.params,
      }),
      artifact,
      identity,
      configured,
    );
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
