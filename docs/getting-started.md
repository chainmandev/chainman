# Getting started

[Guide index](README.md) · [Configuration](configuration.md) · [Recipes](recipes.md)

## Execution modes

Install Git, just, and either a running Docker/Podman engine or Nix 2.24+. Container
Nix is the default. Select Podman explicitly with
`export CHAINMAN_CONTAINER_ENGINE=podman`, or host Nix with
`export CHAINMAN_MODE=host-nix`. Keep this mode selection in your shell environment.
No host language interpreter or GitHub CLI is required.

## New projects

```sh
git clone https://github.com/chainmandev/chainman.git chainman
cd chainman
just init '../my project' 0.1.0
cd '../my project'
git init
git add .
git commit -m "Adopt Chainman"
just setup
just exec python3 examples/core/greeting.py
just verify
```

The destination must be new or empty, have an existing parent, and contain no
symlink path components. `init` takes the numeric version, without `v` or `latest`.
It checks the public release's immutable status, tag revision, asset identities,
sizes, SHA-256 digests, and unpacked NAR hashes before installing files. Explicit
adoption does not wait for the automatic-update maturity window.

Initialization tooling comes from this source checkout's pinned Nix shell. In
container mode, a temporary container owns a private Nix store and its generated
output is copied out with host ownership. The container is removed afterward.
The resulting project fetches its own hash-pinned runtime independently. You can
remove the Chainman checkout after initialization.

### What gets checked in

| Files | Owner |
| --- | --- |
| `chainman.lock`, `scripts/chainman.sh`, `scripts/chainman-fetch.nix` | Managed release pin and launchers |
| `scripts/chainman.just` | Generated facade from project recipe bindings |
| `chainman.toml`, `justfile`, `flake.nix`, `flake.lock` | Project configuration and toolchain |
| `modules/`, `examples/`, project scripts | Project commands and acceptance checks |

Do not edit the managed launchers to customize behavior. Put changes in project
configuration, adapters, or task scripts so runtime updates can verify and replace
managed files safely. No runtime archive is checked in.

### Optional languages

The generated configuration begins with:

```toml
schema = 3
modules = ["core"]
```

To try JavaScript, change only the module list to
`modules = ["core", "javascript"]`, preserving the rest of the file, then run:

```sh
just setup
just module javascript verify
just verify
```

Read the selected example's README for its dependency setup and native SDK needs.
The supplied examples are small acceptance fixtures, not application scaffolds.

## Existing projects

Adopt Chainman on a clean working branch. Keep your application commands and
toolchain locks. First obtain verified launcher files and a release pin in a
disposable starter; run these commands from the repository you are adopting:

```sh
scratch=$(mktemp -d)
git clone --depth 1 https://github.com/chainmandev/chainman.git "$scratch/chainman"
just --justfile "$scratch/chainman/justfile" init "$scratch/starter" 0.1.0
mkdir -p scripts
cp "$scratch/starter/chainman.lock" .
cp "$scratch/starter/scripts/chainman.sh" scripts/
cp "$scratch/starter/scripts/chainman-fetch.nix" scripts/
```

Give your existing acceptance recipe the name `project-check`. Create
`chainman.toml` with this minimal schema-3 configuration:

```toml
schema = 3

[project]
default_profile = "default"

[profiles.default]
runtime_profile = "core"

[tasks.check]
commands = [["just", "project-check"]]

[recipes]
verify = ["check"]
verify-lite = ["check"]

[updates]
minimum_age_days = 30
verify_task = "check"
```

If your project already has a Nix development shell, replace
`runtime_profile = "core"` with, for example, `flake = "nix#default"`. Profiles
select environments; your application commands and toolchains stay project-owned.
Do not have `project-check` invoke the generated `verify` recipe: that would recurse.

Generate the recipe facade through the verified launcher:

```sh
./scripts/chainman.sh exec --profile host -- sh -eu -c 'python3 "$CHAINMAN_RUNTIME/scripts/recipes.py" "$CHAINMAN_PROJECT_ROOT"'
```

Add this import to your existing `justfile`, resolving any duplicate public recipe
names by keeping the facade's public names and renaming application internals:

```just
import 'scripts/chainman.just'
```

Then validate the configuration and run your acceptance check:

```sh
just config validate
just verify
git add chainman.toml chainman.lock justfile scripts/chainman.sh scripts/chainman-fetch.nix scripts/chainman.just
git commit -m "Adopt Chainman v0.1.0"
```

This minimal adoption provides environment entry and verification. Add explicit
[setup inputs and readiness artifacts](configuration.md),
[service declarations](services.md), and [dependency adapters and update
outputs](updates.md) for your application before using those features. Chainman
cannot infer a complete dependency policy from an arbitrary shell script.

## While the first release matures

`init` deliberately selects v0.1.0 immediately. Ordinary updates retain the default
30-day age policy; a newly published runtime is not yet eligible. You can update
project dependencies independently:

```sh
just deps-update --skip-chainman mode=dry-run
just deps-update --skip-chainman commit=off
```

`commit=off` applies successfully verified changes without creating a commit. The
default is to commit. See [updates](updates.md) for maturity exceptions and
[troubleshooting](troubleshooting.md#verification-and-recovery) for failed candidates.
