import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { Server } from "node:http";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { command, object, sha512, string } from "../src/common.js";
import { npmAdapter } from "../src/npm.js";
import { policy } from "../src/model.js";
import {
  dependencyOrder,
  npmPublish,
  npmRegistry,
  publish,
  snapshotPackages,
} from "../src/publication.js";
import type { Package, Registry } from "../src/publication.js";
import type { VerifiedManifest } from "../src/provenance.js";
import { configured, identity, statement } from "./fixtures.js";
import { predicateType } from "../src/provenance.js";
const actualTarball = process.env["CHAINMAN_ATTEST_SMOKE_TARBALL"],
  actualBundle = process.env["CHAINMAN_ATTEST_SMOKE_BUNDLE"],
  actualRepository = process.env["CHAINMAN_ATTEST_SMOKE_REPOSITORY"];

const leaf: Package = {
  name: "leaf",
  version: "1.0.0",
  path: "/leaf.tgz",
  bundlePath: "/leaf.sigstore.json",
  bytes: Buffer.from("leaf"),
  bundleBytes: Buffer.from("{}"),
  bundle: {},
  dependencies: [],
};
const parent = {
  ...leaf,
  name: "parent",
  dependencies: ["leaf"],
  bytes: Buffer.from("parent"),
};
await test("all registry versions and saved bundles preflight before upload; collisions fail closed", async () => {
  for (const collision of ["bytes", "provenance", "lookup"]) {
    const calls: string[] = [];
    const registry: Registry = {
      lookup(pkg) {
        calls.push(`lookup:${pkg.name}`);
        if (pkg.name === "parent" && collision === "lookup")
          return Promise.reject(new Error("lookup failure"));
        return Promise.resolve(
          pkg.name === "parent"
            ? collision === "bytes"
              ? "wrong"
              : sha512(pkg.bytes)
            : undefined,
        );
      },
      publish(pkg) {
        calls.push(`upload:${pkg.name}`);
        return Promise.resolve();
      },
      verify() {
        return Promise.reject(new Error("provenance mismatch"));
      },
    };
    await assert.rejects(publish([parent, leaf], registry));
    assert.ok(!calls.some((call) => call.startsWith("upload:")));
  }
  assert.deepEqual(
    dependencyOrder([parent, leaf]).map((pkg) => pkg.name),
    ["leaf", "parent"],
  );
  assert.throws(() => dependencyOrder([leaf, leaf]), /Duplicate/);
  assert.throws(
    () => dependencyOrder([{ ...leaf, dependencies: ["parent"] }, parent]),
    /cycle/,
  );
});
await test("registry reconciliation recovers lost upload acknowledgements and resumes without another upload", async () => {
  let uploaded = false,
    uploads = 0,
    verifies = 0;
  const registry: Registry = {
    lookup(pkg) {
      return Promise.resolve(uploaded ? sha512(pkg.bytes) : undefined);
    },
    publish() {
      uploaded = true;
      uploads++;
      return Promise.reject(new Error("lost acknowledgement"));
    },
    verify() {
      verifies++;
      return Promise.resolve();
    },
  };
  await publish([leaf], registry);
  await publish([leaf], registry);
  assert.equal(uploads, 1);
  assert.equal(verifies, 2);
});
await test("public adapter refuses other registries; manual publication refuses CI and missing npm tag", async () => {
  await assert.rejects(
    npmRegistry({ registry: new URL("https://registry.example/") }),
    /public npm/,
  );
  const flags = ["CI", "GITHUB_ACTIONS"] as const;
  const previous = Object.fromEntries(
    flags.map((flag) => [flag, process.env[flag]]),
  );
  try {
    for (const active of [
      ["CI"],
      ["GITHUB_ACTIONS"],
      ["CI", "GITHUB_ACTIONS"],
    ]) {
      for (const flag of flags) {
        if (active.includes(flag)) process.env[flag] = "true";
        else delete process.env[flag];
      }
      await assert.rejects(
        npmPublish("unused", identity.runId, configured),
        /manual only/,
      );
    }
    // Isolate the missing-tag case from the real inherited CI environment.
    for (const flag of flags) delete process.env[flag];
    const { tag: _tag, ...genericPolicy } = configured;
    await assert.rejects(
      npmPublish("unused", identity.runId, genericPolicy),
      /explicit next/,
    );
  } finally {
    for (const flag of flags) {
      const value = previous[flag];
      if (value === undefined) delete process.env[flag];
      else process.env[flag] = value;
    }
  }
});
await test("registry errors never mistake authentication/transport failures for absence or leak diagnostics", async () => {
  for (const output of [
    "E404 synthetic-token",
    '{"error":{"code":"E401","message":"E404 synthetic-token"}}',
    "null",
    "[]",
    "{}",
  ]) {
    const registry = await npmRegistry({
      attempts: 1,
      command: async (_program, args) =>
        args[1] === "config"
          ? { status: 0, stdout: "https://registry.npmjs.org/\n", stderr: "" }
          : { status: 1, stdout: output, stderr: "" },
    });
    await assert.rejects(
      registry.lookup(leaf),
      (error) =>
        error instanceof Error &&
        /Registry lookup failed/.test(error.message) &&
        !error.message.includes("synthetic-token"),
    );
  }
});
async function listening(server: Server): Promise<URL> {
  await new Promise<void>((done) => {
    server.listen(0, "127.0.0.1", done);
  });
  const address = server.address();
  if (!address || typeof address === "string")
    throw new Error("Missing loopback server address");
  return new URL(`http://127.0.0.1:${address.port}/`);
}
async function close(server: Server): Promise<void> {
  await new Promise<void>((done, reject) => {
    server.close((error) => {
      if (error) reject(error);
      else done();
    });
  });
}
async function pack(
  directory: string,
  name: string,
  extra: Record<string, unknown> = {},
): Promise<{ bytes: Buffer; version: string }> {
  const npm = await npmAdapter(),
    path = join(directory, name.replaceAll(/[^A-Za-z0-9]/g, "-"));
  await mkdir(path);
  await writeFile(
    join(path, "package.json"),
    JSON.stringify({
      name,
      version: "1.0.0-rc.0",
      repository: `git+https://github.com/${configured.repository}.git`,
      scripts: {
        prepublishOnly: "exit 91",
        prepare: "exit 92",
        publish: "exit 93",
      },
      publishConfig: { registry: "https://untrusted.invalid/", tag: "latest" },
      ...extra,
    }),
  );
  await writeFile(join(path, "index.js"), "export default 1;\n");
  const result = await command(process.execPath, [
    npm.cliPath,
    "pack",
    path,
    "--ignore-scripts",
    "--pack-destination",
    directory,
    "--json",
  ]);
  assert.equal(result.status, 0, result.stderr);
  const packed: unknown = JSON.parse(result.stdout);
  const record = Array.isArray(packed)
    ? object(packed[0])
    : object(object(packed)[name]);
  return {
    bytes: await readFile(join(directory, string(record["filename"]))),
    version: "1.0.0-rc.0",
  };
}
function verified(
  bytes: Buffer,
  name: string,
  version: string,
): VerifiedManifest {
  const subject = `pkg:npm/${name.replace("@", "%40")}@${version}`,
    artifact = {
      path: `${name.replaceAll(/[^A-Za-z0-9]/g, "-")}.tgz`,
      subject,
      digest: { sha512: sha512(bytes) },
    },
    path = `/local/${artifact.path}`;
  const bundle = {
    mediaType: "application/vnd.dev.sigstore.bundle.v0.3+json",
    dsseEnvelope: {
      payloadType: "application/vnd.in-toto+json",
      payload: Buffer.from(JSON.stringify(statement(artifact))).toString(
        "base64",
      ),
    },
  };
  const bundleBytes = Buffer.from(JSON.stringify(bundle));
  return {
    identity,
    loaded: {
      root: "/local",
      path: "/local/manifest.json",
      manifest: {
        version: 1,
        sourceCommit: identity.sourceCommit,
        artifacts: [artifact],
      },
      artifacts: [{ artifact, bytes, path }],
    },
    artifacts: [
      {
        artifact,
        bytes,
        path,
        bundle,
        bundleBytes,
        bundlePath: `${path}.sigstore.json`,
      },
    ],
    dispose: async () => {},
  };
}
await test(
  "packed repository identities match signing policy across npm URL forms and preserve owner/project case",
  { timeout: 60000 },
  async () => {
    const directory = await mkdtemp(
        join(tmpdir(), "chainman-npm-repositories-"),
      ),
      policy = { ...configured, repository: "Example/Package" };
    const accepted: unknown[] = [
      "https://github.com/Example/Package",
      "https://GITHUB.COM/Example/Package.git",
      "git+https://github.com/Example/Package.git",
      "git@github.com:Example/Package.git",
      "ssh://git@github.com/Example/Package.git",
      "git+ssh://git@github.com/Example/Package.git",
      "github:Example/Package",
      "Example/Package",
      {
        type: "git",
        url: "https://github.com/Example/Package.git",
        directory: "packages/fixture",
      },
      { url: "github:Example/Package", directory: "another/package" },
    ];
    const rejected: unknown[] = [
      undefined,
      null,
      "",
      "not a repository",
      {},
      { type: "git" },
      { url: 42 },
      [],
      42,
      "https://github.com/Example",
      "https://github.com/Example/Package/extra",
      "https://github.com/Example/%ZZ",
      "https://github.com/Other/Package",
      "https://github.com/Example/Other",
      "https://gitlab.com/Example/Package",
      "https://github.example/Example/Package",
      "https://github.com/example/Package",
      "https://github.com/Example/package",
    ];
    try {
      for (const [index, repository] of [...accepted, ...rejected].entries()) {
        const name = `@example/repository-${index}`,
          packed = await pack(directory, name, { repository }),
          frozen = join(directory, `frozen-${index}`);
        await mkdir(frozen);
        const snapshot = snapshotPackages(
          verified(packed.bytes, name, packed.version),
          frozen,
          policy,
        );
        if (index < accepted.length) {
          assert.equal((await snapshot)[0]?.name, name);
        } else {
          await assert.rejects(
            snapshot,
            (error) =>
              error instanceof Error &&
              error.message.includes(`${name}@${packed.version}`) &&
              error.message.includes("https://github.com/Example/Package"),
          );
        }
      }
    } finally {
      await rm(directory, { recursive: true, force: true });
    }
  },
);
await test("a later packed repository mismatch prevents every registry lookup and upload", async () => {
  const directory = await mkdtemp(join(tmpdir(), "chainman-npm-preflight-")),
    calls: string[] = [];
  const registry: Registry = {
    lookup(pkg) {
      calls.push(`lookup:${pkg.name}`);
      return Promise.resolve(undefined);
    },
    publish(pkg) {
      calls.push(`upload:${pkg.name}`);
      return Promise.resolve();
    },
    verify(pkg) {
      calls.push(`verify:${pkg.name}`);
      return Promise.resolve();
    },
  };
  try {
    const good = await pack(directory, "@example/good"),
      bad = await pack(directory, "@example/bad", {
        repository: "github:example/other",
      }),
      first = verified(good.bytes, "@example/good", good.version),
      second = verified(bad.bytes, "@example/bad", bad.version),
      fixture: VerifiedManifest = {
        ...first,
        loaded: {
          ...first.loaded,
          manifest: {
            ...first.loaded.manifest,
            artifacts: [
              ...first.loaded.manifest.artifacts,
              ...second.loaded.manifest.artifacts,
            ],
          },
          artifacts: [...first.loaded.artifacts, ...second.loaded.artifacts],
        },
        artifacts: [...first.artifacts, ...second.artifacts],
      },
      frozen = join(directory, "frozen");
    await mkdir(frozen);
    await assert.rejects(
      async () =>
        publish(await snapshotPackages(fixture, frozen, configured), registry),
      /@example\/bad@1\.0\.0-rc\.0.*https:\/\/github\.com\/example\/package/,
    );
    assert.deepEqual(calls, []);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
await test(
  `actual pinned npm CLI publishes exact local tarball with saved provenance to loopback and reconciles native view results (${actualTarball ? "real Sigstore bundle" : "offline signature boundary"})`,
  { timeout: 60000 },
  async () => {
    const directory = await mkdtemp(join(tmpdir(), "chainman-npm-cli-"));
    const npm = await npmAdapter();
    const smokeInputs = [actualTarball, actualBundle, actualRepository];
    if (smokeInputs.some(Boolean) && !smokeInputs.every(Boolean))
      throw new Error(
        "Actual smoke tarball, saved bundle and signing repository are all required",
      );
    const snapshotPolicy = policy({
      ...configured,
      repository: actualRepository ?? configured.repository,
    });
    const actualMetadata = actualTarball
      ? await npm.package(actualTarball)
      : undefined;
    const packageName = actualMetadata
        ? string(actualMetadata["name"])
        : "@example/fixture",
      version = actualMetadata
        ? string(actualMetadata["version"])
        : "1.0.0-rc.0";
    const scope = packageName.startsWith("@")
      ? packageName.slice(0, packageName.indexOf("/"))
      : undefined;
    let registryUrl: URL,
      uploaded: Record<string, unknown> | undefined,
      uploads = 0,
      wrongReadback = false,
      requests = 0;
    const server = createServer(async (request, response) => {
      requests++;
      response.setHeader("Content-Type", "application/json");
      const path = decodeURIComponent(request.url ?? "");
      if (request.method === "PUT") {
        const chunks: Buffer[] = [];
        for await (const chunk of request)
          chunks.push(Buffer.from(chunk as Uint8Array));
        uploaded = object(
          JSON.parse(Buffer.concat(chunks).toString()) as unknown,
        );
        uploads++;
        response.statusCode = 201;
        response.end('{"ok":true}');
        return;
      }
      if (path.includes("/-/npm/v1/attestations/")) {
        const attachments = object(uploaded?.["_attachments"]),
          provenance = Object.entries(attachments).find(([name]) =>
            name.endsWith(".sigstore"),
          );
        response.end(
          JSON.stringify({
            attestations: [
              {
                predicateType,
                bundle: wrongReadback
                  ? {}
                  : (JSON.parse(
                      string(object(provenance?.[1])["data"]),
                    ) as unknown),
              },
            ],
          }),
        );
        return;
      }
      if (path.includes("/-/")) {
        const attachment = Object.entries(
          object(uploaded?.["_attachments"]),
        ).find(([name]) => name.endsWith(".tgz"));
        response.setHeader("Content-Type", "application/octet-stream");
        response.end(
          Buffer.from(string(object(attachment?.[1])["data"]), "base64"),
        );
        return;
      }
      if (!uploaded) {
        response.statusCode = 404;
        response.end('{"error":"not_found"}');
        return;
      }
      const versions = object(uploaded["versions"]),
        published = object(versions[version]),
        dist = object(published["dist"]);
      response.end(
        JSON.stringify({
          ...uploaded,
          "dist-tags": { next: version },
          versions: {
            [version]: {
              ...published,
              dist: {
                ...dist,
                tarball: new URL("fixture/-/fixture.tgz", registryUrl).href,
                attestations: {
                  url: new URL("-/npm/v1/attestations/fixture", registryUrl)
                    .href,
                  provenance: { predicateType },
                },
              },
            },
          },
        }),
      );
    });
    try {
      registryUrl = await listening(server);
      const packed = actualTarball
        ? { bytes: await readFile(actualTarball), version }
        : await pack(directory, packageName);
      let fixture = verified(packed.bytes, packageName, version);
      if (actualBundle) {
        const bundleBytes = await readFile(actualBundle),
          bundle: unknown = JSON.parse(bundleBytes.toString());
        fixture = {
          ...fixture,
          artifacts: fixture.artifacts.map((selected) => ({
            ...selected,
            bundleBytes,
            bundle,
          })),
        };
      }
      const frozen = join(directory, "frozen");
      await mkdir(frozen);
      const packages = await snapshotPackages(fixture, frozen, snapshotPolicy),
        pkg = packages[0];
      assert.ok(pkg);
      const require = createRequire(import.meta.url),
        npmPackagePath = require.resolve("npm/package.json");
      const preload = join(directory, "fixture-trust.cjs"),
        marker = join(directory, "trust-calls.jsonl"),
        config = join(directory, "npmrc");
      // This offline test isolates the already-verified signature boundary. It proves
      // native npm subject checks, upload serialization and readback, not Sigstore trust.
      await writeFile(
        preload,
        `const fs=require('node:fs');const r=require('node:module').createRequire(${JSON.stringify(npmPackagePath)});Object.defineProperty(r('sigstore'),'verify',{value:async b=>{fs.appendFileSync(${JSON.stringify(marker)},JSON.stringify(b)+'\\n')}});`,
      );
      await writeFile(
        config,
        `registry=https://untrusted.invalid/\n${scope ? `${scope}:registry=https://untrusted.invalid/\n` : ""}//${registryUrl.host}/:_authToken=synthetic-loopback-token\n`,
      );
      const commands: readonly string[][] = [];
      const adapter = await npmRegistry({
        registry: registryUrl,
        attempts: 1,
        delayMs: 0,
        command: async (program, args) => {
          (commands as string[][]).push([...args]);
          const result = await command(
            program,
            [...(actualBundle ? [] : ["--require", preload]), ...args],
            false,
            {
              ...process.env,
              NPM_CONFIG_USERCONFIG: config,
              NPM_CONFIG_CACHE: join(directory, "cache"),
              NPM_CONFIG_FETCH_RETRIES: "0",
            },
          );
          if (args[1] === "publish")
            assert.equal(result.status, 0, result.stderr);
          return result;
        },
      });
      assert.equal(
        await adapter.lookup(pkg),
        undefined,
        "real npm JSON E404 permits absent version",
      );
      await publish(packages, adapter);
      assert.equal(uploads, 1);
      assert.ok(uploaded);
      assert.deepEqual(
        uploaded["dist-tags"],
        { next: version },
        "explicit next overrides publishConfig latest",
      );
      const attachments = object(uploaded["_attachments"]),
        tgz = Object.entries(attachments).find(([name]) =>
          name.endsWith(".tgz"),
        ),
        provenance = Object.entries(attachments).find(([name]) =>
          name.endsWith(".sigstore"),
        );
      assert.deepEqual(
        Buffer.from(string(object(tgz?.[1])["data"]), "base64"),
        packed.bytes,
      );
      assert.deepEqual(
        JSON.parse(string(object(provenance?.[1])["data"])) as unknown,
        pkg.bundle,
      );
      if (!actualBundle)
        assert.equal(
          (await readFile(marker, "utf8")).trim().split("\n").length,
          1,
          "native npm invoked its signature-verification boundary",
        );
      const native = await command(
        process.execPath,
        [
          npm.cliPath,
          "view",
          `${packageName}@${version}`,
          "dist",
          "--json",
          "--registry",
          registryUrl.href,
          ...(scope ? [`--${scope}:registry=${registryUrl.href}`] : []),
          "--loglevel=silent",
        ],
        false,
        {
          ...process.env,
          NPM_CONFIG_USERCONFIG: config,
          NPM_CONFIG_CACHE: join(directory, "cache"),
        },
      );
      assert.equal(native.status, 0, native.stderr);
      assert.ok(
        Array.isArray(JSON.parse(native.stdout) as unknown),
        "npm12 exact-version view returns an array",
      );
      await publish(packages, adapter);
      assert.equal(
        uploads,
        1,
        "matching published version resumes without upload",
      );
      wrongReadback = true;
      await assert.rejects(
        publish(packages, adapter),
        /differs from the exact verified bundle/,
      );
      assert.equal(uploads, 1);
      assert.ok(requests > 5);
      assert.ok(
        commands.some(
          (args) =>
            args.includes("--ignore-scripts") &&
            args.includes("--provenance-file") &&
            (!scope ||
              args.includes(`--${scope}:registry=${registryUrl.href}`)),
        ),
      );
      const bad = await pack(directory, "@example/bad-tag", { tag: "latest" }),
        badFrozen = join(directory, "bad-frozen");
      await mkdir(badFrozen);
      await assert.rejects(
        snapshotPackages(
          verified(bad.bytes, "@example/bad-tag", bad.version),
          badFrozen,
          configured,
        ),
        /next publication policy/,
      );
      assert.equal(
        uploads,
        1,
        "top-level latest tag rejected before any registry mutation",
      );
    } finally {
      await close(server);
      await rm(directory, { recursive: true, force: true });
    }
  },
);
