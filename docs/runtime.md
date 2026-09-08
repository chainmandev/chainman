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

The container runs project commands as the calling UID/GID. Nix runs without a
build-users group inside the container, including when the caller is namespace
root; this does not require adding container capabilities. A short preparatory
container owns only its named Nix and download volumes, never a writable host project mount.
Volumes are scoped by user and explicit architecture; Docker and Podman maintain
separate engine stores. Project outputs live separately under `.cache/toolchain/work`.
Package managers own locking in shared downloads. Rust compiler cache servers run
in the foreground and retain the project operation lock until they exit. Cold Nix
environment realization completes before the cache-server readiness deadline starts.
A process
that is forcibly killed can leave children holding that lock; inspect those processes
before stopping an exact compiler endpoint. Do not remove a live operation lock.

Cache reporting distinguishes project builds, shared downloads and free disk bytes.
Limits and stale age live in `cache`; automatic pruning removes only old declared
build contexts after acquiring the project lock. `clean` clears those same contexts.
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
