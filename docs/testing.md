# Qualification

[Documentation index](README.md) · [Contributing](contributing.md) · [Publishing](releasing.md)

## Source gates

`just verify` runs formatting, strict typing for Linux and Darwin targets, unit and
property tests, real Git transaction tests, host-Nix bootstrap tests, and the small
example build. Native adapters and service ownership have additional focused gates
listed in [contributing](contributing.md).

The core source suite stops at its first error or failure, so the traceback and
captured startup output are reported before a later job deadline can cancel the
runner. A successful source gate still executes the complete discovered suite.

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
The shell lifetime tests also occupy all descriptors 3–9 and require literal stdin,
the child's exit status, and every caller-owned descriptor to survive execution.
Public Git-entry controls fill descriptors 3–9 and exercise both supervisors with
Bash, including literal streams, status, startup-file suppression, signal handling,
and private-export cleanup. Without Bash, a full descriptor table must fail before
dispatch without changing the caller's leases. Real-Nix bootstrap controls fill
descriptors 3–9 through both public entry and direct runtime-storage reentry,
requiring successful dispatch and intact caller descriptor identities.
Ordinary entry with a free descriptor must retain its portable path even when an
unusable host Bash precedes the pinned shell. The primary-input compatibility
fixture independently requires the bootstrap to reach its pinned Bash.
The host preview fixture first constructs and executes its native controller through
the public bootstrap, with a bounded ten-minute cold-build allowance. It then keeps
the separate sixty-second HTTP readiness deadline, interrupt handling, and port
release assertions. Container preview qualification retains its existing engine
interruption path without compiling the native controller.
Consumer profile tests evaluate a real shallow Git checkout with a nested flake,
tracked edits and untracked caches, requiring the edits to survive and caches to
stay outside the Nix source.

The Git source tests separately exercise runtime materialization. Files must match
the selected tree's bytes and executable identity, independent of attributes,
untracked files, or worktree modifications. Unsupported tree modes and missing
revisions fail explicitly. Source imported into Nix is checked against the verified
Git export and rooted through execution.

Bootstrap tests use restricted PATH fixtures without host language interpreters.
Container-only qualification must also exclude host Nix. A usable container engine
includes its installed helper programs: rootless Podman needs UID mapping,
networking and OCI helpers. Restricted fixtures retain those specific executables,
without restoring the host PATH or adding Python, Node, Go, lefthook, gh, curl or wget.
See Podman's [rootless setup](https://github.com/podman-container-tools/podman/blob/main/docs/tutorials/rootless_tutorial.md).
Keep these lanes distinct
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

Candidate export tests cover acceptance before verification, failed and mutating
verifiers, interrupted publication, corruption, unsafe destinations, exact file
bytes/modes/deletions, and reconstruction in a fresh repair checkout. Regression
cases reject results from a previous or competing invocation and require final
failure results when verification changes Git metadata or the original changes.
Public host-Nix fixtures exercise both source and consumer entrypoints; the source
case uses an unpublished local commit imported into the Nix store. The opt-in
container suite repeats consumer success, verification failure, Git mutation and
no-change exports using real Git transport and a deterministic provider selection
fixture. These checks qualify the export boundary, not the bot's repair policy,
sandbox or publication.
The runtime-only fixture selects a real Git revision, verifies with that runtime,
and checks the bundle's entry, resolution and verification identities while the
original consumer pin stays unchanged.

Public candidate-export fixtures allow ten minutes per export for cold native-task
and pinned-toolchain construction on both hosts and containers. They still require
the original result stdout, verification status, unchanged consumer Git state, and
detached-bundle readback. Exceeding the allowance remains an error.

Real lifecycle tests run Git-pinned runtimes through Nix. They must demonstrate
successful updates and cleanup, failed candidates, interruption/resume, and runtime
upgrades that leave the consumer bootstrap byte-identical. Native service tests add
readiness, startup failure, shared ownership, crash recovery, and volume lifecycle.
Task-entry checks require a durable kernel identity before project code executes,
including commands that close inherited descriptors. Lease receipts retain their
original bytes and locked inode; identity publication uses a separate atomic file.
Tests cover failed publication, removed/replaced leases, surviving descendants,
and caller death without losing local or repository service ownership. Exec
checks preserve executable lookup, inherited directories, and declared environment
overrides, including replacement of inherited service-lease metadata.
Graceful shutdown checks run on Linux and Darwin. Linux additionally pauses the
forwarding owner to detect duplicate group signals deterministically. That pause
is not used on Darwin: the hosted runner also loses a queued termination signal
in a standalone Go `signal.Notify` program across `SIGSTOP`/`SIGCONT`. Darwin still
checks one graceful application signal and process cleanup. This is a limitation
of the pause-based test, not a guarantee of graceful handling by an externally
suspended process; shutdown deadlines retain their forced-cleanup fallback.

Pending-admission signal tests run in child processes because resetting signal
notifications can legitimately terminate the admission owner. Both handled and
default signal exits must deny the workload permission; captured output also
keeps the test waiting until the gated child exits before checking for side effects.

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

The storage and bootstrap job-control fixtures open a disposable PTY and acquire
its controlling terminal in a fresh, exec'd Python interpreter. They must not run
`pty.fork()` or Python pre-exec callbacks in the test runner: on macOS, forking
after threads or higher-level system APIs have initialized can deadlock before
the fixture's timeout starts. The threaded-runner regression retains the real
shell suspend/resume, foreground-ownership, cancellation, and terminal-mode checks.
While waiting for PTY children to exit, including after interruption or forced
cleanup, use the shared draining wait in `tests/terminal_fixture.py`. Darwin can
wait for pending terminal output during close; a plain process wait can therefore
stall even after the test's job-control assertions pass. The interruption fixture
emits more than a terminal buffer of output to exercise this boundary on Linux too.
Sending shell commands must also drain terminal output: PTY echo can block a
large input write before the fixture reaches its assertion timeout. Use the
shared nonblocking `write_terminal` helper, retain its drained output for marker
checks, and keep its deadline. The bidirectional backpressure regression sends
and echoes 256 KiB; a separate stalled-reader case checks the write deadline and
restoration of the descriptor's original blocking mode.
Do not paste nested program bodies through an interactive PTY: its input queue
can corrupt a long command before the shell consumes it, even with bounded
writes. Store the quoted argv and result marker in a disposable shell script,
then send only its short launch command. The large quoted-argument regression
checks byte-for-byte argv delivery while retaining real stop/resume assertions.
Reentry assertions compare canonical project paths and exercise symlink aliases;
temporary-directory spellings such as `/tmp` and `/private/tmp` need not match.
Native state-directory fixtures use `physicalTempDir(t)` for the same reason.
Exercise update-cache tests with an aliased `TMPDIR` as a portable regression;
explicit negative cases must still reject symlinked cache bases before creation.

Native service PTYs use the same fresh-process launcher and draining waits.
Finite native task anchors observe both sequence stops and exits while attached
to a terminal. The partial-stop regression stops only the disposable sequence and
payload, then requires the anchor to propagate the stop, restore foreground
ownership and preserve exit status 7 after resume. Ordinary services, probes and
non-terminal tasks retain their existing wait path.
The background job-control fixture retries `tcsetattr` only on `EINTR`: Darwin
returns that error after stopping a background ioctl with `SIGTTOU` and resuming
it. Mode restoration compares every configured field and control character,
excluding only Darwin's kernel-managed `PENDIN` input bookkeeping bit. XNU adds
that bit when restoring canonical mode; it is cleared by subsequent input/read.
The raw round-trip fixture records the observed flag delta, and synthetic
negative checks retain failures for actual mode or control-character changes.
See [XNU terminal handling](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/tty.c).

## Failure diagnostics

If the stopped-task cancellation assertion times out, it records only the
disposable outer process, its descendants and its published anchor group.
The snapshot contains process IDs, parents, groups, states and executable names,
plus whether the workload received termination and retained its owner receipt.
It excludes arguments and environment, sends no signals, limits the process
query to two seconds and retains at most64 rows. Query errors preserve the
original three-second assertion failure. Successful tests are unchanged.

The `diagnose_stopped_cancellation` dispatch input of the registered
`verify.yml` workflow selects an exact-source Intel macOS diagnostic instead of
the normal matrix. Its default is false; ordinary verification keeps every
existing matrix lane and command. The diagnostic runs the unchanged full native
ownership gate. A new standalone dispatch workflow must first exist on GitHub's
default branch, so this branch uses the existing registered entrypoint.
If that gate passes, it repeats the original cancellation assertion twelve
times in the same pinned control profile. It stops on the first failure and
does not establish release qualification. Phase markers distinguish profile
entry, the control-package build and individual repetitions. Each repetition
prints its Python thread stacks every30 seconds if it stalls; these observations
do not change the native assertion's three-second deadline. The full ownership
output is uploaded before repetition starts, and repetition output is uploaded
even after a step failure. An eight-minute diagnostic step limit reserves time
for that upload inside the existing thirty-minute job limit. Reaching either
diagnostic limit is a failure, not qualification. Logs exclude environment dumps
and retain source identity. Runtime sources and the
publication/readback workflow remain unchanged. Restore the original
fourteen-lane workflow before final full release qualification.

The deliberate diagnostic also enables `CHAINMAN_TEST_STOPPED_TASK_STACKS=1`.
After the original stopped-task assertion fails, it checks that the running
outer controller is the test process's child, and that its running anchor still
owns the published disposable group. It sends fatal Go `SIGQUIT` dumps only to
those two controllers, reads at most64KiB for two seconds, and finishes that
verified group even if the outer has exited. This opt-in failure path changes
cleanup and never proves successful cancellation. Missing or changed ownership
refuses all signals. Captured stderr from the failed assertion is retained so
the anchor's cancellation marker can be distinguished from its stack state.
The original exception, three-second assertion and successful path remain.

Terminal job-control assertions record the disposable PTY's foreground group
and its session-owned process IDs, parents, groups, states and executable names
before cleanup. This read-only snapshot runs only after an assertion fails;
arguments and environment variables are excluded, and diagnostic errors never
replace the assertion. Test deadlines and successful paths are unchanged.

The diagnostic-only job opts into `CHAINMAN_TEST_TERMINAL_STACKS=1`. After an
assertion fails, a unique running Go outer controller with a stopped anchor in
the disposable PTY session can receive `SIGQUIT` for a stack dump. Live session
and group ownership are checked before signals. Capture is limited to two
seconds and 64KiB, then only that controller and its owned anchor group are
finished. The original assertion remains the failure. Ordinary qualification
does not enable this fatal, failure-only forensic path.

Finite task owners treat child-change notifications as wakeup hints and use a
100 millisecond periodic process-status check while waiting. A stopped or
exited child cannot remain hidden solely because a notification is unavailable.
The status waiter remains the only consumer of child status; cancellation,
terminal restoration and actual exit codes retain their existing paths. The
owning control exercises a stopped child and exit status 7 with a deliberately
silent notification channel. Native qualification remains required to establish
the observed Apple failure is repaired.

Both task status waiters request stopped children with `WUNTRACED` and never
request `WCONTINUED`. They decode the stop-report byte directly because Go's
Darwin `syscall.WaitStatus.Stopped` excludes `SIGSTOP`, although Apple's kernel
reports that signal as a stop. This matters when an anchor propagates a sequence
stop with `SIGSTOP`. Controls cover Linux and Darwin stop encodings, exits,
signal termination, and a real child's native `SIGSTOP` report. A future waiter
that requests continue reports must distinguish those reports separately.
The native Darwin control reports `0x117f` for `SIGSTOP` (signal17), while Go's
`syscall.WaitStatus.Stopped` returns false for that actual stop report.

Terminal task anchors capture `SIGTSTP`, `SIGTTIN` and `SIGTTOU` so terminal
stops flow through their sequence waiter. This includes background terminal I/O:
letting the anchor stop first can leave a sequence stop pending after resume,
which then causes another stop. Services, probes and non-terminal anchors retain
their existing signal handling.

After stopping itself, an anchor returns to its blocking sequence-status wait
instead of awaiting a separate continue notification. The kernel can resume a
process even when a later stop discards the pending `SIGCONT` notification;
requiring that notification can strand a running anchor with a stopped child.
The anchor does not resume the sequence itself. Actual child stops and exits
remain the authority. See [POSIX signal generation](https://pubs.opengroup.org/onlinepubs/9699919799/functions/V2_chap02.html).
