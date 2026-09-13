# Chainman

Chainman keeps shared development machinery behind a project's `just` commands.
Developers install `just`, Git, and either Docker/Podman or Nix. A checked-in launcher
fetches a pinned source archive, verifies its SHA-256 NAR hash through Nix, and executes
the runtime directly from its verified Nix-store source. Python and development languages
come from Nix. Update libraries are loaded only for dependency operations. There is no global Chainman installation.

The intended public home is **chainman.dev**, with source at
**github.com/chainmandev/chainman**. These are publication destinations; local release
artifacts and bundled consumers work before anything is published there. Licensed
under [MIT](LICENSE).

## Use in a project

Start with the independent example produced by this repository:

```sh
just release
just example '/path/to/new-project'
cd '/path/to/new-project'
just setup
just exec python3 examples/core/greeting.py
just verify
```

`just release` requires a clean committed Chainman source tree. `just example` takes
an empty destination and copies a small working core, optional modules and the exact
runtime archive. It does not initialize Git or run the new project's commands.
Its README explains adoption, caches, updates and native SDK requirements.

New consumers default to containerized Nix. Set `CHAINMAN_MODE=host-nix` for host Nix
or `CHAINMAN_CONTAINER_ENGINE=podman` to select Podman. Linux and macOS are supported;
Windows uses WSL2 with the checkout in the Linux filesystem. Native Apple SDK work
uses host Nix on macOS. Native SDK discovery does not replace app packaging/device
verification.

An existing project keeps its `justfile`, flake, workspace organization and application
commands. Copy `bootstrap/chainman.sh` to `scripts/chainman.sh` and `bootstrap/fetch.nix`
to `scripts/chainman-fetch.nix`, adopt a release lock, and call
`./scripts/chainman.sh exec --profile default -- COMMAND...` from its existing adapter.
Import the generated `scripts/chainman.just` facade and declare the standard recipe
bindings. See [project recipes](docs/recipes.md), [configuration](docs/configuration.md)
and [dependency updates](docs/updates.md).
The bootstrap and helper are managed release files; custom behavior belongs in the
project adapter/configuration, so self-updates can check and replace them safely.

## Develop Chainman

Chainman's own source development requires `just`, Git and host Nix. Its small source
launcher enters the pinned core shell directly, avoiding a bootstrap dependency on
an older release of itself. Consumer installation is exercised separately against
real disposable archives and projects.

Source `format` and `deps-update` use the same isolated candidate, verification and
exact-commit machinery as consumers. `format commit=off` generates and formats in
place; `format-write` only formats. Failed acceptance retains the candidate for
inspection and `resume=...`, with the original checkout unchanged.

```sh
just setup
just verify
just module rust verify
just module javascript verify
just javascript-test
just bootstrap-test docker
just bootstrap-test podman
just release
just example
```

`just verify` runs syntax/format checks, unit tests, real neutral Git transactions,
real host-Nix bootstrap tests, and the tiny deterministic demo build.
The bootstrap suite includes public dependency/runtime updates, failed and
interrupted verification with resume, and preservation of partial staging.
Container tests opt into an installed engine. `just bootstrap-test` exposes the
host engine client to the Nix test process; it does not use a host language interpreter.
`just javascript-test` uses the pinned npm and pnpm binaries against a disposable
local registry to qualify dependency resolution, overrides and frozen lock checks.
It includes the JavaScript unit suite and local pnpm installation/receipt checks,
and requires no public registry downloads.
Optional modules cover JavaScript/TypeScript, Rust, Python, Go, Flutter/Dart,
SwiftPM/SwiftUI and Gradle/Compose. They are loaded only when requested. The manually
dispatched workflow contains portable and native Apple lanes; running one lane is
not evidence that another platform works.

The release uses [an explicit inventory](release-files.json) read from the clean Git
commit, stable archive ordering, timestamps and permissions. It emits a source
archive, `chainman-release.json` and `SHA256SUMS` under `dist/release/`. The metadata
records the full source revision, flat archive SHA-256 and unpacked SHA-256 NAR hash.
Build metadata is outside the archive to avoid self-referential hashes. Identical
source produces identical artifacts. Nothing in these commands pushes or publishes.

## Scope

Chainman owns environment entry, scoped caches, setup fingerprints, dependency update
transactions and repeatable distribution. Projects own their requirements, dependency
selection extensions, native SDK configuration and application commands. A custom
resolver must explicitly accept eligibility responsibility; the transaction cannot
infer release dates from arbitrary shell scripts. Keep specialized cleanup close to
the outputs it understands.

Read [the runtime and bootstrap contract](docs/runtime.md) before extending mounts,
cache lifetimes or managed release files. The development environment runs trusted
project code with declared access; it is not a sandbox for hostile source.

`just verify` includes Ruff correctness checks across Python source and tests, plus
`just type-check` for the runtime/control modules listed in `mypy.ini`. Mypy checks
unannotated function bodies in that scope; this is not a repository-wide strict
typing claim. Adapter signatures are visible to the gate, while expanding coverage
of their implementations remains separate work. All checkers come from pinned Nix.
