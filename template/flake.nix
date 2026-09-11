{
  description = "Shared Chainman SDK shell profiles";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs =
    inputs:
    let
      runtime = import ./scripts/chainman-fetch.nix {
        root = toString ./.;
        action = "fetch";
      };
    in
    (import (builtins.toPath (runtime + "/nix/flake.nix"))).outputs inputs;
}
