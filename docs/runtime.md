# Runtime and distribution contract

For an adopted repository, project shells use the local Git flake source: tracked
working-tree edits are visible, while untracked package downloads and build caches
stay out of the Nix store. Add new flake inputs to Git before importing them. Copies
without their own Git repository use a path source and never borrow a parent
repository as their project. Installed runtimes execute from verified project-local
source generations, separate from project code and rehashed before each launch.

The checked-in launcher, companion Nix expression, lock and selected Nix/container
bootstrap are the trust base. The lock identifies a version and source revision,
an HTTPS release URL and SHA-256 SRI NAR hash. Git revision identity and archive
integrity are separate: the NAR hash covers executable bits, paths and unpacked
bytes regardless of the source repository's Git object format. HTTPS metadata is
release-authority input, not a signature or a separate transparency service.

Nix verifies the archive before its flake or Python code runs. A bundled archive or
`CHAINMAN_ARCHIVE` override must match the same pin. Source archives contain regular
files only; symlinks are rejected. Bootstrap stores source generations under
`.chainman/<sha256-of-NAR-SRI>`, rehashes them on every launch and refuses local edits.
A bootstrap lock and staging directory protect concurrent/repeated installation.
Only a complete verified directory is renamed into place. Previous generations are
retained; no mutable current pointer is followed. The filesystem assumptions are
ordinary local POSIX directories, atomic same-filesystem rename and cooperating
process locks. Hard power-loss durability of every downloaded byte is not claimed;
a later launch rechecks identity and fails on corrupt state.

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
Volumes are scoped by user and explicit architecture; Docker and Podman maintain
separate engine stores. Project outputs live separately under `.cache/toolchain/work`.
Git administrative mounts belong only to a repository whose root is the selected
project, including linked worktrees. Nested unadopted examples receive global/system
Git identity and signing policy without mounting or inheriting their enclosing repository.
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

The consumer bootstrap shell includes the runtime and its Python libraries, Git,
Just, and basic command-line tools. It does not include Chainman's source
formatters or a C compiler; development and optional language profiles supply
their own tools. Source development continues to use the full `core` profile.
After verifying the installed generation, the launcher reuses that same pinned
bootstrap interpreter instead of entering a duplicate bootstrap environment.

After bootstrap, the verified runtime shell supplies the Nix executable family ahead
of project tools, including after shell refreshes. Other languages still come from
the selected project shell. `CHAINMAN_NIX_BIN` selects the initial bootstrap only;
`CHAINMAN_RUNTIME_NIX_BIN` is internal and cannot be set in project configuration.
Bootstrap entry invalidates a previous project-profile token, so nested launchers reload
the actual project shell. The runtime includes Nix's CLI and Linux namespace helper;
Nix's combined development package and generated manuals are excluded from the
consumer shell.
The pinned Nix 2.34.8 includes a narrow patch for renaming read-only owned output
directories as capability-free root. It preserves fresh-inode copying, hash checks
and original modes, including restoration on rename failure. Updating Nix requires
reviewing or retiring that version-bounded patch; it is never applied speculatively
to a new version. In the Chainman source checkout, run `just verify-nix` on the
host to repeat the full patched Nix package build and its upstream unit/functional
gates after changing either the patch or pin. A fresh store without a cached patched
build compiles Nix and needs its build dependencies, even with the small consumer
bootstrap profile. Host and container stores are separate; published binary-cache
coverage would reduce this first-entry cost without changing the runtime pin.

Cache reporting distinguishes project builds, shared downloads and free disk bytes.
Limits and stale age live in `cache`; automatic pruning removes only old declared
build contexts after acquiring exclusive maintenance access. `clean` clears those same contexts.
Application outputs elsewhere require application-owned cleanup. Symlink escapes
and actual deletion failures fail visibly. Host-wide Nix GC and container volume
removal are explicit operator actions and can affect other projects. SDK removal is
separately opt-in and restricted to declared disposable hosted CI locations.

`release-files.json` is the archive allowlist. The release builder reads committed
regular-file blobs and executable modes, never Git history, ignored caches or local
build products. The consumer generator verifies both flat archive checksum and NAR
hash before copying templates and examples. Every generated consumer bundles the
same release archive at `vendor/chainman/chainman.tar.gz`. Remove that optional file
and its lock field only after its pinned public URL is available.
