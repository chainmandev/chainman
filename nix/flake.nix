{
  description = "Development shells for a small modular project";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "aarch64-linux"
        "x86_64-linux"
        "aarch64-darwin"
        "x86_64-darwin"
      ];
      each =
        f:
        builtins.listToAttrs (
          map (system: {
            name = system;
            value = f nixpkgs.legacyPackages.${system};
          }) systems
        );
    in
    {
      devShells = each (
        pkgs:
        let
          python = pkgs.python3.withPackages (p: [
            p.packaging
            p.tomlkit
            p.pyyaml
            p.semantic-version
            p.ruamel-yaml
          ]);
          # Package wrappers otherwise embed nixpkgs' default Node even when a
          # newer node executable leads PATH, splitting engine checks from builds.
          nodejs = pkgs.nodejs_latest;
          pnpm = pkgs.pnpm.override { nodejs-slim = nodejs; };
          prettier = pkgs.prettier.override { inherit nodejs; };
          # Flutter's Linux wrapper references aapt even for non-Android commands.
          # Its aapt2 binary remains x86_64-only; this only enables those commands on ARM.
          flutterPkgs =
            if pkgs.stdenv.hostPlatform.system == "aarch64-linux" then
              pkgs.extend (
                _: prev: {
                  aapt = prev.aapt.overrideAttrs (old: {
                    meta = old.meta // {
                      platforms = prev.lib.unique ((old.meta.platforms or [ ]) ++ [ "aarch64-linux" ]);
                    };
                  });
                }
              )
            else
              pkgs;
          base = with pkgs; [
            python
            git
            just
            nix
            bash
            coreutils
            gnugrep
            gnused
            findutils
            diffutils
            curl
            cacert
            nixfmt
            shellcheck
            shfmt
            ruff
          ];
          shell =
            name: packages: extra:
            pkgs.mkShell {
              packages = base ++ packages;
              # Development has no installed output; its dummy path can contain
              # spaces that compiler wrappers cannot represent in linker flags.
              NIX_NO_SELF_RPATH = "1";
              shellHook = ''
                export PATH=${pkgs.lib.makeBinPath (base ++ packages)}${
                  pkgs.lib.optionalString (name == "swift" && pkgs.stdenv.hostPlatform.isDarwin) ":/usr/bin:/bin"
                }
                export TOOLCHAIN_ACTIVE_PROFILE=${name}
                export PYTHONDONTWRITEBYTECODE=1
                export UV_PYTHON_DOWNLOADS=never
                export UV_PYTHON=${python}/bin/python3
                export GOTOOLCHAIN=local
                export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
                ulimit -c 0 2>/dev/null || true
                ${extra}
              '';
            };
        in
        {
          core = shell "core" [ ] "";
          default = shell "core" [ ] "";
          javascript = shell "javascript" [ nodejs pnpm prettier ] "";
          rust = shell "rust" [
            pkgs.rustc
            pkgs.cargo
            pkgs.clippy
            pkgs.rustfmt
            pkgs.pkg-config
            pkgs.sccache
            pkgs.stdenv.cc
          ] "";
          python = shell "python" [ pkgs.uv pkgs.ruff ] "";
          go = shell "go" [ pkgs.go_latest pkgs.stdenv.cc ] "";
          flutter = shell "flutter" [ flutterPkgs.flutter ] "";
          swift =
            shell "swift"
              (
                [ pkgs.swift-format ]
                ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
                  pkgs.swift
                  pkgs.swiftPackages.swiftpm
                  pkgs.swiftPackages.XCTest
                  pkgs.stdenv.cc
                  pkgs.binutils
                ]
              )
              (
                pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isLinux ''
                  export LD_LIBRARY_PATH=${pkgs.swiftPackages.Dispatch}/lib:${pkgs.swiftPackages.Foundation}/lib/swift/linux:${pkgs.swiftPackages.XCTest}/lib/swift/linux
                  export LIBRARY_PATH=$LD_LIBRARY_PATH
                ''
              );
          compose = shell "compose" [ pkgs.gradle pkgs.jdk21 pkgs.ktlint ] "";
          browser = shell "browser" [ nodejs pnpm pkgs.playwright-driver.browsers ] ''
            export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
          '';
        }
      );
      packages = each (pkgs: {
        just = pkgs.just;
      });
      formatter = each (pkgs: pkgs.nixfmt);
    };
}
