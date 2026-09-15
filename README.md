# Chainman

Chainman runs a repository's development commands in pinned Nix environments and
coordinates setup, services, and verified dependency updates. The project owns its
toolchains and workflows; Chainman supplies the execution machinery.

Each repository records **one Chainman Git commit** in `chainman.lock` and a small
bootstrap recipe in its justfile. Running `just chainman …` obtains that exact
revision and uses it for the command. No global Chainman installation or permanent
Chainman checkout is needed. See [chainman.dev](https://chainman.dev) for the introduction.

**v0.1.0 is an alpha release.** Expect configuration and command changes as the
interfaces mature. A project's pin changes only through an explicit edit or a
verified update; launching a command never silently upgrades it.

## Prerequisites

- Git and [just](https://just.systems).
- Docker or Podman for the default **container-Nix** mode, or **Nix 2.24+** for host mode.
- Ordinary shell utilities available on supported Linux and macOS hosts.

Python, Node, `gh`, curl and wget are not bootstrap prerequisites. Project SDKs come
from the project's flake. Some workflows still require native Apple or Android SDKs;
see [execution modes and native tools](docs/runtime.md).

## Adopt an existing project

Adoption is a deliberate integration. Start with one existing command, then add
setup, service ownership, and updates as needed. Existing root or nested flakes
need no Chainman import. Preserve your justfile and acceptance gates.

### 1. Record the revision

From your project directory, resolve the published lightweight `v0.1.0` tag and
write its commit pin. This block fails if the tag is unavailable:

```sh
sh -eu <<'SH'
remote=$(git ls-remote --exit-code https://github.com/chainmandev/chainman.git refs/tags/v0.1.0)
printf '%s\n' "$remote" | cut -f1 > chainman.lock
test "$(wc -c < chainman.lock)" -eq 41
SH
```

Review the selected commit as you would any executable dependency. Future tag moves
do not change the recorded SHA. See [release trust](docs/release-trust.md).

### 2. Add this complete recipe to your justfile

Keep your existing recipes. Reserve the name `chainman` for this entrypoint; if it
already exists, rename that recipe deliberately before proceeding.

```just
# Stable consumer bootstrap. Runtime behavior belongs to the pinned Git revision.
[group("Chainman")]
[positional-arguments]
chainman +args:
    #!/bin/sh
    set -eu
    IFS= read -r revision < chainman.lock
    case "$revision" in ''|*[!0-9a-f]*) echo 'chainman.lock requires a full lowercase Git SHA' >&2; exit 2 ;; esac
    test "${#revision}" -eq 40 && test "$(wc -c < chainman.lock)" -eq 41
    cache=${XDG_CACHE_HOME:-$HOME/.cache}/chainman/git/github.com-chainmandev-chainman/$revision.git
    g() (
        unset $(GIT_CONFIG_PARAMETERS='' GIT_CONFIG_COUNT=0 git rev-parse --local-env-vars)
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=0 GIT_TERMINAL_PROMPT=0 git --no-replace-objects -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
    )
    if test ! -e "$cache"; then
        mkdir -p "${cache%/*}"
        temporary=$(mktemp -d "$cache.XXXXXX")
        g init --bare --quiet --template= "$temporary"
        ln -sn "$temporary" "$cache" 2>/dev/null || rm -rf "$temporary"
    fi
    if ! g --git-dir="$cache" cat-file -e "$revision" 2>/dev/null; then
        g --git-dir="$cache" -c gc.auto=0 fetch --no-auto-maintenance --no-write-fetch-head https://github.com/chainmandev/chainman.git "$revision"
    fi
    g --git-dir="$cache" fsck --full --strict --no-reflogs --no-dangling
    test "$(g --git-dir="$cache" cat-file -t "$revision")" = commit
    entry=$(g --git-dir="$cache" cat-file blob "$revision:bootstrap/git-entry.sh")
    exec sh -c "$entry" chainman "$PWD" "$cache" "$revision" "$@"
```

This is the entire committed bootstrap. Its Git cache is a download cache, not an
installation or a mutable “current” version. Runtime updates leave this recipe
unchanged. Cache behavior and repair are described in [troubleshooting](docs/troubleshooting.md).

### 3. Route one existing command

For a project whose existing flake exposes `devShells.<system>.default`, add
`chainman.toml`:

```toml
schema = 3

[project]
default_profile = "default"

[profiles.default]
flake = ".#default"

[tasks.check]
commands = [["just", "check"]]
```

Then exercise the **existing** `check` recipe inside its existing environment:

```sh
just chainman run check
```

Use your actual recipe name in place of `check`. For a nested flake, use a reference
such as `nix#default`. The command above must be a command that can already run
*inside* the development environment. If `just check` currently enters Nix itself,
starts Docker, requests credentials, or assumes it is on the host, first separate
its environment entry from the underlying command. Do not create a forwarding
loop between `check` and `just chainman run check`.

For host Nix, select it explicitly:

```sh
CHAINMAN_MODE=host-nix just chainman run check
```

After the command behaves correctly, commit the recipe, pin, and configuration.
Follow the [progressive adoption walkthrough](docs/adoption.md) for task routing,
existing host wrappers, setup, services, and verification before updates.

## Start a new project

Use a disposable checkout to initialize a **new or empty** directory:

```sh
git clone --branch v0.1.0 --depth 1 https://github.com/chainmandev/chainman.git chainman-init
just --justfile chainman-init/justfile init "../my-project" v0.1.0
cd my-project
just chainman setup
just verify
```

`just` resolves recipe paths from the checkout: `../my-project` above is next to
`chainman-init`. Absolute destination paths also work. The checkout can be removed
once initialization succeeds.

`init` accepts a numeric version, `vVERSION`, or a full commit SHA. It rejects moving
selectors such as `main` and `latest`. It obtains the selected revision and uses
that revision's generator and templates. Version selection requires a published
stable release and bypasses the automatic update age policy.

The starter contains a small example, its verification command, and a project-owned
flake and lock. Git initialization and the initial commit happen by default using
your host identity, signing policy, and branch defaults. A commit failure preserves
the files and prints recovery instructions. To generate files only:

```sh
just --justfile chainman-init/justfile init "../another-project" v0.1.0 --no-git
```

Initialization does **not** run project setup or certify the application. The larger
[language examples](examples/) remain in this repository for reference.

## Commands and updates

```sh
just chainman version
just chainman shell
just chainman exec -- python3 --version
just chainman recipe verify
just chainman deps-update --skip-chainman mode=dry-run
```

The shell and exec commands use the project's default profile. Standard recipes
such as `recipe verify` need [project bindings](docs/recipes.md); dependency updates
need configured adapters and a complete verification gate. The starter includes
both for its small example. The minimal existing-project configuration above
intentionally introduces only one task.

Runtime updates select stable published releases at least 30 days old by default,
considering both publication and commit time. They test the proposed SHA and
reconciled outputs in an isolated candidate before applying changes. Unqualified
candidates are preserved for inspection and recovery. Without `mode=dry-run` or
`commit=off`, successful updates commit the verified changes. Use `--skip-chainman`
for project-only updates while the first runtime release matures.

## Documentation

Start with the [documentation index](docs/README.md):

- [Adoption walkthrough](docs/adoption.md)
- [Schema-3 configuration](docs/configuration.md) and [recipes](docs/recipes.md)
- [Setup and services](docs/services.md)
- [Dependency updates and recovery](docs/updates.md)
- [Execution modes, caches, and native tools](docs/runtime.md)
- [Troubleshooting](docs/troubleshooting.md) and [release trust](docs/release-trust.md)
- [Contributing and qualification](docs/contributing.md)
