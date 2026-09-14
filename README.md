# Chainman

**Development environments and project-wide maintenance behind your `just` commands.**

Chainman brings Nix environments, dependency setup, development services, scoped
caches, and verified dependency updates into a project's existing workflow. You
check in a small launcher and a release pin. Nix fetches the pinned runtime and
checks its hash before running it; languages and build tools come from pinned Nix
shells. There is no global Chainman installation or runtime archive to vendor.

Projects keep their own commands, toolchains, dependency policies, and acceptance
tests. Chainman coordinates them, including projects with several languages or
services. [chainman.dev](https://chainman.dev) explains the motivation and scope.

**v0.1.0 is an experimental alpha, not recommended for general adoption.** Expect
breaking changes, investigate failures, and qualify updates against your own
application. See [release trust](docs/runtime.md) and
[testing coverage](docs/testing.md) for the guarantees and their limits.

## Prerequisites

- Git and [just](https://just.systems).
- **Docker or Podman**, running locally, for the default container Nix mode; or
  **Nix 2.24 or later** for host Nix mode.

You do not need host Python, Node, `gh`, or a global package manager. Linux and
macOS are supported; Windows uses WSL2 with the project in its Linux filesystem.
Native Apple SDK work requires host Nix on macOS and the relevant Apple tools.
Containers execute trusted project code with declared access; they are not a
sandbox for hostile repositories.

## Start a project

Run this with Docker or Podman available:

```sh
git clone https://github.com/chainmandev/chainman.git chainman
cd chainman
just init ../my-project 0.1.0
cd ../my-project
git init
git add .
git commit -m "Adopt Chainman"
just setup
just exec python3 examples/core/greeting.py
just verify
```

For host Nix, set this before the same commands:

```sh
export CHAINMAN_MODE=host-nix
```

`init` accepts a new or empty destination whose parent exists, including paths with
spaces. It verifies the explicitly selected published release and creates an
independent schema-3 starter with a URL-only lock. It does not initialize Git or
run setup. After initialization, the new project does not depend on the Chainman
checkout. The first command can take time while Nix downloads the pinned tools.

The starter includes a working Python demo and optional JavaScript/TypeScript,
Rust, Python, Go, Flutter/Dart, Swift, and Compose examples. Enable only the modules
you need. For an existing repository, follow the
[adoption guide](docs/getting-started.md#existing-projects).

## Everyday commands

```sh
just --list
just setup
just exec python3 --version
just config validate
just setup-status
just verify
just deps-update mode=dry-run
just deps-update commit=off
just stop
```

Dependency updates allow major versions by default, apply a configurable **30-day
minimum age**, and run project verification in an isolated candidate before
applying changes. **Successful updates commit by default**; use `commit=off` to
review the verified changes yourself. Failed verification retains the candidate
for inspection and resume. Formatting also commits by default where the project
uses Chainman's transaction-backed formatting recipe.

Explicit initial adoption can select a new release. Automatic runtime updates
still apply the age policy. While v0.1.0 matures, update project dependencies with:

```sh
just deps-update --skip-chainman mode=dry-run
just deps-update --skip-chainman commit=off
```

See [updates and recovery](docs/updates.md) before your first update.

## Documentation

- [Guide index](docs/README.md) and [getting started](docs/getting-started.md)
- [Configuration](docs/configuration.md), [recipes](docs/recipes.md), and [services](docs/services.md)
- [Dependency updates](docs/updates.md) and [troubleshooting](docs/troubleshooting.md)
- [Installation and release trust](docs/runtime.md)
- [Contributing](docs/contributing.md) and [publishing releases](docs/releasing.md)

Chainman's source development uses host Nix:

```sh
just setup
just verify
just control-test
```

Licensed under [MIT](LICENSE).
