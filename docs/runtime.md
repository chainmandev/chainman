# Execution modes, caches, and native tools

[Documentation index](README.md) · [Adoption](adoption.md) · [Release trust](release-trust.md)

## Per-project runtime selection

A project records a full Git SHA in `chainman.lock`. The bootstrap verifies that
commit's Git objects and reads its initial entrypoint directly from the Git object,
then that entrypoint materializes the selected tree. It never runs an unchecked
cached checkout. The runtime handles configuration, Nix, containers, services, and
update transactions. See [release trust](release-trust.md) for the integrity boundary.

The source checkout used during initialization is disposable. Project flakes and
locks remain independently owned; they do not import chainman.

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

Host-Nix mode requires Nix 2.24 or newer and uses the selected host installation. An
explicit `CHAINMAN_NIX_BIN` must be an absolute executable path. chainman does not
replace the host's Nix. The same project flake supplies its language tools.

A profile named `host` means the already bootstrapped execution context. In
container mode that context remains in the container; it does not escape to the
physical host. Existing wrappers that start Nix or Docker need review before being
invoked inside a managed task. See [adoption](adoption.md#existing-justfiles-and-host-wrappers).

## Caller-installed tools: discouraged escape hatch

```sh
CHAINMAN_MODE=host just chainman exec -- node --version
CHAINMAN_MODE=host just chainman run check
```

This mode requires your own Python 3.12+, Bash when requested by the workflow,
and every project tool. chainman installs none of them. Use it only if you intend
to maintain and diagnose that environment yourself; it does not establish a
reproducible toolchain or qualify the project for managed updates.

The Git pin and object checks still apply. The runtime executes from a private
export of that revision, retained for the command's lifetime. It preserves your
PATH and tool/cache settings and applies declared project/profile environment.
It does not evaluate flakes, execute their shell hooks, or provision compiler
caches. A profile's environment values still apply; its flake is not entered.

Supported operations are `exec`, `shell`, ordinary tasks, project setup,
`setup-status`, `version`, `doctor`, `config`, and `explain`. Setup commands can
install project dependencies using your existing tools. Readiness is distinct
from either Nix mode; switching modes requires setup to be checked again.

For Python virtual-environment readiness, host setup uses `python3` on your PATH
unless you select an absolute executable with `UV_PYTHON`. The installer receives
that same selection. Supply the project's required Python version yourself;
chainman's Python 3.12 minimum does not establish compatibility with the project.

Managed services, dependency/runtime updates, transactional formatting,
initialization, and tasks requiring native timeouts or child-process containment
require `host-nix` or `container-nix`. An unsupported task anywhere in a requested
graph rejects the graph before setup or task commands run. Standard recipes also
check every bound task and its dependencies before their first step, while keeping
the declared sequential execution order. chainman does not
silently disable those task contracts. Host mode does not prune managed caches.

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

Nix source roots live under the host chainman runtime-roots cache or the owned
container store. Temporary environment roots remain while their managed operations
need them. Runtime generations coexist across projects and updates. Cache cleanup
must respect active operations and service ownership.

Nested commands in an active Nix environment verify the pinned Git tree again
and reuse the matching Nix-store runtime. A fresh temporary Git export does not
invalidate setup readiness. A changed pin or mismatched runtime requires leaving
the active environment and entering it again.

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
