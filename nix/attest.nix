{
  pkgs,
  nodejs,
  pnpm,
}:
pkgs.stdenvNoCC.mkDerivation (finalAttrs: {
  pname = "chainman-attest";
  version = "0.1.0";
  src = pkgs.lib.cleanSourceWith {
    src = ./attest;
    filter =
      path: type:
      !(builtins.elem (baseNameOf path) [
        "node_modules"
        "dist"
        ".pnpm-store"
      ]);
  };
  pnpmDeps = pkgs.fetchPnpmDeps {
    inherit (finalAttrs) pname version src;
    inherit pnpm;
    fetcherVersion = 5;
    hash = "sha256-NDms50xBvQqco6sTl3EsVj+17WvVM3WnPwNGljsTZ2M=";
  };
  nativeBuildInputs = [
    nodejs
    pnpm
    pkgs.pnpmConfigHook
    pkgs.makeWrapper
  ];
  buildPhase = ''
    runHook preBuild
    pnpm run check
    pnpm run build
    # Prune only the disposable Nix staging tree, after compiling with dev tools.
    mkdir runtime
    cp -r node_modules package.json pnpm-lock.yaml runtime/
    (
      cd runtime
      pnpm_config_offline=true pnpm_config_frozen_lockfile=true \
        pnpm prune --prod --ignore-scripts
      # Runtime resolution needs the links, not pnpm's build/store metadata.
      rm -f node_modules/.modules.yaml node_modules/.pnpm-workspace-state-v1.json \
        node_modules/.pnpm/lock.yaml
      rmdir node_modules/@types node_modules/@typescript 2>/dev/null || true
    )
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/chainman-attest/dist" "$out/bin"
    cp -r dist/src "$out/lib/chainman-attest/dist/"
    cp -r runtime/node_modules runtime/package.json "$out/lib/chainman-attest/"
    makeWrapper ${nodejs}/bin/node "$out/bin/chainman-attest-adapter" \
      --add-flags "$out/lib/chainman-attest/dist/src/adapter.js" \
      --unset NODE_PATH --set NODE_OPTIONS --use-openssl-ca
    makeWrapper ${nodejs}/bin/node "$out/bin/chainman-attest-npm" \
      --add-flags "$out/lib/chainman-attest/node_modules/npm/bin/npm-cli.js" \
      --unset NODE_PATH --set NODE_OPTIONS --use-openssl-ca
    # Development packages include TypeScript 7's platform native compiler.
    test ! -e "$out/lib/chainman-attest/node_modules/typescript"
    test ! -e "$out/lib/chainman-attest/node_modules/@typescript"
    test ! -e "$out/lib/chainman-attest/node_modules/@types/node"
    test ! -e "$out/lib/chainman-attest/node_modules/@types/yauzl"
    test ! -e "$out/lib/chainman-attest/node_modules/.bin/tsc"
    test ! -e "$out/lib/chainman-attest/dist/test"
    test ! -e "$out/lib/chainman-attest/src"
    test ! -e "$out/lib/chainman-attest/test"
    if find "$out/lib/chainman-attest/node_modules/.pnpm" -mindepth 1 -maxdepth 1 \
      \( -name 'typescript@*' -o -name '@typescript+typescript-*' \
        -o -name '@types+node@*' -o -name '@types+yauzl@*' \) \
      -print -quit | grep -q .; then
      echo 'Development compiler or types leaked into the attestation runtime' >&2
      exit 1
    fi
    if find "$out/lib/chainman-attest/node_modules" -name nativecompiler \
      -print -quit | grep -q .; then
      echo 'Development compiler or types leaked into the attestation runtime' >&2
      exit 1
    fi
    runHook postInstall
  '';
  doInstallCheck = true;
  installCheckPhase = ''
    runHook preInstallCheck
    npmVersion=$(${nodejs}/bin/node -p \
      'JSON.parse(require("fs").readFileSync("package.json", "utf8")).dependencies.npm')
    mkdir -m 700 smoke
    mkdir -p smoke/archive/package
    cat > smoke/archive/package/package.json <<'JSON'
    {"name":"@example/pkg","version":"1.0.0-rc.0","repository":{"type":"git","url":"git+https://github.com/Example/Repo.git"}}
    JSON
    tar -czf "$PWD/smoke/package.tgz" -C smoke/archive package
    # Both wrappers must ignore caller preload hooks and resolve exact store tools.
    export NODE_PATH=/not-a-node-module-path NODE_OPTIONS=--require=/not-a-node-preload
    test "$(PATH=/not-a-command-path "$out/bin/chainman-attest-npm" --version)" = "$npmVersion"
    printf '{"protocol":1,"operation":"npm-inspect","tarball":"%s"}\n' "$PWD/smoke/package.tgz" | \
      PATH=/not-a-command-path "$out/bin/chainman-attest-adapter" \
        --response-file "$PWD/smoke/response.json"
    env -u NODE_PATH NODE_OPTIONS=--use-openssl-ca \
      ${nodejs}/bin/node --input-type=module - "$PWD/smoke/response.json" <<'JS'
    import assert from 'node:assert/strict';
    import { lstatSync, readFileSync } from 'node:fs';
    const path = process.argv[2];
    const response = JSON.parse(readFileSync(path, 'utf8'));
    assert.equal(lstatSync(path).mode & 0o777, 0o600);
    assert.equal(response.protocol, 1);
    assert.equal(response.operation, 'npm-inspect');
    assert.equal(response.manifest.name, '@example/pkg');
    assert.equal(response.manifest.version, '1.0.0-rc.0');
    assert.equal(response.subject, 'pkg:npm/%40example/pkg@1.0.0-rc.0');
    assert.equal(response.repository, 'github.com/Example/Repo');
    JS
    runHook postInstallCheck
  '';
  meta.mainProgram = "chainman-attest-adapter";
})
