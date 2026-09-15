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
failure, partial application, staged formatting, and resume. Runtime selection tests
cover release age, commit age, changed tag identity, VERSION agreement, pin copies,
unchanged versions, failed source acquisition, and rollback.

Real lifecycle tests run Git-pinned runtimes through Nix. They must demonstrate
successful updates and cleanup, failed candidates, interruption/resume, and runtime
upgrades that leave the consumer bootstrap byte-identical. Native service tests add
readiness, startup failure, shared ownership, crash recovery, and volume lifecycle.

Git transactions assume cooperating processes and are not a filesystem transaction
against an adversarial same-user writer. Final application or commit interruption
can preserve partially applied verified changes. Recovery checks must account for
that state rather than resetting user work.

## Adoption and generated projects

Initializer tests check explicit version/SHA selection, moved tags, selected-revision
templates, empty-destination rules, host Git identity/branch/signing, commit-failure
recovery, and `--no-git`. Exercise the generated starter in both execution modes.

For established consumers, preserve existing commands and flakes, review host-entry
wrappers and name collisions, and run focused checks followed by the full declared
gate in disposable qualification checkouts. Exosuit scaffolds and Rynet exports must
remain synchronized and must not reintroduce copied runtime implementation.

## Release evidence

Record the exact Chainman SHA, consumer candidate tips, test commands/results,
platform omissions, and unrelated application failures. After publication, read the
public lightweight tag and release metadata, then prove fresh public Git installation
with host Nix and container-only prerequisites. Run the documented quickstarts and
validate links/configuration examples.

Do not promote consumers before public readback and their required gates pass.
Separate an unavailable platform lane or application blocker from a Chainman failure;
do not weaken acceptance or dependency policy to turn either into a passing result.
