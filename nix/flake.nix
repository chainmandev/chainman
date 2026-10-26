{
  description = "Development shells for a small modular project";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  # Current unstable no longer provides Intel macOS packages. Keep its supported
  # compatibility channel explicit and independently locked/updated.
  inputs.nixpkgs-darwin.url = "github:NixOS/nixpkgs/nixpkgs-26.05-darwin";
  outputs =
    {
      nixpkgs,
      nixpkgs-darwin ? nixpkgs,
      ...
    }:
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
            value = f (
              (if system == "x86_64-darwin" then nixpkgs-darwin else nixpkgs).legacyPackages.${system}
            );
          }) systems
        );
    in
    {
      devShells = each (
        pkgs:
        let
          # The Intel macOS compatibility channel lags these security fixes.
          # Reuse the reviewed package definitions with its supported stdenv.
          uv =
            if pkgs.lib.versionOlder pkgs.uv.version "0.13.4" then
              pkgs.callPackage (nixpkgs + "/pkgs/by-name/uv/uv/package.nix") {
                # uv 0.13 requires Rust >= 1.97; the compatibility channel's
                # default compiler remains 1.95, but it also supplies 1.98.
                rustPlatform = pkgs.rustPackages_1_98.rustPlatform;
              }
            else
              pkgs.uv;
          trustedCacert = pkgs.callPackage (nixpkgs + "/pkgs/by-name/ca/cacert/package.nix") {
            # NSS 3.131.1 already removed the three affected emSign roots.
            # Complete the NixOS CVE-2026-111943 mitigation with the remaining root.
            blacklist = [
              "Comodo AAA Services root"
            ];
          };
          emptyCertDirectory = pkgs.runCommand "chainman-empty-ca-directory" { } "mkdir -p $out";
          pythonPackages = p: [
            p.packaging
            p.tomlkit
            p.pyyaml
            p.semantic-version
            p.ruamel-yaml
          ];
          python = pkgs.python3.withPackages pythonPackages;
          testPython = pkgs.python3.withPackages (
            p:
            pythonPackages p
            ++ [
              p.hypothesis
              p.types-pyyaml
            ]
          );
          # Package wrappers otherwise embed nixpkgs' default Node even when a
          # newer node executable leads PATH, splitting engine checks from builds.
          nodejs = pkgs.nodejs_latest;
          # Retain the supported Node-based line on every platform. pnpm 12
          # introduces a separate native TLS client with platform trust defaults.
          pnpm = pkgs.pnpm_11.override { nodejs-slim = nodejs; };
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
          # Use the upstream library directly; its optional CLI dependencies are
          # not needed to inspect literal Git blobs. npm tarball is hash-pinned.
          trojanSource = pkgs.stdenvNoCC.mkDerivation {
            pname = "anti-trojan-source";
            version = "1.12.1";
            src = pkgs.fetchurl {
              url = "https://registry.npmjs.org/anti-trojan-source/-/anti-trojan-source-1.12.1.tgz";
              hash = "sha256-KjA3s/WevVXpqXJjFyAhDQldQ+bhP8lcuNwyGapAr0g=";
            };
            installPhase = "mkdir -p $out; cp -r src package.json $out/";
          };
          runtimeBase = with pkgs; [
            python3
            git
            just
            bash
            coreutils
            gnugrep
            gnused
            gawk
            findutils
            diffutils
            curl
            trustedCacert
          ];
          updateBase = [ python ] ++ runtimeBase;
          base = [
            testPython
          ]
          ++ runtimeBase
          ++ (with pkgs; [
            nixfmt
            shellcheck
            shfmt
            ruff
            mypy
            actionlint
          ]);
          shellWith =
            builder: base: name: packages: extra:
            builder {
              packages = base ++ packages;
              # Development has no installed output; its dummy path can contain
              # spaces that compiler wrappers cannot represent in linker flags.
              NIX_NO_SELF_RPATH = "1";
              shellHook = ''
                export PATH=${pkgs.lib.makeBinPath (base ++ packages)}${
                  pkgs.lib.optionalString (name == "swift" && pkgs.stdenv.hostPlatform.isDarwin) ":/usr/bin:/bin"
                }
                export TOOLCHAIN_ACTIVE_PROFILE=${name}
                # The launcher selects host/image Nix before entering this shell.
                if [ -n "''${CHAINMAN_RUNTIME_NIX_BIN:-}" ]; then
                  export PATH="$CHAINMAN_RUNTIME_NIX_BIN:$PATH"
                fi
                export PYTHONDONTWRITEBYTECODE=1
                # Use the same reviewed store across OpenSSL, embedded-bundle
                # clients and bootstrap downloads, including Intel macOS.
                # Standard PEM omits distrusted roots instead of relying on
                # OpenSSL-specific rejection attributes that other clients ignore.
                export SSL_CERT_FILE=${trustedCacert}/etc/ssl/certs/ca-no-trust-rules-bundle.crt
                export SSL_CERT_DIR=${emptyCertDirectory}
                export NIX_SSL_CERT_FILE="$SSL_CERT_FILE"
                export CURL_CA_BUNDLE="$SSL_CERT_FILE"
                export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
                export GIT_SSL_CAINFO="$SSL_CERT_FILE"
                export CARGO_HTTP_CAINFO="$SSL_CERT_FILE"
                export NPM_CONFIG_CAFILE="$SSL_CERT_FILE"
                export npm_config_cafile="$SSL_CERT_FILE"
                export UV_SYSTEM_CERTS=true
                export NODE_OPTIONS="--use-openssl-ca ''${NODE_OPTIONS:-}"
                export UV_PYTHON_DOWNLOADS=never
                UV_PYTHON=$(command -v python3)
                export UV_PYTHON
                export GOTOOLCHAIN=local
                export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
                # Repeated shell refreshes must not nest browser socket paths.
                # With no caller override, retain Nix's first scoped directory.
                if [ -n "''${CHAINMAN_TEMP_BASE:-}" ]; then
                  export TMPDIR="$CHAINMAN_TEMP_BASE"
                elif [ -n "''${TMPDIR:-}" ]; then
                  export CHAINMAN_TEMP_BASE="$TMPDIR"
                fi
                ulimit -c 0 2>/dev/null || true
                ${extra}
              '';
            };
          shell = shellWith pkgs.mkShell base;
        in
        {
          # Consumer entry needs the runtime, not Chainman's source formatters or
          # a C compiler. Language toolchains still come from the chosen profile.
          bootstrap = shellWith pkgs.mkShellNoCC runtimeBase "bootstrap" [ ] "";
          hooks = shellWith pkgs.mkShellNoCC runtimeBase "hooks" [ pkgs.lefthook nodejs ] ''
            export CHAINMAN_TROJAN_SOURCE=${trojanSource}/src/main.js
          '';
          format-text = shellWith pkgs.mkShellNoCC runtimeBase "format-text" [
            prettier
            pkgs.ruff
            pkgs.taplo
            pkgs.shfmt
            pkgs.nixfmt
          ] "";
          format-rust = shellWith pkgs.mkShellNoCC runtimeBase "format-rust" [ pkgs.rustfmt ] "";
          updates = shellWith pkgs.mkShellNoCC updateBase "updates" [ ] "";
          release = shellWith pkgs.mkShellNoCC (updateBase ++ [ pkgs.gh ]) "release" [ ] "";
          core = shell "core" [ ] "";
          trust = shellWith pkgs.mkShellNoCC updateBase "trust" [ nodejs pnpm uv pkgs.openssl ] "";
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
          python = shell "python" [ uv pkgs.ruff ] "";
          go = shell "go" [ pkgs.go_latest pkgs.stdenv.cc ] "";
          control = shell "control" [ pkgs.go_latest pkgs.stdenv.cc pkgs.lefthook ] "";
          flutter = shell "flutter" [ flutterPkgs.flutter ] "";
          swift =
            shellWith (if pkgs.stdenv.hostPlatform.isDarwin then pkgs.mkShellNoCC else pkgs.mkShell) base
              "swift"
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
                + pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''
                  # Use one selected Xcode for the native compiler and its SDK.
                  # Nix's Apple SDK hook also sets DEVELOPER_DIR. xcrun honors
                  # that inherited value even when --sdk macosx is explicit.
                  # xcode-select itself honors the same environment override.
                  unset DEVELOPER_DIR SDKROOT
                  DEVELOPER_DIR=$(/usr/bin/xcode-select --print-path)
                  export DEVELOPER_DIR
                  SDKROOT=$(/usr/bin/xcrun --sdk macosx --show-sdk-path)
                  CC=$(/usr/bin/xcrun --find clang)
                  CXX=$(/usr/bin/xcrun --find clang++)
                  export SDKROOT CC CXX
                ''
              );
          compose = shell "compose" [ pkgs.gradle pkgs.jdk21 pkgs.ktlint ] "";
          browser = shell "browser" [ nodejs pnpm pkgs.playwright-driver.browsers ] ''
            export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
          '';
        }
      );
      packages = each (
        pkgs:
        {
          just = pkgs.just;
          bash = pkgs.bashInteractive;
          audit-rust = pkgs.cargo-deny;
          audit-go = pkgs.govulncheck;
          # pip-api launches Python separately, without inheriting sys.path.
          audit-python = pkgs.pip-audit.overridePythonAttrs (old: {
            makeWrapperArgs = (old.makeWrapperArgs or [ ]) ++ [
              "--set"
              "PIPAPI_PYTHON_LOCATION"
              "${pkgs.python3.withPackages (p: [ p.pip ])}/bin/python3"
            ];
          });
        }
        // builtins.listToAttrs (
          builtins.concatMap
            (target: [
              {
                name = "control-${target}";
                value = import ./control.nix { inherit pkgs target; };
              }
              {
                name = "hook-lefthook-${target}";
                value = import ./hook-lefthook.nix { inherit pkgs target; };
              }
              {
                name = "task-${target}";
                value = import ./control.nix {
                  inherit pkgs target;
                  withBackends = false;
                };
              }
            ])
            [
              "linux-arm64"
              "linux-amd64"
              "darwin-arm64"
              "darwin-amd64"
            ]
        )
      );
      formatter = each (pkgs: pkgs.nixfmt);
    };
}
