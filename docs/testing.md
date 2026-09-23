# Qualification

[Documentation index](README.md) · [Contributing](contributing.md) · [Publishing](releasing.md)

## Source gates

`just verify` runs formatting, strict typing for Linux and Darwin targets, unit and
property tests, real Git transaction tests, host-Nix bootstrap tests, and the small
example build. Native adapters and service ownership have additional focused gates
listed in [contributing](contributing.md).

The manually dispatched verification workflow binds every lane to the requested
source SHA. It includes Linux x86-64/ARM64 and macOS ARM64/Intel host lanes,
Docker/Podman container lanes, and language/native-tool checks. A source commit is
qualified only by the lanes actually executed successfully. Availability of a
workflow definition is not evidence that it has passed.

## Bootstrap and source identity

The Git bootstrap tests exercise real Git objects with temporary repositories. Only
the remote transport is substituted when public code is not yet available. They
check the consumer recipe's size, malformed pins, cold and offline-warm startup,
corruption, replacement refs, concurrency, interrupted fetches, source checkout
modifications, literal arguments, stdin, process status, and signals.

The Git source tests separately exercise runtime materialization. Files must match
the selected tree's bytes and executable identity, independent of attributes,
untracked files, or worktree modifications. Unsupported tree modes and missing
revisions fail explicitly. Source imported into Nix is checked against the verified
Git export and rooted through execution.

Bootstrap tests use restricted PATH fixtures without host language interpreters.
Container-only qualification must also exclude host Nix. Keep these lanes distinct
from unit tests that substitute a Nix store import or starter generation: those
unit fixtures test the surrounding policy, not the substituted boundary.

## Updates and operations

Unit transaction tests use real Git/index/raw-file state in disposable projects.
They cover frozen authority, declared output scope, concurrent edits, verification
failure, partial application, and resume.
`just hooks-test` builds the native helper once and tests real host Git indexes,
installation ownership, partial-file refusal, concurrency, interrupted application,
and outgoing scanning. The opt-in container hook fixture removes host Nix, Python,
Node, Go and lefthook from PATH while using real container-managed tools. Runtime selection tests
cover default-branch discovery and renaming, unavailable/empty remotes, rewritten
history, same-version/different-SHA updates, unchanged pins, pin copies, failed
source acquisition, and frozen selection across branch movement and resume.
Project dependency age enforcement has separate regression coverage.

Real lifecycle tests run Git-pinned runtimes through Nix. They must demonstrate
successful updates and cleanup, failed candidates, interruption/resume, and runtime
upgrades that leave the consumer bootstrap byte-identical. Native service tests add
readiness, startup failure, shared ownership, crash recovery, and volume lifecycle.
Task-entry checks require a durable kernel identity before project code executes,
including commands that close inherited descriptors. Lease receipts retain their
original bytes and locked inode; identity publication uses a separate atomic file.
Tests cover failed publication, removed/replaced leases, surviving descendants,
and caller death without losing local or repository service ownership.
Graceful shutdown checks run on Linux and Darwin. Linux additionally pauses the
forwarding owner to detect duplicate group signals deterministically. That pause
is not used on Darwin: the hosted runner also loses a queued termination signal
in a standalone Go `signal.Notify` program across `SIGSTOP`/`SIGCONT`. Darwin still
checks one graceful application signal and process cleanup. This is a limitation
of the pause-based test, not a guarantee of graceful handling by an externally
suspended process; shutdown deadlines retain their forced-cleanup fallback.

Git transactions assume cooperating processes and are not a filesystem transaction
against an adversarial same-user writer. Final application or commit interruption
can preserve partially applied verified changes. Recovery checks must account for
that state rather than resetting user work.

## Adoption and generated projects

Initializer tests check default-branch and explicit SHA selection, selected-revision
templates, empty-destination rules, host Git identity/branch/signing, commit-failure
recovery, and `--no-git`. Exercise the generated starter in both execution modes.

For established consumers, preserve existing commands and flakes, review host-entry
wrappers and name collisions, and run focused checks followed by the full declared
gate in disposable qualification checkouts. Exosuit scaffolds and Rynet exports must
remain synchronized and must not reintroduce copied runtime implementation.

## Release evidence

Record the exact chainman SHA, consumer candidate tips, test commands/results,
platform omissions, and unrelated application failures. After publication, read the
public default-branch identity, then prove fresh public Git installation
with host Nix and container-only prerequisites. Run the documented quickstarts and
validate links/configuration examples.

Do not promote consumers before public readback and their required gates pass.
Separate an unavailable platform lane or application blocker from a chainman failure;
do not weaken acceptance or dependency policy to turn either into a passing result.

### Scoped transport checks

`test_execution_transport.py` uses neutral declarations and synthetic X11 records
to check semantic composition, optional inputs, inspection, credential scoping and cleanup.
`test_setup_terminal.py` checks prompt consent without consuming command stdin,
plus bounded HUP/INT/TERM shutdown before a prompt request and removal of private
transport files. Its native-controller cases run in `just control-test`.
`test_native_hooks.py` exercises actual lefthook terminal handling and callback
cancellation; it checks helper exit separately from captured-pipe completion.
`test_bootstrap.py` includes opt-in real-container profile and display-helper
checks, including interactive setup consent before credential mounts.
The display-helper fixture uses a local Unix socket and synthetic cookie;
it proves transport behavior, not browser rendering or a real desktop login.
Run these focused tests sequentially; no application build or production account
is needed.
