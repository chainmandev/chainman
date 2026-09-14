# Troubleshooting

[Guide index](README.md) · [Runtime](runtime.md) · [Updates](updates.md)

## Bootstrap and execution mode

For host Nix, use `export CHAINMAN_MODE=host-nix` and confirm `nix --version` is
2.24 or later. For container Nix, start Docker or Podman and confirm its `info`
command succeeds. `CHAINMAN_CONTAINER_ENGINE=podman` selects Podman explicitly.
Do not install a host Python interpreter to repair bootstrap: Nix provides it.

```sh
just --list
just config validate
just exec python3 --version
```

A release hash mismatch means the downloaded bytes or unpacked tree do not match
the pin. Recheck the published release identity; do not replace the lock's hashes
with hashes of unexpected bytes. An unavailable asset must be restored by a new
release if the existing release is immutable. `CHAINMAN_ARCHIVE` is a local archive
override for disposable qualification; it must still satisfy the pinned hash.
Older consumers can retain `bundled_archive`, but new consumers use URL-only locks.

Initialization and new runtime selection read GitHub release metadata. If GitHub
reports an API rate limit, wait for the reset or supply an optional `GITHUB_TOKEN`
environment variable with access to the public repository. The initializer passes
it to its temporary container when container mode is selected. Keep credentials
out of project files and do not change the token during a command. Release assets
are downloaded anonymously; ordinary launches need neither this token nor the
release-metadata API. A missing release tag or incomplete asset set must be fixed
by the publisher, rather than by changing the consumer's hashes.

## Setup and services

```sh
just setup-status
just setup
just services-status
just stop
```

Setup readiness depends on declared inputs, fingerprints, and artifacts. When
dependencies or toolchains change, setup reruns the affected work. Declare outputs
that actually demonstrate readiness, rather than an arbitrary marker file. Keep
service startup and stop commands with the project that owns their lifecycle.
Use the service/task inspection commands listed by `just --list` to diagnose a
failed readiness probe or dependency ordering issue.

## Verification and recovery

Updates and transaction-backed formatting verify an isolated candidate before
applying it. A failed run prints the retained worktree and resume location. Inspect
that location and its logs, fix the cause, then pass the exact printed resume value
to the same command, for example:

```sh
just deps-update resume=/absolute/path/printed/by/the/failed/run
```

Do not replace the placeholder with the original project directory. Resume checks
recorded identity and state; it does not silently restart against a changed
checkout. If interruption occurred while applying verified changes, inspect both
the project Git status and retained transaction state before trying again. The
[update contract](updates.md) explains partial application and recovery limits.

Managed launchers or facades changed by hand can prevent runtime updates. Move
custom behavior to project-owned configuration or scripts, then reconcile the
managed files against the release you already trust. Preserve unrelated changes
and staging; avoid a blanket reset.

If no runtime satisfies the 30-day policy, use `just deps-update --skip-chainman`
for project-only updates. Explicit initialization of a selected version and
automatic release selection intentionally have different age requirements.

## Caches and cold starts

The first Nix realization downloads tools and may build missing substitutes.
Subsequent launches reuse Nix and declared download/build caches. Use the cache
inspection and cleanup recipes in `just --list` before deleting anything by hand.
Chainman scopes mutable state and serializes destructive operations with active
work. Nix-store garbage collection is separate from project cache cleanup; it can
make the next command cold again. Initializer containers use a temporary store,
so initialization and the first normal project command may each fetch tools.

## Native SDKs and platform lanes

Nix language tools do not replace platform SDKs, signing identities, simulators,
devices, or native acceptance checks. Read the Flutter, Swift, or Compose example
README before enabling it. Apple-native tasks need macOS, host Nix, and the
appropriate Xcode selection. Container success on Linux does not qualify macOS,
Windows, mobile packaging, or a physical device. Run the platform's declared lane
and retain its evidence separately.
