{
  pkgs,
  target,
  withBackends ? true,
}:
let
  sources = builtins.fromJSON (builtins.readFile ./control-sources.json);
  parts = pkgs.lib.splitString "-" target;
  archive = pkgs.fetchurl {
    url = "https://github.com/F1bonacc1/process-compose/releases/download/v${sources.process-compose.version}/process-compose_${builtins.elemAt parts 0}_${builtins.elemAt parts 1}.tar.gz";
    sha256 = sources.process-compose.hashes.${target};
  };
  watchTarget =
    (if builtins.elemAt parts 1 == "arm64" then "aarch64" else "x86_64")
    + (if builtins.elemAt parts 0 == "linux" then "-unknown-linux-musl" else "-apple-darwin");
  watchArchive = pkgs.fetchurl {
    url = "https://github.com/watchexec/watchexec/releases/download/v${sources.watchexec.version}/watchexec-${sources.watchexec.version}-${watchTarget}.tar.xz";
    sha256 = sources.watchexec.hashes.${target};
  };
in
pkgs.buildGoModule.override { go = pkgs.go_latest; } {
  pname = "chainman-${if withBackends then "control" else "task"}-${target}";
  version = "0.1.0";
  src = ./control;
  vendorHash = "sha256-Np+MQ+oy8nyCBIT1ivJyt0sRpxgGkwGs8M9Je4oLt1I=";
  doCheck = false;
  env.CGO_ENABLED = "0";
  buildPhase = ''
    export GOOS=${builtins.elemAt parts 0} GOARCH=${builtins.elemAt parts 1}
    go build -trimpath -ldflags='-s -w' -o chainman-control .
  '';
  installPhase = ''
    mkdir -p "$out/bin" "$out/share/licenses/process-compose"
    install -m755 chainman-control "$out/bin/chainman-control"
    ${pkgs.lib.optionalString withBackends ''
      tar -xzf ${archive} -C "$TMPDIR" process-compose LICENSE
      install -m755 "$TMPDIR/process-compose" "$out/bin/process-compose"
      install -m644 "$TMPDIR/LICENSE" "$out/share/licenses/process-compose/LICENSE"
      mkdir -p "$TMPDIR/watch" "$out/share/licenses/watchexec"
      tar -xJf ${watchArchive} -C "$TMPDIR/watch" --strip-components=1
      install -m755 "$TMPDIR/watch/watchexec" "$out/bin/watchexec"
      install -m644 "$TMPDIR/watch/LICENSE" "$out/share/licenses/watchexec/LICENSE"
    ''}
    mkdir -p "$out/share/licenses/golang.org-x-sys"
    install -m644 vendor/golang.org/x/sys/LICENSE "$out/share/licenses/golang.org-x-sys/LICENSE"
  '';
}
