{ pkgs, target }:
let
  assets = {
    linux-arm64 = "c5f4fcfc63e849279ac531bce2394a918fb28746339088a7d3d02bb5fb218a68";
    linux-amd64 = "3792e1ed9f383832eb2362154444e8564fbc8e7e8e7cff8754c68aea5eca086e";
    darwin-arm64 = "21c83530a13e156d207be17db4d9fe6b76629ca9ac33560554cf2f416b1d8e4b";
    darwin-amd64 = "844756b341772fdcb9f4abe076c041077cde0128e5c06d4afa0c1d41bc316663";
  };
  parts = pkgs.lib.splitString "-" target;
  archive = pkgs.fetchurl {
    url = "https://github.com/F1bonacc1/process-compose/releases/download/v1.120.0/process-compose_${builtins.elemAt parts 0}_${builtins.elemAt parts 1}.tar.gz";
    sha256 = assets.${target};
  };
in
pkgs.buildGoModule.override { go = pkgs.go_latest; } {
  pname = "chainman-control-${target}";
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
    tar -xzf ${archive} -C "$TMPDIR" process-compose LICENSE
    install -m755 "$TMPDIR/process-compose" "$out/bin/process-compose"
    install -m644 "$TMPDIR/LICENSE" "$out/share/licenses/process-compose/LICENSE"
    mkdir -p "$out/share/licenses/golang.org-x-sys"
    install -m644 vendor/golang.org/x/sys/LICENSE "$out/share/licenses/golang.org-x-sys/LICENSE"
  '';
}
