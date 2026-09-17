# Git hooks and staged formatting

[Guide index](README.md) · [Recipes](recipes.md) · [Configuration](configuration.md)

The optional hook preset uses pinned lefthook, supplied by the selected chainman
revision. No global lefthook, Node, Python, pnpm or Corepack installation is needed
in either Nix mode. The starter declares the preset; existing projects opt in:

```toml
[hooks]
enabled = true

[formatters.text]
paths = ["*.md", "*.json", "*.ts"]
profile = "format-text"
write = ["prettier", "--write", "--"]
check = ["prettier", "--check", "--"]
```

Use a project-owned formatter profile when you need a particular formatter
version or plugins. `format-text` supplies the runtime's pinned Prettier, Ruff,
Taplo, shfmt and nixfmt. `format-rust` supplies rustfmt without preparing Cargo
dependencies. Keep formatter environments small. A formatter's optional `setup`
array names only its own required installation groups. A staged-format transaction
authorizes those groups inside its disposable snapshot, even in a noninteractive
hook; it never prepares the original project or unrelated application groups.
Commands receive batches of literal `./path` arguments; include the tool's `--` separator where supported.
`paths` and optional `exclude` use shell-style patterns; `**/` also matches files
at the repository root. For a formatter that accepts one file on stdin and emits
only formatted bytes on stdout, set `stdin = true`. No filenames are appended in
this mode. For example, `rustfmt --edition 2024 --emit stdout` formats a single
Rust source without traversing sibling modules; add `--check` to its check command.
Supply an explicit config path if the project keeps settings outside its root.
Symlinks and submodule contents are never formatted.

Add short project-owned recipes alongside the complete bootstrap:

```just
[positional-arguments]
setup *args:
    #!/bin/sh
    exec just chainman setup "$@"

format-staged:
    @just chainman format-staged

[positional-arguments]
hooks +args:
    #!/bin/sh
    exec just chainman hooks "$@"
```

## Setup and ownership

```sh
just setup
just hooks status
just hooks config
just hooks install
just hooks uninstall
```

Complete setup prepares **every** declared setup group, runs the project's
`recipes.setup` extensions, then validates and installs hooks. Raw
`just chainman setup` has the same contract. `just setup --no-hooks` is the explicit
CI/disposable-checkout lane. Targeted `just chainman setup GROUP` and automatic
repairs never install hooks. Initialization generates files and commits them; it
does not run setup. Outside a Git project root, hook installation reports that it
does not apply.

The installed shell bridges call the repository's unchanged bootstrap. They live
in that worktree's Git administration directory. Installation is idempotent and
refuses to overwrite a different hook manager, existing hooks, or modified
bridges. Resolve the reported `core.hooksPath` conflict deliberately; chainman
does not delete another manager's configuration. Uninstall removes only intact,
owned bridges and their worktree setting. An unrelated common hook setting is
preserved. Git, just and the chosen Nix/container engine must be on the Git
client's PATH, including for graphical clients.

Linked worktrees, including those backed by a bare repository, keep separate
hook settings. When first enabling Git's worktree configuration, installation
moves shared `core.bare` and `core.worktree` values to the primary repository's
worktree configuration before activation. Other checkouts keep their identity
and hook settings. Configuration contention stops installation; a failed
installation restores the previous configuration and bridges. If those shared
values come from included Git configuration, configure the worktree settings
explicitly first; chainman reports this before changing them.

## What pre-commit does

Pre-commit formats staged content. It runs **no lint fixes, type checking,
generation, builds, or repository-wide formatting task**. If no declared
formatter matches, it does not prepare any formatter environment.

The transaction snapshots Git's active index, including `GIT_INDEX_FILE` and the
temporary index used by `git commit -a` or a path-limited commit. Formatter
configuration and files come from staged blobs; orchestration declarations are
frozen independently. Existing working-tree edits cannot silently become staged.

For a fully staged file, the formatted result updates both index and worktree.
For a partially staged file, chainman formats its staged version and three-way
merges that formatting into the working version. Only formatted staged content
enters the index. A genuine merge conflict, concurrent edit, formatter failure or
out-of-scope output stops the transaction. It does not stash, reset, run clean
filters, or delete Git-owned lockfiles. Resolve a reported conflict by staging a
coherent version or formatting that file manually, then retry.

You can commit while an independent managed development command or shell is
running. Only one staged-format transaction runs per worktree; a second attempt
asks you to retry. Cleanup and verified updates remain excluded while formatting
is active, and concurrent changes to the index or selected files still stop
application of the formatted result.

Before applying anything, the transaction computes every merge and acquires the
active index's own lock. If interrupted while applying, recovery material remains
under `.chainman/staged-format/transaction-*`: `original-index`, numbered original
and merged files, staged snapshot and `apply.json` with paths/modes. Subsequent
formatting stops until you review that directory. Compare these files with the
current index/worktree before restoring anything; preserve subsequent edits.
After resolving the interrupted application, remove its transaction directory
and retry. The ordinary update `resume=` command is for update transactions, not
staged formatting. Bare-host mode does not support transactional formatting.

For container hooks, alternate indexes outside the repository must live in a
dedicated directory that can be mounted; an index directly in `/tmp` or `$HOME`
would require exposing the whole directory and is rejected with guidance.

## What pre-push does

The default scan checks source blobs in **all outgoing commits**, including
intermediate commits removed from the final tree. It handles multiple ref updates,
new refs, force pushes, annotated tags and deletions. With a missing remote base,
it explains that it is conservatively scanning locally reachable history. It never
silently skips that history or fetches remote objects during the hook.

The separately hash-pinned upstream
[anti-trojan-source](https://github.com/lirantal/anti-trojan-source) library detects
suspicious Unicode characters. It does not detect arbitrary malware or certify
dependencies. Clean results are cached by scanner identity, policy and blob hash;
diagnostics identify commit, path, line, column and character. Each hook check gets
the original pre-push ref stream independently. Setup prompts use the controlling
terminal, leaving that stream untouched.

Source-classified files are scanned as UTF-8, including embedded NULs; unsupported
encodings fail with a commit/path diagnostic. Only the implicit executable-file
fallback skips recognized native binaries, reporting the skip without caching a
clean-source result. The classification identity invalidates older clean caches.
Traversal inventories the first outgoing tree, then its successive differences;
it retains one diagnostic location per distinct source blob, not every unchanged
occurrence in history. Every outgoing tree is still covered, including intermediate
changes and merge resolutions. Large history walks report progress on stderr.

Scan a particular committed tree explicitly:

```sh
revision=$(git rev-parse HEAD)
just chainman trojan-source "$revision"
```

Defaults include common source formats (including Dart, Astro, Nix and Just),
Justfiles, Dockerfiles and executable regular files regardless of extension.
The exact patterns are in the runtime's `scripts/trojan_source.py`. Override `hooks.trojan_source.paths` to select project source formats. A narrow
exception names the **exact path and blob**, plus a reason; editing that file
invalidates the exception:

```toml
[[hooks.trojan_source.exceptions]]
path = "tests/unicode-fixture.ts"
blob = "0123456789012345678901234567890123456789"
reason = "Intentional bidirectional-character scanner fixture"
```

Obtain the real blob identity with `git rev-parse HEAD:tests/unicode-fixture.ts`.

## Project overrides and additional checks

```toml
[hooks]
enabled = true
config = "lefthook.yml"
```

```yaml
pre-push:
  commands:
    project-policy:
      run: '"$CHAINMAN_HOOK_ENTRY" run check-policy'
```

The runtime composes its preset with that file using upstream lefthook `extends`.
Use named commands to override defaults or add checks; `just hooks config` prints
the effective configuration. `CHAINMAN_HOOK_ENTRY` is an internal pinned-runtime
bridge available to hook commands, not a global installation. It replays saved
pre-push input and dispatches ordinary finite project tasks. Project-specific
malware rules, credentials, services and policy remain project-owned. In
particular, ATHL's DPRK scanner is not part of this preset.

A composed pre-push check can use `CHAINMAN_HOOK_REMOTE_NAME` and
`CHAINMAN_HOOK_REMOTE_URL` for Git's literal remote name and destination. Quote
these variables in shell commands. This supports project publication/privacy
gates that need both the destination and the independently replayed ref stream.
