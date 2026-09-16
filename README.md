# Chainman

Chainman runs a repository's development commands in pinned Nix environments and
coordinates setup, services, and verified dependency updates. The project owns its
toolchains and workflows; Chainman supplies the execution machinery.

Each repository records **one Chainman Git commit** in `chainman.lock` and a small
bootstrap recipe in its justfile. Running `just chainman …` obtains that exact
revision and uses it for the command. No global Chainman installation or permanent
Chainman checkout is needed. See [chainman.dev](https://chainman.dev) for the introduction.

**Chainman is alpha software.** Expect configuration and command changes as the
interfaces mature. A project's pin changes only through an explicit edit or a
verified update; launching a command never silently upgrades it.

## Prerequisites

- Git and [just](https://just.systems).
- Docker or Podman for the default **container-Nix** mode, or **Nix 2.24+** for **host-Nix** mode.
- Ordinary shell utilities available on supported Linux and macOS hosts.

In these Nix modes, Python, Node, `gh`, curl and wget are not bootstrap prerequisites. Project SDKs come
from the project's flake. Some workflows still require native Apple or Android SDKs;
see [execution modes and native tools](docs/runtime.md). That guide also describes
the discouraged, caller-maintained `CHAINMAN_MODE=host` escape hatch.

## Start a new project

Use a disposable checkout to initialize a **new or empty** directory:

```sh
git clone --depth 1 https://github.com/chainmandev/chainman.git chainman-init
just --justfile chainman-init/justfile init "../my-project"
cd my-project
just chainman setup
just verify
```

`just` resolves recipe paths from the checkout: `../my-project` above is next to
`chainman-init`. Absolute destination paths also work. The checkout can be removed
once initialization succeeds.

`just init DEST [SHA] [--no-git]` selects the public default branch's current commit
when SHA is omitted. Selection is frozen at the start; the selected revision's
generator and templates produce the project. Ordinary launches use the resulting
pin without checking for updates. No tag or GitHub release is needed.

The following alternatives run from the directory containing `chainman-init`
(return there first if you followed the quickstart's `cd my-project`). To reproduce
an existing project's runtime, supply its full lowercase SHA explicitly:

```sh
revision=$(cat /absolute/path/to/existing-project/chainman.lock)
just --justfile chainman-init/justfile init "../reproduced-project" "$revision"
```

Branch names, tags, and numeric versions are not accepted as the optional SHA.

The starter contains a small example, its verification command, and a project-owned
flake and lock. Git initialization and the initial commit happen by default using
your host identity, signing policy, and branch defaults. A commit failure preserves
the files and prints recovery instructions. To generate files only:

```sh
just --justfile chainman-init/justfile init "../another-project" --no-git
```

Initialization does **not** run project setup or certify the application. The larger
[language examples](examples/) remain in this repository for reference.

## Adopt an existing project

Adoption is a deliberate integration. Start with one existing command, then add
setup, service ownership, and updates as needed. Existing root or nested flakes
need no Chainman import. Preserve your justfile and acceptance gates.

### 1. Record the revision

From your project directory, resolve the public repository's current `HEAD` and
record its full commit SHA. Git follows the advertised default branch; you do not
need to know its name. Review an existing pin before replacing it.

```sh
sh -eu <<'SH'
remote=$(git ls-remote --exit-code https://github.com/chainmandev/chainman.git HEAD)
revision=$(printf '%s\n' "$remote" | cut -f1)
case "$revision" in ''|*[!0-9a-f]*) echo 'Invalid public Git identity' >&2; exit 1 ;; esac
test "${#revision}" -eq 40
printf '%s\n' "$revision" > chainman.lock
SH
```

Review the selected commit as you would any executable dependency. This is a
snapshot: future branch changes do not change your pin. See [Git trust](docs/release-trust.md).

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

Runtime updates resolve the current public default branch immediately and compare
Git SHAs. A changed SHA is eligible even when `VERSION` is unchanged. Project
dependencies retain their configurable 30-day maturity policy. Exact security
fixes can use [temporary age exceptions](docs/updates.md#temporary-security-exceptions);
verified updates remove those entries from TOML once they are no longer needed.

```sh
just chainman chainman-update mode=dry-run
just chainman chainman-update
```

Updates verify the selected SHA and reconciled outputs in an isolated candidate
before applying changes. A branch advance during verification or resume does not
replace that SHA; a later update discovers the newer tip. Failed candidates remain
available for inspection and recovery. Without `mode=dry-run` or `commit=off`,
successful updates commit the verified changes. Use `--skip-chainman` for
project-only dependency updates.

## Documentation

Start with the [documentation index](docs/README.md):

- [Adoption walkthrough](docs/adoption.md)
- [Schema-3 configuration](docs/configuration.md) and [recipes](docs/recipes.md)
- [Setup and services](docs/services.md)
- [Dependency updates and recovery](docs/updates.md)
- [Execution modes, caches, and native tools](docs/runtime.md)
- [Troubleshooting](docs/troubleshooting.md) and [release trust](docs/release-trust.md)
- [Contributing and qualification](docs/contributing.md)

Setup validates every declared installation, including package-manager readiness
checks, before reporting success. Ordinary commands prompt before required repairs;
CI should run setup explicitly or opt into `CHAINMAN_SETUP=auto`. See
[setup and readiness](docs/configuration.md#named-setup-groups-and-tasks).
