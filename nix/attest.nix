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
    hash = "sha256-k+JLzEWWQiwzO8vgN/OxdRF4XHy01PdL2VFmycm0lZw=";
  };
  nativeBuildInputs = [
    nodejs
    pnpm
    pkgs.pnpmConfigHook
    pkgs.makeWrapper
  ];
  buildPhase = ''
    runHook preBuild
    pnpm run build
    runHook postBuild
  '';
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/chainman-attest" "$out/bin"
    cp -r dist node_modules package.json "$out/lib/chainman-attest/"
    makeWrapper ${nodejs}/bin/node "$out/bin/chainman-attest" \
      --add-flags "$out/lib/chainman-attest/dist/src/cli.js" \
      --set CHAINMAN_ATTEST_GH ${pkgs.gh}/bin/gh
    runHook postInstall
  '';
  meta.mainProgram = "chainman-attest";
})
