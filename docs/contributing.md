# Contributing

[Guide index](README.md) · [Testing](testing.md) · [Releasing](releasing.md)

Source development uses Git, just, and host Nix. The source launcher enters the
pinned shell directly, avoiding a dependency on a previously released Chainman.

```sh
just setup
just verify
just control-test
```

`verify` includes formatting, strict typing on Linux and Darwin targets, Python
unit and property tests, real Git transactions, host-Nix bootstrap tests, and the
small demo build. Use focused gates while iterating:

```sh
just format-write
just type-check
just init-test host-nix
just init-test docker
just init-test podman
just bootstrap-test docker
just bootstrap-test podman
just javascript-test
just python-test
just rust-test
just swift-test
just gradle-test
just module go verify
```

Initializer tests use real archives and Nix with only HTTP transport substituted,
then remove the source checkout and run the standalone starter. They exclude host
Python and GitHub CLI from the outer PATH. Container tests require a working engine. Native adapter fixtures use pinned tools
and disposable registries/repositories. The [testing guide](testing.md) describes
each lane; passing one lane does not qualify the others. Never exercise destructive
Git/filesystem tests against a user's project. Use disposable qualification copies.

Production Python modules enter strict mypy automatically. Validate external data
at its boundary; do not add blanket typing exemptions. Keep bootstrap independent
of host languages, runtime source immutable, and application behavior in project
configuration/adapters. Service and update lifecycle changes need failure and
interruption coverage, not only a successful-path test.

## Local release fixtures

Commit the exact source first. Releases read an explicit inventory from a clean
Git commit; adding a source file also requires updating `release-files.json`.

```sh
just release
just release dist/release-repeat
diff -r dist/release dist/release-repeat
just example /tmp/chainman-example
```

`example` is a maintainer fixture generator using local release metadata. Public
consumers use `just init DEST VERSION`. Both generate URL-only consumers. Before
publication, qualify the generated fixture with an external archive override:

```sh
CHAINMAN_ARCHIVE="$PWD/dist/release/chainman-0.1.0.tar.gz" CHAINMAN_MODE=host-nix just --justfile /tmp/chainman-example/justfile setup
CHAINMAN_ARCHIVE="$PWD/dist/release/chainman-0.1.0.tar.gz" CHAINMAN_MODE=host-nix just --justfile /tmp/chainman-example/justfile verify
just consumer-check --release dist/release/chainman-release.json /tmp/chainman-example
```

Use a fresh destination if `/tmp/chainman-example` exists. Do not vendor the
qualification archive or commit machine-local paths. Preserve project-specific
flake locks and acceptance gates when changing consumer pins.
