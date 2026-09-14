# Runtime and distribution contract

For an adopted repository, project shells use the local Git flake source: tracked
working-tree edits are visible, while untracked package downloads and build caches
stay out of the Nix store. Add new flake inputs to Git before importing them. Copies
without their own Git repository use a path source and never borrow a parent
repository as their project. Installed runtimes execute from verified Nix-store
sources, separate from project code and rehashed before each launch.

The checked-in launcher, companion Nix expression, lock and selected Nix/container
bootstrap are the trust base. The lock identifies a version and source revision,
an HTTPS release URL and SHA-256 SRI NAR hash. Git revision identity and archive
integrity are separate: the NAR hash covers executable bits, paths and unpacked
bytes regardless of the source repository's Git object format. HTTPS metadata is
release-authority input, not a signature or a separate transparency service.

Nix verifies the archive before its flake or Python code runs. A bundled archive or
`CHAINMAN_ARCHIVE` override must match the same pin. Source archives contain regular
files only; symlinks are rejected. Bootstrap rehashes the fetched Nix-store source
before evaluation and executes it directly. There is no second project-local runtime
installation or mutable current pointer. Concurrent fetching and atomic store
installation are Nix responsibilities. Existing project-local generations are not
executed; they can be removed after stopping old sessions.

Bootstrap fetches the source with `nix build --out-link`, registering a normal
[Nix garbage-collection root](https://nix.dev/manual/nix/2.28/command-ref/new-cli/nix3-build)
before that evaluator exits. Roots are keyed by the pinned NAR hash under
`${XDG_CACHE_HOME:-$HOME/.cache}/chainman/runtime-roots` on hosts and
`/nix/var/nix/chainman-runtime-roots` in the selected Nix container volume.
They retain runtime source archives across nested commands and concurrent versions;
they do not install a Nix executable or retain every project SDK. Old source roots
may be removed after all sessions using those versions stop. Nix then decides
when to collect the unreferenced source. Runtime-cache directory symlinks are refused.
Existing content-keyed roots are reused without replacing their symlinks; each
entry still fetches and verifies the selected archive and checks the exact root
target. This avoids temporary-link collisions between equal PIDs in separate
containers sharing a Nix volume. Concurrent first registration can reuse another
writer's completed root only after the same verification succeeds.
If the root inventory is unavailable, bootstrap re-fetches and registers the
verified runtime before dispatch. A failed registration remains a failed command.
This handles the reproduced Nix temporary-root inventory race without assuming
that an existing symlink alone proves registration or retrying project commands.

A stop request announces a durable cancellation ticket before acquiring the
service mutation lock. Startup checks that ticket while waiting for readiness,
including waits in shared repository scopes; Ctrl-C and termination use the same
cleanup path. A stop interrupted before acknowledgement remains pending until an
explicit stop completes recovery. Later starts accept completed tickets. Process
Compose remains responsible for the actual readiness probes and thresholds.

Native task helpers have temporary GC roots for the duration of command execution.
Controller export similarly retains its Nix package until the standalone binaries
have been copied. The temporary directories, including their roots, are removed
when these operations finish or fail; Nix handles later collection normally.
Runtime updates and inspection likewise register fetched sources before the Nix
evaluator exits and retain them through their final reads or execution. Dependency
audit tool packages remain rooted for the complete audit. Controller export reads
licenses while its package root is still held.

Container command probes validate the engine, owner label and running state, then
execute against the inspected immutable container ID. A stopped, missing or replaced
container cannot satisfy readiness merely by reusing a service's name.

The container maps project commands to the calling user's ownership: rootful Docker
uses the caller's UID/GID, rootless Docker uses its mapped `0:0`, and Podman uses
`keep-id`. A failed Docker identity probe stops before execution. Nix runs without a
build-users group inside the container, with all capabilities dropped and
`no-new-privileges` set. Before Nix evaluates or runs project commands, mapped UID 0
makes the container root directory mode `0555`. Other users already lack root-directory
write access. This keeps tools from accidentally creating Nix's nonexistent build
HOME, `/homeless-shelter`, and breaking later derivations. `/tmp` remains disk-backed
and writable, as do the declared project, HOME, Nix and download mounts. This is a
build-purity invariant for trusted workflows, not a sandbox against project code;
the container's root owner can change its own directory permissions.
A short preparatory
container owns only its named Nix and download volumes, never a writable host project mount.
Daemon compatibility checks use each engine's inspection representation: Podman
normalizes digest-pinned image names and exposes resolved capability sets and an
explicit private PID mode. A matching daemon can be reused without recreating its
store volume. Legacy-client detection uses Podman's label filter, which also
works on 4.x engines without Docker's per-label display accessor.

Volumes are scoped by user and explicit architecture; Docker and Podman maintain
separate engine stores. One unmodified upstream Nix daemon owns each container
store's state. Project containers connect through its Unix socket in the Nix
volume; they do not run independent local-store writers with conflicting PID
namespaces. The daemon runs as the same mapped user, with a read-only container
root, all capabilities dropped, no new privileges, no published ports, no host
project mounts and no container-engine socket. Its trusted clients already own
the same per-user Nix volume; it is not a privileged service for other users.

Managed temporary GC roots live under `/nix/tmp`, where both clients and the
daemon can see them. Application temporary-directory settings remain separate.
The small idle daemon remains available between commands and is started again
by the next bootstrap if stopped. Its container is named `<nix-volume>-daemon`.
After stopping all clients of that volume, the engine's normal stop/remove
commands can remove the daemon while retaining the cached Nix volume. Bootstrap
refuses to adopt a daemon with a different pinned image, ownership or isolation
configuration; changing those settings requires stopping its clients first.
The initial migration also refuses to run while containers from the earlier
local-store arrangement still use that volume; it does not stop those clients.
Host Nix continues using its own existing store arrangement.

Project outputs live separately under `.cache/toolchain/work`.
Named workflows apply the shared age and size pruning policy before acquiring
their setup artifacts, only when no managed operation is active. Set
`cache.automatic_prune = false` to keep pruning explicit.

`setup-status [GROUP...]` reports the same fingerprints and artifact readiness
used by `setup`, returning 1 if any selected group is stale. It never installs
dependencies or records unverified readiness. Use `setup [GROUP...]` to restore
readiness after changing inputs or removing outputs.
The shared pnpm defaults keep a project-local virtual store backed by the shared
download cache, independent of whether `CI` is set. `pnpm run` and `pnpm exec`
check dependency freshness and report an error instead of reinstalling a leased
setup. Explicit install/update commands still perform their declared work.
Declare `--config.confirmModulesPurge=false` on frozen pnpm install commands,
as the shared JavaScript module does. It permits rebuilding their owned module
directory without a terminal prompt when migrating an earlier virtual-store
layout. This pnpm install option has no environment-setting equivalent; it does
not enable installation during ordinary task execution.
Current pnpm and older environment-setting aliases are reconciled through the
project/profile environment layers; projects selecting another layout should
keep that choice consistent across setup and task execution.
Setup groups may declare `exclude_inputs` glob patterns when broad manifest
patterns would otherwise include installed dependencies or build directories.
Exclusions affect only that group's fingerprint inputs, never its artifact checks.

`CHAINMAN_CONTAINER_PLATFORM=linux/amd64|linux/arm64` explicitly selects the Nix
container architecture, including planning and later service invocations. The
engine must support that architecture. The selected architecture has its own Nix
and download volumes, and saved service plans retain the selection. An explicit
`--platform` in a container options file uses the same mechanism.
`CHAINMAN_NIX_VOLUME` selects a dedicated Nix volume when an isolated cache is
needed. The default remains shared by user; an architecture suffix is appended
when selected, and the paired download volume adds `-downloads`. Use a volume
reserved for Chainman, since its Nix directories and ownership are initialized.

Standalone tasks can opt into `CHAINMAN_CONTAINER_NETWORK_MODE=host` on engines
that support host networking. This is useful for an audit against a server bound
only to host loopback. The default is `bridge`. In host network mode, `{host}` and
`{bind}` both expand to `127.0.0.1`; published port mappings are omitted and the
process's listening port is used directly. Setup fingerprints distinguish this
mode. Service workflows use their declared, owned namespaces and reject a host
network override. Host mode (`CHAINMAN_MODE=host-nix`) already uses host loopback.

Git administrative mounts belong only to a repository whose root is the selected
project, including linked worktrees. Nested unadopted examples receive global/system
Git identity and signing policy without mounting or inheriting their enclosing repository.
Disposable update candidates also disable automatic Git maintenance. Update tasks
receive only their isolated candidate checkout and a dedicated, ignored
workspace-transaction root on the checkout's own rename domain; they do not receive
the updater's private control directory or original checkout.
Ordinary managed commands receive `.chainman/workspace-transactions` as their
consumer-created location through the same environment contract, keeping atomic
staging inside the mounted project without treating it as project source.
Independent ordinary commands and shells may run concurrently in one project.
Each public managed command owns an inherited advisory lease. Updates and cleanup take
an exclusive writer gate and refuse to proceed while an independent family is
active. A nested update can acquire that gate when only its own ancestors remain
active; background managed commands from the same shell have distinct leases.
Its execution lease stays held even if admission fails or the updater
is killed. Cleanup remains forbidden inside an active managed operation.
Automatic pruning runs only while no execution or ancestor for that project is active; otherwise
it is deferred. Stale lease files are reclaimed only after their lock is free.
Module setup and use remain exclusive for their complete lifecycle, so another
context cannot reinstall an environment while it is in use. Direct execution and
declared application commands can run concurrently; projects still own the safety
of arbitrary application commands and unmanaged background processes.
All ancestor leases remain inherited when entering another project. New executions
also hold a shared legacy lock, preventing an older runtime from performing cleanup.
An incoming old exclusive runtime can hand off to the new pin. Explicitly executing
older runtime code from a new ordinary session fails closed; use the pinned launcher.

Package managers own locking in shared downloads. Rust compiler cache servers run
in the foreground and retain their execution lease until they exit. Independent
executions own distinct server sockets and share the on-disk compiler cache. Cold Nix
environment realization completes before the cache-server readiness deadline starts.
The owned server has no idle timeout, so a long non-Rust phase cannot outlive it.
Cleanup reaps an already-exited launcher, checks the actual compiler process lifetime
lease before removing its unchanged socket, and reports a nonzero server exit. A process
that is forcibly killed can leave children holding that lock; inspect those processes
before stopping an exact compiler endpoint. Do not remove a live operation lock.

The consumer bootstrap shell includes standard-library Python, Git, Just and basic
command-line tools. Dependency operations enter the separate `updates` shell for
parsing and policy libraries only when needed. It does not include Chainman's source
formatters or a C compiler; development and optional language profiles supply
their own tools. Source development continues to use the full `core` profile.
After verifying the installed generation, the launcher reuses that same pinned
bootstrap interpreter instead of entering a duplicate bootstrap environment.

Host mode retains the installed host Nix; container mode retains the pinned upstream
image's Nix. `CHAINMAN_NIX_BIN` explicitly selects an absolute host executable.
Bootstrap checks Nix >= 2.24 using the evaluator version, so vendor-specific version
strings do not determine compatibility. Failure stops before runtime evaluation;
Chainman never installs a replacement Nix. Supported platform qualification is a
separate release gate, not implied by passing that minimum-version check.

The selected executable family remains ahead of project tools after shell refreshes.
`CHAINMAN_RUNTIME_NIX_BIN` is internal routing for that selection, not a separately
packaged Nix. Bootstrap invalidates inherited project-profile tokens. Project language
and SDK versions remain pinned by their own flakes. There is no Chainman Nix patch.
The upstream container image is pinned by digest and updated deliberately.

Cache reporting distinguishes project builds, shared downloads and free disk bytes.
Limits and stale age live in `cache`; automatic pruning removes only old declared
build contexts after acquiring exclusive maintenance access. `clean` clears those same contexts.
Application outputs elsewhere require application-owned cleanup. Build contexts
and their parent directories cannot be symlinks. Links inside a context, including
compiler-generated references to source files and download caches, count only
their own bytes and are removed without following or modifying their targets.
Actual deletion failures fail visibly. Host-wide Nix GC and container volume
removal are explicit operator actions and can affect other projects. SDK removal is
separately opt-in and restricted to declared disposable hosted CI locations.

`release-files.json` is the archive allowlist. The release builder reads committed
regular-file blobs and executable modes, never Git history, ignored caches or local
build products. The consumer generator verifies both flat archive checksum and NAR
hash before copying templates and examples. The release emits a runtime archive and a separately hashed source archive.
Development tests, examples, templates and authoring utilities belong to the source
archive. The consumer generator verifies both products and their shared file
identities; every generated consumer bundles only the runtime archive at `vendor/chainman/chainman.tar.gz`. Remove that optional file
and its lock field only after its pinned public URL is available.

Disposable update and staged-format repositories disable Git's automatic
maintenance. Their administrative directories are hashed as transaction inputs,
so a detached repack must not race that identity read or outlive cleanup.

`just release` builds a local candidate; it does not authorize publication.
`just control-release-check` enforces at least 30 days of maturity for the native
backend inputs recorded in `nix/control-sources.json`. Process Compose 1.122.0 is
the candidate because it fixes stopping processes waiting on dependencies; its
publication gate opens on 2026-09-16 at 23:01:55 UTC. Qualification may run before
that date, but publication must not bypass the gate. `just control-test` exercises
the actual pinned backend and cross-compiles the ownership adapter for all four
targets. Cross-compilation does not substitute for execution on each supported OS
and engine.

For Just shebang recipes, use
`#!/usr/bin/env -S ./scripts/chainman.sh script` (optionally adding
`--profile NAME`). The host launcher reads Just's temporary Bash script and
passes its contents as a literal argument to Bash in the selected environment.
It preserves script arguments, `$0` and the caller's stdin, so the container does
not need a mount of the host's temporary directory. This uses the ordinary
`exec` environment and operation lease; declare a named task for bounded child
cleanup, setup dependencies or managed services.

The default `GRADLE_OPTS` disables persistent Gradle daemons and selects Kotlin's
in-process compiler. Gradle can still start a single-use JVM for project JVM
settings; it exits after the build. This retains download and compilation caches
without leaving an idle build process after setup or verification. Explicit
project/profile environment options take precedence. See the upstream
[Gradle daemon contract](https://docs.gradle.org/current/userguide/gradle_daemon.html)
and [Kotlin execution strategies](https://kotlinlang.org/docs/compiler-execution-strategy.html).

Container clients reserve `NIX_CONFIG` as well as the Nix connection variables.
Chainman restores the standard daemon store setting after external flake shell
hooks, because a `store` setting in `NIX_CONFIG` overrides `NIX_REMOTE`.
Host Nix retains the operator's configuration.

See [explicit composition and configuration inspection](composition.md) for reusable
workflow declarations and the versioned generator/inspection contract.
