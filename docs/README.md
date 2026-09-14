# Chainman guides

Start with the [repository README](../README.md) for prerequisites and a working
v0.1.0 quickstart. These guides describe the checked-in launcher and runtime API.
New projects use **configuration schema 3**; older schemas remain compatibility
interfaces and are marked in the configuration reference.

| Guide | Use it for |
| --- | --- |
| [Getting started](getting-started.md) | New projects, existing-project adoption, and optional languages |
| [Configuration](configuration.md) | Profiles, setup ownership, tasks, mounts, caches, and update policy |
| [Recipes](recipes.md) | Generated `just` facade and project command bindings |
| [Services](services.md) | Service dependencies, readiness, and lifecycle |
| [Updates](updates.md) | Selection, adapters, commit defaults, verification, and recovery |
| [Troubleshooting](troubleshooting.md) | Bootstrap, setup, caches, failed updates, and native SDKs |
| [Runtime and release trust](runtime.md) | Pins, Nix hashes, isolation limits, and release artifacts |
| [Testing](testing.md) | Qualification evidence and platform limitations |
| [Contributing](contributing.md) | Source development, tests, and local release fixtures |
| [Releasing](releasing.md) | Immutable GitHub publication and optional attestation verification |

## Language examples

The starter enables only `core`. See each example before enabling its module:
[JavaScript](../examples/javascript/README.md), [Rust](../examples/rust/README.md),
[Python](../examples/python/README.md), [Go](../examples/go/README.md),
[Flutter](../examples/flutter/README.md), [Swift](../examples/swift/README.md), and
[Compose](../examples/compose/README.md). Project-owned toolchains and adapters can
replace these defaults without changing the pinned Chainman runtime.
