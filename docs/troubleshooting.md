# Troubleshooting

[Documentation index](README.md) · [Runtime](runtime.md) · [Update recovery](updates.md)

## Pin or Git cache failures

`chainman.lock` must contain one full lowercase 40-character commit SHA and a
newline. Branches, tags, shortened SHAs, and the pre-publication JSON archive lock
are not supported. A missing revision never falls back to a newer release.

A cold launch requires public Git access. Confirm the canonical repository is
reachable with `git ls-remote https://github.com/chainmandev/chainman.git`. A warm
launch uses existing verified objects without contacting GitHub. A successful warm
bootstrap can still be followed by a project-tool download; inspect which operation
failed before diagnosing the source cache.

If Git reports corruption, preserve or quarantine the affected revision's cache
entry, then obtain a fresh copy of the same SHA. Do not edit the pin to suppress an
integrity failure. The cache path is printed in Git diagnostics and lives under
`${XDG_CACHE_HOME:-$HOME/.cache}/chainman/git/`. Do not repair active caches or remove
Nix roots while operations are running.

## Nix or container entry

Container mode is the default. Start Docker/Podman, or explicitly choose
`CHAINMAN_MODE=host-nix`. Host mode requires a compatible Nix installation.
Chainman does not supply a host-language fallback.

An existing wrapper that starts another Nix shell or Docker container may fail
inside a managed environment. Route its underlying command instead; see
[host wrappers](adoption.md#existing-justfiles-and-host-wrappers). A `host` profile
inside container mode does not mean the physical host.

Missing native SDKs require the project's platform setup. Do not broaden mount
permissions to work around a missing tool. Declare the actual SDK path and the
specific task that needs it.

## Initialization and Git commit failure

`init` accepts only a new or empty destination. Use manual adoption for an existing
project. If the initial Git commit fails, the generated project remains in place.
Follow the printed commands after correcting the host Git identity or signing
backend. `--no-git` deliberately creates files without initializing Git.

Initialization does not run setup or verification. Use the generated README for
those next steps.

## Setup and services

```sh
just chainman setup-status
just chainman setup
just chainman services-status
just chainman services-stop
```

A stale setup check should name the changed inputs or missing readiness artifact.
Check ownership before deleting installation directories. Service recovery uses
saved ownership state; status and stop remain available even when current service
configuration is broken. Destructive data reset requires its explicit reset option.

## Updates and recovery

Use `just chainman deps-update --skip-chainman` for project-only updates.
Runtime selection needs an available advertised default branch, without an age delay.
Missing project dependency age/provenance evidence is an error, not an implicit waiver.

Updates prepare isolated candidates and preserve failures. Use the transaction path
printed by the failure message:

```sh
just chainman deps-update resume=/absolute/path/to/transaction
```

Resume retains the selected runtime, rechecks original state and the exact Git objects,
and reruns acceptance. It cannot silently choose a different revision. Reconcile
modified declared pin copies through their owning generator before retrying.

If application or Git commit was interrupted, inspect the original diff, index,
HEAD, and retained candidate first. Some already-verified writes or the commit may
have completed. Do not assume an absent success message means no changes occurred.
See [transaction limits and recovery](updates.md).
