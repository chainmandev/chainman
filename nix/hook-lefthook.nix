{ pkgs, target }:
let
  parts = pkgs.lib.splitString "-" target;
in
pkgs.lefthook.overrideAttrs (old: {
  pname = "chainman-lefthook-${target}";
  env = (old.env or { }) // {
    CGO_ENABLED = "0";
  };
  buildPhase = ''
    export GOOS=${builtins.elemAt parts 0} GOARCH=${builtins.elemAt parts 1}
    go build -trimpath -ldflags='-s -w' -o lefthook .
  '';
  installPhase = ''
    mkdir -p "$out/bin" "$out/share/licenses/lefthook"
    install -m755 lefthook "$out/bin/lefthook"
    install -m644 LICENSE "$out/share/licenses/lefthook/LICENSE"
  '';
  postInstall = "";
})
