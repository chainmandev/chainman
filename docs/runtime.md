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

Tracked project flakes use a Git source reference so untracked package caches do
not enter the Nix store. A shallow checkout explicitly sets `shallow=1` on that
reference, including when the flake lives in a subdirectory. It remains a shallow
source snapshot; Chainman does not invent a revision count or complete history.

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

## Caches and temporary transactions

| Cache | Role |
|---|---|
| User-local bare Git objects | Downloaded source objects, keyed by canonical source and commit |
| Nix store and lifetime roots | Verified runtime source and executable tool environments |
| Project/download caches | Package downloads, setup artifacts, and build outputs |
| Temporary update candidates | Disposable diagnostics with bounded retention |

The Git cache is under `${XDG_CACHE_HOME:-$HOME/.cache}/chainman/git/`. It has no
mutable “current” pointer. Concurrent first use publishes an initialized cache
before fetching; an interrupted fetch can be retried. Warm bootstrap performs no
network request when the required objects are present. Integrity failures stop
execution rather than selecting a different revision.

Offline warm **bootstrap** does not guarantee offline project execution: a newly
requested Nix environment, package manager command, or network-dependent test can
still need downloads. Keep the relevant environments and project dependencies
available if offline operation is required.

Nix source roots and the matching bootstrap interpreter/Git environment live
under the host chainman runtime-roots cache or the owned container store. The
bootstrap profile stays rooted after Nix hands execution to Python, so a consumer
profile refresh cannot collect the runtime's interpreter. Temporary environment roots remain while their managed operations
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

### Shared storage retention

```sh
just chainman storage-status
just chainman storage-prune
just chainman storage-prune --all
```

In the source checkout, omit `chainman`. These commands report JSON inventories,
collection eligibility, ownership/retention reasons, paths actually removed, and
per-entry errors. Inspection failures preserve the affected entry; successful
removals elsewhere are still reported and errors produce a nonzero exit status.
`--all` bypasses age/size thresholds, never ownership or recovery checks. Existing
project-cache and update-candidate commands keep their separate meanings.

New managed package homes live under the download root's
`.chainman-storage-v1/downloads/data/`. Recognized Cargo, Go, Gradle, Pub, pnpm,
uv and pip download payloads share a **16 GiB** budget and **30-day** idle age.
Over-budget maintenance removes oldest cache families until under budget. It
does not delete enclosing package-manager homes, credentials, configuration,
installed tools, or arbitrary SDK directories. Compiler caching retains its
separate configured budget (8 GiB by default). Custom download-root overrides
remain caller-managed; host and owned-container download stores are independent.
First use copies recognized regular legacy configuration/credential files into
private new homes and links existing installed-tool directories; the legacy
originals stay in place. Subsequent configuration edits belong to the selected
home. Indirect or unusual legacy configuration requires explicit migration.

Admission and completion perform maintenance. A shared admission gate and
inherited lifetime leases protect all users of each managed download root,
including native service/task children. Active caches can exceed the budget.
Eviction changes the cache epoch before deleting any payload, invalidating setup
evidence even after a partial failure; the ordinary setup policy controls repair.
There is no background collector. `cache.automatic_prune` continues to control
project build outputs; shared storage has its own fixed retention policy.

Runtime source and bootstrap-interpreter roots use a versioned, leased pool under
`chainman/runtime-roots/` (or the owned container's Nix state). Temporary roots
protect cold registration and lifetime leases protect execution. Generations
remain leased while an interactive job is suspended. Ctrl-Z returns control to
the calling shell; `fg` resumes with terminal ownership and `bg` leaves ownership
with the shell. Cancellation and completion restore borrowed terminal settings.
Generations unused for 30 days lose their Chainman GC roots; ordinary Nix GC
decides whether their store objects are still reachable elsewhere. Root inventory counts link
bytes, not closure size, and never runs global host Nix GC.

Native service binaries share verified content-addressed files through stable
scope-local hardlinks, with a copy fallback where hardlinks are unsupported.
Unreferenced executable generations expire after 30 days. Current plans and
ownership/recovery receipts protect their assets; missing worktrees alone never
authorize discarding database, volume, network or resource identities. Abandoned
plans without those obligations may expire after 30 days. Admission lock inodes
remain stable. Service commands attempt maintenance at most once per day; explicit
maintenance is immediate. Container maintenance reports that service state belongs
to the host; inspect it using a host-Nix entry.

Legacy download directories and runtime roots remain untouched. Older pinned
runtimes do not provide the new lifetime protocol, so updating a pin enables
future retention without making old state automatically collectible. Inspect and
remove obsolete legacy artifacts separately when their consumers are idle.

### Temporary update candidates

New source and consumer update transactions live in
`${XDG_CACHE_HOME:-$HOME/.cache}/chainman/updates/v1/`. These workspaces are
**disposable diagnostics, not backups**. Copy useful edits elsewhere yourself;
chainman does not preserve work merely because it happened inside a candidate.

Successful candidates are removed once their owned processes have stopped.
Inactive failed or interrupted candidates are kept for at most 24 hours, subject
to a shared 12 GiB budget. Oldest candidates are removed first; an oversized
candidate can disappear immediately after failure. Resume is best-effort, not a
durability guarantee. Failure output reports whether a candidate remains.

Maintenance runs at update entry and completion, or explicitly:

```sh
just chainman update-cache-status
just chainman update-cache-prune
just chainman update-cache-prune --all
```

In the chainman source checkout, omit `chainman` from these commands. Status emits
JSON with transaction paths, sizes, active state, expiry timestamps, current
collection eligibility and per-entry inspection errors. Pruning reports the paths
actually removed even when another entry cannot be inspected; the command exits
nonzero if any inspection or removal failed. `--all`
discards all recognized **inactive** candidates, including experimental edits.
These commands do not change the existing project build-cache commands.

Expiry is checked on the next maintenance invocation; no background timer is
installed. The budget excludes active transactions and is not an in-build quota.
Lifetime leases protect running work and surviving children, including detached
service owners. Container witnesses also prevent removal while an engine still
retains a transaction's containers. Receipts bind each engine to its daemon
identity: an unavailable or changed daemon protects that candidate while cleanup
continues for other independently verified idle candidates. Restore the original
engine context to collect a protected candidate. Older container receipts without
a recorded daemon identity require manual inspection and are never automatically
adopted into the current context. New receipts use schema 2 so older collectors
skip them; use the updated commands for maintenance.

An update already running under a schema-1 supervisor keeps that receipt format
and its original update helper, even when candidate services use a newer runtime.
The newer helper, or an updated maintenance pass, adds a small collection guard
using a field that the old writer preserves. Old collectors treat that guard as
an active witness whenever real engines have been registered, including engines
registered later. This prevents a context switch from making the old collector
delete a candidate still used by another daemon. Updated status reports the
missing daemon identity; these older container candidates require manual
inspection even after successful updates. A current daemon identity cannot prove
which daemon owned an earlier container.

Host-only legacy updates retain normal completion, cleanup and resume behavior.
A newer supervisor removes the guard from the engine list and migrates an older
host-only receipt when explicitly resuming it, after verifying that all previous
owners have stopped. Engine registration cannot migrate receipts; use the helper
supplied by the supervisor. The guard uses only the host shell, stays inside the
candidate, and is deleted with it. Status alone does not install guards. Updates
started under the new supervisor use the normal identity checks and retention
limits without this compatibility guard.
Do not manually wipe a cache during active operations: disposable does not mean
safe to remove concurrently.

Legacy `updates/candidate.*` directories have no compatible lifetime protocol.
They remain explicitly resumable but are never automatically adopted or deleted.
Unknown, symlinked and foreign-owned entries are likewise not cleanup targets.
Inspect legacy workspaces manually after their operations have stopped. Existing
consumer pins must be updated to receive the bounded-retention behavior.

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
