# Execution modes, caches, and native tools

[Documentation index](README.md) · [Adoption](adoption.md) · [Release trust](release-trust.md)

## Per-project runtime selection

A project records a full Git SHA in `chainman.lock`. The bootstrap verifies that
commit's Git objects and reads its initial entrypoint directly from the Git object,
then that entrypoint materializes the selected tree. It never runs an unchecked
cached checkout. The runtime handles configuration, Nix, containers, services, and
update transactions. See [release trust](release-trust.md) for the integrity boundary.

The source checkout used during initialization is disposable. Project flakes and
locks remain independently owned; they do not import Chainman.

## Container Nix is the default

```sh
just chainman run check
```

The runtime selects Docker or Podman, uses its pinned Nix image, and provisions
owned Nix/download volumes. Container configuration and image pins belong to the
runtime revision, not the consumer recipe. You can select an engine explicitly:

```sh
CHAINMAN_CONTAINER_ENGINE=podman just chainman run check
```

Ordinary project containers run as the mapped user, with dropped capabilities and
no engine socket mount. The runtime controls owned services from the host. The
normal mounts are the project, required linked-worktree Git administration, and
owned cache volumes. Extra mounts, environment forwarding, and published ports
must be declared. Blanket home/root and socket mounts are rejected.

This isolation limits accidental access; it is not a hostile-code sandbox.
Commands can write their project, share admitted caches, and access the network.
Host orchestration and explicitly authorized host/native operations have host
access. Container mode does not make untrusted code safe to execute.

## Host Nix is explicit

```sh
CHAINMAN_MODE=host-nix just chainman run check
```

Host mode requires Nix 2.24 or newer and uses the selected host installation. An
explicit `CHAINMAN_NIX_BIN` must be an absolute executable path. Chainman does not
replace the host's Nix. The same project flake supplies its language tools.

A profile named `host` means the already bootstrapped execution context. In
container mode that context remains in the container; it does not escape to the
physical host. Existing wrappers that start Nix or Docker need review before being
invoked inside a managed task. See [adoption](adoption.md#existing-justfiles-and-host-wrappers).

## Three separate caches

| Cache | Role |
|---|---|
| User-local bare Git objects | Downloaded source objects, keyed by canonical source and commit |
| Nix store and lifetime roots | Verified runtime source and executable tool environments |
| Project/download caches | Package downloads, setup artifacts, and build outputs |

The Git cache is under `${XDG_CACHE_HOME:-$HOME/.cache}/chainman/git/`. It has no
mutable “current” pointer. Concurrent first use publishes an initialized cache
before fetching; an interrupted fetch can be retried. Warm bootstrap performs no
network request when the required objects are present. Integrity failures stop
execution rather than selecting a different revision.

Offline warm **bootstrap** does not guarantee offline project execution: a newly
requested Nix environment, package manager command, or network-dependent test can
still need downloads. Keep the relevant environments and project dependencies
available if offline operation is required.

Nix source roots live under the host Chainman runtime-roots cache or the owned
container store. Temporary environment roots remain while their managed operations
need them. Runtime generations coexist across projects and updates. Cache cleanup
must respect active operations and service ownership.

The owned container daemon uses pressure garbage collection. Host Nix retains its
own configuration. Nix collection does not prune package-manager downloads or
project outputs. Inspect project caches explicitly:

```sh
just chainman cache-status
just chainman cache-prune
```

Use `cache.automatic_prune = false` if project cache pruning should be explicit.
See the configuration reference for size, age, and compiler-cache settings.

## Native SDKs and credentials

Apple SDK workflows require an appropriate macOS host and Xcode/Command Line Tools;
a Linux container cannot supply Apple's host platform. Android workflows may need
an installed SDK, emulator/device access, or a project-specific native adapter.
Declare narrow SDK paths and environment inputs where appropriate. A successful
bootstrap is not proof that a native platform lane is available.

Project environment values are data applied in the selected execution lane. They
do not override the host container engine's environment. Forward only named host
inputs needed by the task. Keep credential-bearing publication and deployment
operations separate from ordinary verification.

## Paths and process behavior

Spaces in project, cache, and checkout paths are supported. Git-linked worktrees
and nested independent projects retain their own project identity. Container mount
paths cannot contain commas; newline-containing runtime/mount paths are rejected.
Arguments and stdin are passed literally, and command failures propagate to just.
Service and update orchestration also own cleanup and interruption handling.

Use `TMPDIR` for an explicit temporary base. Internal `CHAINMAN_*` routing and
transaction variables are not project configuration. Do not carry those variables
from one independent project invocation into another.
