# Nix/just toolchain

Copy this entire directory, including dotfiles, into a new project root. It contains
a small executable example, optional language modules, pinned Nix inputs and a
pinned Chainman runtime. It works independently of the directory it came from.
Keep the modules you need and adapt application paths and commands.

## Start

Install Git, `just`, and Docker or Podman. On macOS the engine uses its Linux VM;
on Windows use WSL2 and keep the checkout in its Linux filesystem.

```sh
just setup
just exec python3 examples/core/greeting.py
just verify
```

Containerized Nix is the default. Select Podman with
`CHAINMAN_CONTAINER_ENGINE=podman just verify`. For host Nix, install Nix and use
`CHAINMAN_MODE=host-nix just verify`. No host language toolchain or global Chainman
command is needed. `just --justfile '/path with spaces/project/justfile' verify`
also works from another directory.

The checked-in launcher verifies `chainman.lock` before executing the immutable
runtime in the Nix store. The bundled `vendor/chainman/chainman.tar.gz` matches that pin
and allows installation before its public URL is available. Keep the bootstrap
companions unchanged. The root `flake.nix` imports SDK profiles from that same
verified runtime using this project’s own `flake.lock`; the project does not copy
Chainman’s Nix implementation or service controller. Project extensions belong in
`chainman.toml`, modules or
project-owned scripts. The intended upstream is github.com/chainmandev/chainman,
with future public home chainman.dev. Publication is separate from local adoption.

## Commands

| Command | Purpose |
|---|---|
| `just setup` | Prepare selected modules, reusing valid fingerprints and readiness artifacts |
| `just exec COMMAND...` | Execute literal arguments in the project's core profile |
| `just shell --profile rust -- COMMAND...` / `just shell --profile rust` | Select one language profile |
| `just build` / `just test` / `just verify` | Run selected module commands |
| `just format` / `just format-check` | Generate, format, check and commit / check formatting |
| `just module NAME verify` | Exercise an optional module without enabling it globally |
| `just deps-update mode=dry-run` | Resolve and verify a disposable copy |
| `just deps-update` | Update project dependencies, verify, commit locally |
| `just deps-update commit=off` | Leave a verified update for coordinated review |
| `just chainman-update` | Update the runtime, managed bootstrap and bundled archive |
| `just cache-status` / `just cache-prune` | Report disk use or prune stale managed build contexts |
| `just clean` | Explicitly remove managed build contexts |
| `just doctor` | Report selected project, runtime, mode and modules |
| `just sdk-doctor apple` / `just sdk-doctor android` | Check explicit native SDK prerequisites |
| `just ci-prune --module flutter` | Preview guarded disposable hosted-runner SDK cleanup |

Full project updates include the Chainman runtime pin. Explicit application targets
retain it, as does `just deps-update --skip-chainman`. `just chainman-update` explicitly
updates it; an unavailable release fails rather than silently skipping that request.
Initialize and commit this directory as its own Git project before applying updates.
A nested example refuses to adopt its enclosing repository. Preview performs real
resolution and verification, discards the copy, and never commits to the original.
Automatic commits require a clean repository, cover only declared verified files,
bypass hooks after verification, preserve Git identity/signing and never push. See [update policy](../docs/updates.md).

## Optional modules

Select `modules = ["core", "rust"]` in `chainman.toml`. Profiles load their language
tools only when invoked. Each module declares manifest inputs, generated readiness
artifacts, frozen setup, resolution, verification and update-output scope.

| Module | Example |
|---|---|
| javascript | [pnpm workspace, TypeScript, Node tests and Prettier](../examples/javascript/README.md) |
| rust | [Cargo workspace, library, CLI, fmt/clippy/tests](../examples/rust/README.md) |
| python | [uv workspace, locked build backends, package tests and Ruff](../examples/python/README.md) |
| go | [Go workspace, shared module and executable](../examples/go/README.md) |
| flutter | [Dart/Flutter widget, analysis, tests and release assets](../examples/flutter/README.md) |
| swift | [SwiftPM shared library and conditional SwiftUI shell](../examples/swift/README.md) |
| compose | [Gradle/Kotlin logic and Compose desktop shell](../examples/compose/README.md) |

`sdk-versions.toml` coordinates tool versions and manifest targets; `dependencies.toml`
contains explicit pins, constraints, age policy exceptions and registry sources.
Updates target the latest eligible stable releases, including majors, after the
configurable 30-day window. Nix branch inputs use commit age, not an SDK release date.
Missing artifact identity or age evidence fails. Verification follows fresh shell
entry after changed toolchain inputs. Lockfiles and package-manager pins remain
project-owned. The core's `scripts/demo.py` shows a check-only generated asset and a
deterministic archive under `dist/`; verification fails if committed labels are stale.

## Caches, native lanes and extensions

Downloads/compiler caches are shared separately from project build outputs.
`cache` configures build/compiler limits and stale age. Managed operations hold a
project lock across child commands and foreground compiler-cache lifetimes. Cleanup
rejects symlink escapes and reports deletion errors; it does not collect the host
Nix store or remove SDKs. Application outputs outside managed contexts, such as
`dist/`, remain application-owned. See [runtime contract](../docs/runtime.md).

The manual workflow uses the same verification commands locally and in CI, including
both container engines and an Apple lane. Apple SDKs require macOS/Xcode and host Nix.
Android packaging needs a compatible Android SDK, licenses and a device/emulator lane.
Linux Swift checks do not qualify SwiftUI; a Linux ARM Flutter wrapper does not supply
a working ARM aapt2. Native Windows packaging needs a separate native SDK lane when
an application requires it. A lane's existence is not evidence that it has run.

Add command arrays, project profiles and explicit environment/mount declarations as
shown in [configuration](../docs/configuration.md). Keep architecture choices and native
application shells in your project's requirements. The shared tooling supplies
repeatable execution; the project owns its application behavior and acceptance.

The shared [recipe contract](../docs/recipes.md) also supplies staged formatting,
inspection, dependency coverage, vulnerability audits and service recovery.
