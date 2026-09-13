{
  description = "Shared Chainman SDK shell profiles";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
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
