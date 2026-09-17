# Adopt Chainman progressively

[Documentation index](README.md) · [Complete bootstrap recipe](../README.md#2-add-this-complete-recipe-to-your-justfile)

Adopting Chainman in an established repository is integration work. There is no
repository-rewriting adoption command. The initializer is for new or empty
folders, not a way to upgrade an existing build system.

## 1. Keep the existing environment

Keep the project's flake and lock. A profile selects a shell; the flake does not
import Chainman. Root and nested flakes are both supported:

```toml
schema = 3
[project]
default_profile = "application"
[profiles.application]
flake = "nix#default"
```

Use `.#default` for a root flake or select your actual shell name. For multiple
independent environments, add profiles and assign tasks explicitly.

Record the full Git pin and paste the complete bootstrap from the README. Review
those additions before committing. A disposable checkout used to inspect Chainman
or inspect a revision has no ongoing relationship with your repository.

## 2. Route one command

Choose an existing command that already works inside the environment:

```toml
[tasks.check]
commands = [["bash", "scripts/check.sh"]]
```

```sh
just chainman run check
CHAINMAN_MODE=host-nix just chainman run check
```

Container mode uses the project and declared mounts. Host-Nix mode runs on the host
through Nix. Compare outputs and failure behavior with your existing workflow
before routing more commands.

### Existing justfiles and host wrappers

The embedded bootstrap is a host-shell shebang recipe, so a global `set shell`
that enters Nix does not wrap the bootstrap. However, **commands you invoke inside
a task still retain their own wrappers**. If your old `just check` runs `nix develop`,
calling it from a managed task enters the environment twice. A wrapper that refuses
to run inside Nix will fail outright.

Separate the underlying command from its host entry. Route the underlying script
or executable through Chainman. Keep host service management, credential-bearing
operations, and native platform operations in explicit host entrypoints until their
boundaries have been reviewed. `profile = "host"` means the bootstrapped execution
context; in container mode it is still inside the container, not a host escape.

Review command-name collisions. A project can retain its public `check` name with
an optional forwarding recipe after its underlying operation is separate:

```just
[positional-arguments]
check *args:
    #!/bin/sh
    exec just chainman run check -- "$@"
```

Do not configure the `check` task to call this same `just check` recipe: that would
recurse. Chainman never generates or rewrites these forwarding recipes.

For a project-wide shell wrapper that also runs inside `just chainman shell` or
`exec`, an active environment is not proof of setup readiness. Dispatch its named
task through the verified runtime's reentry helper:

```sh
# root is this wrapper's absolute project root; task is a declared ordinary task.
if [ "${CHAINMAN_ROOT:-}" = "$root" ] && [ -n "${CHAINMAN_ACTIVE_PROFILE:-}" ]; then
    exec "$CHAINMAN_RUNTIME/bootstrap/reenter.sh" "$root" "$task" -- "$@"
fi
exec just --justfile "$root/justfile" chainman run "$task" -- "$@"
```

Reentry preserves operation authority, task admission and setup leases. It reuses
an unchanged selected profile, refreshes changed profile inputs, and refuses a
changed runtime pin until the caller leaves the shell. Select tasks explicitly;
do not infer that all tasks need the largest profile or all setup groups. Service
graphs still start through host entrypoints. These `CHAINMAN_*` bindings are
runtime-provided context, never user configuration.

## 3. Add setup ownership

Define installation inputs and readiness artifacts so tasks can check setup
without silently reinstalling leased dependencies. Start with the actual package
manager's frozen install command. Declare outputs narrowly; keep generated assets
and caches with the project that owns them.

Use [setup groups in the configuration reference](configuration.md#named-setup-groups-and-tasks),
then inspect and run them explicitly:

```sh
just chainman setup-status
just chainman setup
```

## 4. Add services where needed

Convert one task's development services at a time. Declare readiness, shutdown,
ports, storage and environment. Check startup failure and cleanup, not just the
happy path. See [services](services.md).

Do not put credential-bearing release, deployment or account operations into an
ordinary verification task. Keep their authorization and environment explicit.
Native SDKs may require a host lane or narrowly declared mounts; see [runtime
boundaries](runtime.md).

## 5. Establish the acceptance gate

Bind the existing full project gate. Sequential verification is useful when
service configurations must not be active together:

```toml
[recipes]
verify = ["verify-postgres", "verify-spanner"]
[updates]
verify_tasks = ["verify-postgres", "verify-spanner"]
```

Both named tasks must already exist. Recipe bindings and update verification must
select the same gate. Run it through `just chainman recipe verify` and confirm
that failures and service cleanup behave as expected.

## 6. Enable verified updates

Declare dependency adapters, ordered reconciliation, and permitted outputs. Read
[updates and recovery](updates.md) before the first application:

```sh
just chainman deps-check
just chainman deps-update --skip-chainman mode=dry-run
just chainman deps-update --skip-chainman commit=off
```

Start with a disposable branch or checkout and inspect the verified diff. The
preview leaves the original unchanged; `commit=off` applies without committing.
The default commits a successful verified update. Runtime updates select the current public default-branch SHA immediately, while
project dependencies retain their age policy. Add runtime updates after the full
project gate reliably verifies the candidate under Chainman.

## Adoption is complete when

The project's existing acceptance criteria pass in its declared execution modes,
setup and service ownership are correct, public commands remain clear, and update
failures preserve recoverable candidates. A successful `init` or `version` command
alone does not establish those application properties.
