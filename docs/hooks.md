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
Symlinks and submodule contents are never formatted. Paths must be valid UTF-8;
unsupported filename encodings stop the operation before application.

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

Ordinary checkouts use a relative hook path, so moving the checkout preserves its
hooks. Linked worktrees use their Git administration directory; repair Git's
worktree links after relocating the owning repository, then run `just hooks install`.
Installation records its selected setting there and can repair a relocated owned
setting when that record and the intact bridges agree. Legacy moved installations
without an ownership record require the explicit recovery steps in the diagnostic.
Installation and removal share an administrative lock and preserve configuration
and bridges on catchable failure. A competing operation asks you to retry.

Linked worktrees, including those backed by a bare repository, keep separate
hook settings. When first enabling Git's worktree configuration, installation
moves shared `core.bare` and `core.worktree` values to the primary repository's
worktree configuration before activation. Other checkouts keep their identity
and hook settings. Configuration contention stops installation; a failed
installation restores the previous configuration and bridges. If those shared
values come from included Git configuration, configure the worktree settings
explicitly first; chainman reports this before changing them.
First activation also refuses dormant worktree identity, hook-path or include
settings: enabling them could silently change another checkout. Review those
files before retrying; chainman does not overwrite them to force installation.

## What pre-commit does

Pre-commit formats staged content. It runs **no lint fixes, type checking,
generation, builds, or repository-wide formatting task**. If no declared
formatter matches, it does not prepare any formatter environment.

The transaction snapshots Git's active index, including `GIT_INDEX_FILE` and the
temporary index supplied by `git commit -a`, amend, or a path-limited commit.
Formatter configuration and source files come from staged blobs; orchestration
declarations are frozen separately. `commit -a` retains Git's normal behavior:
current tracked changes are included even when an earlier version was staged.

Fully staged files are formatted in the index and working tree. A partially staged
file passes unchanged when its staged content is already formatted. If its staged
content needs formatting, the **whole transaction stops before applying changes**.
Format and review that file, stage the intended changes, and retry. chainman does
not merge formatting into unstaged edits, stash files, or run repository-wide tasks.

Native host Git owns index access, repository discovery and configuration queries.
Git evaluates its own conditional includes, symlinks and attribute paths. Built-in
LF/CRLF normalization is supported; CRLF alone does not make a file partially staged.
Staged and working attributes must agree for files being changed. Custom clean/smudge
filters, ident expansion and working-tree encodings receive an unsupported-transformation
diagnostic; hooks do not execute these transformations implicitly.

You can commit while an independent managed development command is running. Only
one staged-format operation runs per worktree. Updates and cleanup remain excluded
while formatting is active. Changed indexes, configuration, selected working files,
or unexpected formatter output stop application. Git-owned locks are never removed.

Interrupted application retains recovery material under
`.chainman/staged-format/transaction-*`: `original-index`, numbered original and
formatted files, the staged snapshot, and `apply.json` naming paths and modes.
Compare these with the current index and worktree before restoring anything;
preserve subsequent edits. After resolving the interruption, remove that transaction
directory and retry. Older recovery directories also remain protected and require
manual review. Update `resume=` does not apply to staged formatting.

## Host Git and managed tools

The verified runtime provisions native lefthook and its small host helper through
Nix, using host Nix or Docker/Podman. No host Python, Go, Node or lefthook installation
is needed. The helper is operation-scoped; there is no daemon or global installation.
Formatters and scanners run in their declared managed environments.
Private hook directories resolve filesystem aliases in `TMPDIR` before use.
Finite hook commands clean up remaining foreground process-group children on
completion or cancellation, including children whose immediate parent exits first.
Lefthook cancellation uses its SIGINT cleanup path while preserving the caller's
signal exit status. The outer hook waits for managed callback cleanup even if
lefthook has already stopped the callback's client or closed its output.
Direct lefthook shell commands must wait for their own
background work; lefthook owns their job/PTY groups. Hooks must not daemonize or
detach background work into a separate session.

Run `git commit`, `git push`, `just format-staged`, and hook administration from the
host. Container-only development still supports hooks invoked by **host Git**.
Git invoked inside a container uses that container's environment and reachable
repository configuration. chainman does not import host Git configuration, identity,
credentials, attributes or executable hooks. Invoking chainman's repository hooks
from inside a container stops with guidance to run Git on the host or use host Nix.
Verified update candidates retain their separate, frozen Git authority.

Lefthook configuration overrides use native lefthook semantics: direct `run` shell
commands execute on the host. Use the runtime callback to execute a declared project
task with its managed profile, setup and environment:

```yaml
pre-push:
  commands:
    project-check:
      run: '"$CHAINMAN_HOOK_ENTRY" run hook-project-check'
```

Each callback receives the hook's original stdin. For pre-push tasks,
`CHAINMAN_HOOK_REMOTE_NAME` and `CHAINMAN_HOOK_REMOTE_URL` identify the destination.
If a callback needs setup, the foreground host helper asks once for its required
repairs using the controlling terminal. Lefthook's private terminal and captured
output do not own that question. Refusal, EOF, a missing foreground terminal, or
an interrupted consent helper stops admission without running the task. Unattended
callers should run setup first or explicitly select `CHAINMAN_SETUP=auto`.
Do not put project-language commands directly in lefthook `run` unless those tools
are intentionally provided by the host. Bare-host mode does not provide managed
hooks or transactional formatting.

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
fallback skips recognized native binaries that cannot decode as UTF-8; valid UTF-8
executable text is always scanned. Skips are reported without caching a
clean-source result. The classification identity invalidates older clean caches.
Traversal inventories the first outgoing tree, then its successive differences;
it retains one diagnostic location per distinct source blob, not every unchanged
occurrence in history. Every outgoing tree is still covered, including intermediate
changes and merge resolutions.

Scan a particular committed tree explicitly:

```sh
revision=$(git rev-parse HEAD)
just chainman trojan-source "$revision"
```

Defaults include common source formats (including CommonJS `.cjs`, Dart, Astro, Nix and Just),
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
