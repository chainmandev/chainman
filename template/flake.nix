{
  description = "Project-owned development environment";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  inputs.nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  outputs =
    { nixpkgs, nixpkgs-darwin, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
    in
    {
      devShells = nixpkgs.lib.genAttrs systems (
        system:
        let
          source = if system == "x86_64-darwin" then nixpkgs-darwin else nixpkgs;
          pkgs = import source { inherit system; };
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.git
              pkgs.just
            ];
          };
        }
      );
    };
}
