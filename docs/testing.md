# Python reliability and test evidence

## Development gates

Use `just verify` for the complete core gate. `just type-check` runs its mypy
portion for both Linux and Darwin, independent of the current host. This checks
platform-specific Python APIs before native CI. To run the generated contracts alone:

```sh
just exec python3 -B -m unittest discover -s tests -p test_properties.py -v
just exec python3 -B -m unittest discover -s tests -p test_solver_properties.py -v
just exec python3 -B -m unittest discover -s tests -p test_transaction_state.py -v
just exec python3 -B -m unittest discover -s tests -p test_dependency_identity.py -v
just exec python3 -B -m unittest discover -s tests -p test_adapter_data.py -v
just exec python3 -B -m unittest discover -s tests -p test_application_recovery.py -v
```

The configuration compiler, resource policy, transaction checkpoint codec,
dependency identity, native input projections, timing and distribution/development
entrypoints enforce the pinned mypy
strict flags, plus rejection of explicit `Any` and unreachable code. Decoded values
enter as `object` and are narrowed by validation. Resource execution uses an
immutable policy with typed fields. The transaction coordinator, native lock
adapter, source workflow, finite-command adapter and SDK coordination also enforce
strict flags. Native adapter configuration values are projected into typed paths,
profiles, pin arrays and policy maps. The npm adapter requires complete function
annotations and passes decoded records through its decisions; some imported
operations remain dynamic. The other modules listed in
`mypy.ini` check unannotated bodies
but still permit untyped calls and dynamic payloads. The gate checks 42 source
files, including 33 with the strict flags. Recipe bindings and verification task
lists are validated into typed collections before facade generation. Environment
expansion and service addresses consume typed string collections. Distribution inventories and consumed
release metadata are validated before use; produced release metadata has a typed
wire schema. Timing context managers have explicit lifetime and environment types.
Operation leases and compiler lifetime context managers also expose concrete
descriptor, stream and yielded-environment types. Managed subprocess options have
explicit allowed keys and value types, with separate text and binary results.
Manifest discovery enforces strict flags and produces typed dependency pins and
Pub workspace records. Shared lookup/editing accepts unknown parsed values and
narrows mutable mappings and sequences without replacing serializer objects. General
configuration maps still need further typing.
Bootstrap command projections, public configuration inspection and the consumer
qualification checker also enforce strict flags. Service declarations expose
validated named maps; service execution and export narrow their nested records
before constructing commands. The rest of service lifecycle code still needs
complete typing.
Module adapter assembly, nonstandard GitHub tag selection and declared artifact
snapshots also enforce strict flags. Selected tags and artifact identities have
named records; manifest pointers are copied into snapshot records so callers
cannot mutate their source configuration through a returned snapshot.
SDK source pin editing also has strict types for its declaration, selected record
and serializer callback. A write requires an actual source declaration and retains
the existing concurrent-edit checks and original file mode.
SDK synchronization uses named declaration, observation, snapshot and resolution
records. It validates the saved pin, tool and source inventories before source
updates begin, including matching array lengths and required source observations.
The incomplete-baseline regression uses two SDK sources and requires rejection
before the first write, preserving both source and output manifests.
Dependency reporting also enforces strict flags and emits typed coverage rows.
Coverage includes both SDK source declarations and synchronized output pins; its
regression uses a disposable Git repository and verifies reporting changes no
files and runs no SDK probe. Workspace discovery checks inferred and explicit
manifest lists, including exclusions.
JavaScript workspace collections and evidence-cache boundaries now have explicit
types; peer optionality is projected into boolean records when a candidate is
visited. Unused older peer metadata cannot invalidate a valid selection. The
shared registry implementation is also checked: constraint containers are
validated before use, and matching age exceptions become named immutable records
before version ranking. Its callable cache exposes typed cache operations.
pnpm importer edges, package records and snapshot edges are validated before
baseline identity collection, graph auditing and native lock normalization. Graph
traversal consumes these typed records; raw workspace documents, adapter
configuration and solver policy remain dynamic. Inclusion in the gate does not
imply complete strict typing.
Adapter implementations outside that explicit list are not counted as checked.
The unannotated third-party `semantic_version` package uses a local stub for the
complete-version and npm-range API Chainman consumes. The stub is checked in the
source archive and formatted with Python sources; it adds no consumer runtime
code. There is no missing-import suppression for that dependency.
This is incremental coverage, not a claim that all Python is strictly typed.

Hypothesis is pinned through the development Python shell. It is absent from
the consumer bootstrap and update Python environments. Its tests run in ordinary
unittest discovery, with no opt-in or dependency-missing skip. Each property
requests up to 200 generated examples, uses deterministic generation and disables
timing deadlines. Failures still shrink to small counterexamples. The properties
perform no network requests, Nix evaluation or native package-manager operations.

The generated oracles cover:

- Task inheritance against a small field-by-field reference: array replacement,
  empty tables, nested overrides, exact origins, unchanged input and independence
  of sibling results after mutation.
- Unused inheritance cycles across every declaration kind.
- Stable release selection against numeric version tuples and explicit age
  comparisons, including deprecated/prerelease entries and input reordering.
- Multiple observations of the same version straddling an age boundary. Both
  observation orderings must reject it until the latest observation matures.
- JavaScript peer solving against exhaustive integer-domain assignments, including
  cycles, per-release constraints, impossible graphs and a duplicate package pin
  across two scopes. There are at most 243 assignments, below the configured 256
  visited-state bound. A selected tuple must satisfy the independent oracle, and
  failure is allowed exactly when the oracle has no solution. Repeated solves
  must agree. This does not impose a global version-preference optimum.
- Resource budgets against an exact rational capacity calculation, including the
  CPU/configuration caps, minimum one job and monotonicity with more resources.
- Existing flat schema-1 checkpoints and schema-2 explicit runtime selections
  against independent wire fixtures, with
  distinct original/candidate Git identities and snapshots, inspected/uninspected
  states, exact JSON compatibility and independence from later input mutation.
- Dependency records against the existing five-string tuple/JSON contract,
  including named-field order, hash/equality compatibility and duplicate removal.
- Go native records against explicit module/replacement coordinates, preserving
  requirement order and duplicate entries and remaining independent of later
  input mutation. This checks the data boundary, not Go replacement precedence;
  disposable native Go fixtures check that separately.
- pnpm importer and snapshot records against independently generated section,
  alias, declared specifier and resolved context coordinates. These consumed edge
  coordinates are independently copied; unknown resolution keys remain visible
  to source policy.
- Application/recovery sequences against independent expected file bytes, full
  modes, index entries and HEAD. The bounded state machine runs 20 examples of up
  to 12 steps: interrupted application, refused resume over partial outputs,
  explicit operator restoration, resumed inspection and successful application.
  It uses real disposable Git repositories and no Nix or native resolver calls.

These have deliberately bounded vocabularies. They do not establish complete
SemVer/PEP 440 correctness, lock graph correctness or platform behavior.
The peer property tests the solver with declared candidate domains and fixture
metadata; registry decoding and native resolution retain their separate gates.
It detected both deliberately substituted regressions: selecting every newest
candidate while ignoring conflicts, and rejecting every graph indiscriminately.

## What the existing tests establish

Source-module setup treats unparseable JSON content and non-object readiness
stamps as cache misses. A child-process fixture verifies that each invalid stamp
causes one setup run, followed by reuse of the repaired stamp. Missing outputs
and changed declared inputs invalidate readiness; failed setup preserves the
previous stamp and cannot make absent outputs ready.

The reviewed transaction tests operate on disposable Git repositories and assert
actual file bytes, index contents, commit identities and preservation of
concurrent edits. Workflow tests execute fixture programs and inspect their
outputs. These are useful behavioral checks.

Mock assertions can also enforce useful contracts: rejecting an invalid Cargo
attempt budget before registry/native work, bounding retries, and preventing a
failed normalization from reaching publication. Those tests also check original
manifest/lock contents. Call counts alone would not establish resolver semantics.

The fake npm/pnpm processes in the JavaScript unit suite are fault-injection
fixtures. They cannot prove how the real package managers interpret a lockfile.
`just javascript-test` supplies that separate evidence with the pinned binaries
and a disposable local registry. The same distinction applies to other adapters:
mocked native success is not evidence of native acceptance or compatibility.

A repeat native JavaScript run exposed an intermittent upstream-override failure.
The case and complete native suite then passed without production changes.
Investigation confirmed that pnpm 11 ignored the fixture's `.npmrc` cache-directory
setting, leaving metadata shared across neutral package fixtures. The fixture now
sets `PNPM_CONFIG_CACHE_DIR` and checks the effective pnpm/npm cache paths through
the real binaries. Cache interference is a plausible explanation for the original
failure, not an established causal reproduction.
The pnpm setup migration fixture also now closes stdin explicitly: capturing
stdout/stderr did not remove an inherited terminal, so its intended noninteractive
refusal case could hang waiting for confirmation when launched from a shell.

`just python-test` qualifies pinned uv against a disposable loopback Simple API
index with upload timestamps and real wheels. It checks direct and transitive
selection, global/per-package cutoffs, exact no-op retention, retirement of
per-package cutoffs, and rejection of a stale lock without rewriting it. Restored
manifest/lock pairs must pass real offline `uv lock --check`. This is a native
configuration/lock boundary test, not a full Python adapter or PyPI provenance
audit; production artifact checks still use their separate registry evidence.

The native fixture found a conservative boundary difference: uv 0.12.5 excludes
artifacts exactly at its cutoff using millisecond precision; Chainman's shared
eligibility check includes the cutoff. The test covers an upload immediately
before, exactly at and immediately after it, then advances the cutoff one
millisecond. Chainman retains uv's conservative behavior. See the
[pinned uv comparison](https://github.com/astral-sh/uv/blob/0.12.5/crates/uv-resolver/src/version_map.rs#L524-L528).

The public bootstrap suite includes real host-Nix update/recovery lifecycles.
Combined and runtime-only updates use two independently packaged runtime
generations. They assert that project resolution and verification run under the
new runtime while the original checkout retains its old pin until application.
Failed-verification resume keeps the prepared runtime, repeats evidence checks
and verification, and does not rerun project resolution. Only the release
transport is replaced with fixture responses. Project-only lifecycle cases retain
their original runtime and exercise interruption, staged formatting and Git
application independently. Legacy custom resolver arguments are also covered by
real hook/Git tests; adapter argument validation remains a separate strict gate.
Separate application tests inject failed writes, deletions and Git index/commit
operations. They also kill a disposable Python child after its first completed
write, immediately before branch publication and immediately after it. Assertions
check preserved original/candidate bytes and modes, actual staged blobs, branch
history and release of process leases. These are process-interruption checks,
not a simulation of power loss or filesystem durability.

`just gradle-test` enables the existing offline native Gradle fixture suite and
runs in the compose job of the deliberate CI workflow. It checks child-project
transitive locks, native composite-build source bindings, read-only inspection,
resolution failure and termination of the build JVM. Building the Compose example
and qualifying Chainman's Gradle adapter are separate checks.

Container-engine and native controller tests have separate prerequisites/gates;
skipped tests provide no evidence about those paths. Linux success does not
establish macOS success.
`just control-test` runs Go vet and the Go tests with the race detector, then the
native controller/backend fixtures and four target cross-builds. Cross-builds
establish compilation only. The deliberate CI matrix runs core and controller
qualification natively on both Linux and macOS, on ARM64 and x86-64 runners.
The formatting gate includes Actionlint's workflow and embedded-command checks;
PyYAML stubs are pinned in the development shell alongside its implementation.
Distribution tests compare the explicit source inventory with production Python,
Go and test files, and check that statically imported local Python modules are
present in the runtime archive. Dynamic imports and external commands still need
their behavioral and packaged-runtime checks.
Core tests evaluate an actual Nix flake under a directory containing spaces and
URI characters, then provision and execute the native task controller from a
copied runtime under such a path. The latter asserts child output and exit status.
SDK recipe tests exercise the actual Just entrypoint with a fixture launcher;
the macOS job separately runs the real Apple SDK preflight.
Temporary fixture roots are canonicalized immediately after allocation: macOS
can return `/tmp` through its `/private/tmp` alias, whereas Git and subprocesses
report physical paths. Both Python and Go fixture roots use this convention.
Explicit alias and symlink cases construct their own links after that common
fixture setup. The Go race suite also runs successfully with an aliased `TMPDIR`.
Source previews also canonicalize their internally allocated temporary root.
A regression uses an aliased temporary directory and a real copied Git submodule:
valid relative links remain readable, candidate edits verify in the copy, and the
original bytes, index and HEAD remain unchanged.
The bundled macOS Swift profile uses the Xcode selected by `xcode-select` for both
its compiler and SDK. It restores `DEVELOPER_DIR` before refreshing `SDKROOT`,
`CC` and `CXX` through `xcrun`; Nix's Apple SDK hook otherwise changes both the
developer directory and SDK, including when entering from another Nix C shell.
The bundled profiles use the main Nixpkgs lock on Linux and Apple Silicon.
Intel macOS uses the separately locked `nixpkgs-darwin` input from
`nixpkgs-26.05-darwin`, since unstable has removed that platform. The source
dependency policy updates both inputs with the ordinary commit-age rule. Generated
examples inherit both locks and update targets. Intel compatibility depends on
the remaining upstream 26.05 support period; its retirement requires a new
platform-support decision.
Local comparisons of its Python tools record both interpreter versions and module
import paths. Ad hoc `nix shell` checks clear inherited `PYTHONPATH` and
`NIX_PYTHONPATH` before starting the selected Python environment; otherwise an
older interpreter can still import the main shell's newer libraries.
On Intel macOS, source and bootstrap entry make the compatibility input's Bash
available through `nix shell` before `nix develop`. Nix selects its startup Bash
from the input named `nixpkgs` independently of the devShell, and otherwise falls
back to the host's Bash. A real-Nix fixture supplies a primary input that cannot
provide Bash and a failing host Bash; both launchers must still deliver literal
arguments and the child's exit status through the pinned shell. Native Intel CI
also qualifies the actual compatibility packages.
Darwin controller fixtures invoke `/bin/ps` directly for process-state evidence;
system administration tools need not be on a selected Nix profile's `PATH`.

`just rust-test` runs the production Cargo adapter with a loopback sparse index,
real crate archives, and isolated source replacement/cache configuration. It
checks exact direct selection, a native transitive conflict followed by eligible
fallback, restored manifest syntax/modes, native acceptance with `--locked`,
unsatisfiable resolution, and rejection/restoration of mismatched checksum
evidence. The fixture supplies registry publication evidence; it does not qualify
the crates.io HTTP client. A virtual workspace case checks one renamed dependency
inherited by two members through ordinary and development dependencies, native
alias binding, transitive repair and preservation of member manifest bytes/modes.

`just swift-test` runs the production Swift adapter against disposable versioned
Git repositories. A fixture-only Git transport rewrite preserves the declared
GitHub URLs, and non-file transport is disabled. Native manifest evaluation,
update, and frozen dependency graph inspection all use the pinned SwiftPM binary.
The fixture isolates repository, configuration, fingerprint, and compiler caches.
It checks direct selection, transitive versions/revisions, public manifest
restoration, failed resolution, mismatched revision evidence, and read-only
repeated audit. The fixture supplies GitHub publication evidence.
A local-package case reaches remote parent/leaf dependencies through a contained
bridge package. It checks the real frozen graph and command-root lock, preserves
the local manifest, and rejects an incomplete lock despite an already populated
native cache. Local closure discovery does not expand automatic pin ownership.

The Swift test initially reproduced an audit failure after a valid update:
`--skip-update` reused a shared repository cache lacking the newly selected
commits. The audit now allows repository refresh while forcing the resolved
versions and guarding the lock against changes. The native fixture deliberately
populates its cache before publishing later local tags. Both new gates run in
their corresponding deliberate CI jobs, including Swift on macOS.
An isolated mutation restoring `--skip-update` makes the native success case
fail again with missing Git objects; the test does not rely on checking flags alone.

## Targeted test-sensitivity experiment

On 2026-09-12, the baseline at `9c54804` passed 98 tests in `test_configuration`,
`test_resources`, `test_registry` and `test_update_staging`. Each deliberate fault
below was then injected into an imported production module in a separate Python
process, leaving repository files unchanged. The corresponding test module was
run against it. The experiment was repeated with the new generated contracts.

| Deliberate fault | Baseline tests | With generated contracts |
| --- | --- | --- |
| Shallow-copy inherited lists, allowing sibling aliasing | Missed | Detected |
| Concatenate inherited arrays instead of replacing them | Detected | Detected |
| Round memory capacity up instead of down | Detected | Detected |
| Exclude publication exactly at the maturity cutoff | Detected | Detected |
| Use the oldest observation of a version instead of the latest | Missed | Detected |
| Omit the original Git-index comparison before applying an update | Detected | Detected |

This is six selected mutations, not a repository-wide mutation score. It verifies
that specific assertions reject plausible regressions; it does not verify every
test. In particular, an initial broad random inventory property still missed the
oldest-observation mutation. Explicit generation of correlated observations was
necessary. Generate important interactions deliberately instead of relying on
independent random inputs to happen to contain them.

The follow-up boundary work also injected four independent faults: preserving
inspection on resume, confusing original and candidate indexes, bypassing
malformed-baseline validation for an empty result, and swapping identity URL/hash
fields. Each new targeted test failed on its corresponding fault. These mutations
ran in separate Python processes without changing repository files. The codec
checks have explicit field assertions in addition to round trips, so a paired
encoder/decoder mistake cannot silently validate itself.

The adapter follow-up injected four more faults in temporary Python processes:
accepting malformed falsey Go lists as empty, swapping old/new Go replacement
coordinates, serializing the npm projection and losing unknown native metadata,
and deferring npm entry validation until after the frozen native check. All four
were detected by the new tests. The npm cases exercise the production resolver
and assert published lock contents or preserved original inputs, in addition to
native call ordering. Their native processes remain fault-injection fixtures;
the pinned npm/pnpm gate supplies separate real-binary evidence.

The application follow-up reproduced an overwrite before adding destination
rechecks: an edit made to a later output during application was replaced, and
finalization reported success. The regression tests now cover changed bytes,
deletion targets and modes. Three isolated mutation probes were detected:
removing the destination recheck, retaining stale inspection on resume, and
erasing a previously completed output after an application failure. The latter
two were detected by the recovery state machine's assertions. These probes leave
repository files unchanged and do not constitute a repository-wide mutation score.

The JavaScript evidence follow-up detected two further isolated mutations:
bypassing peer-metadata validation and making every declared peer optional.
Assertions check successful fallback to a valid release, rejection when a required
peer has no compatible release, and unchanged project manifests. Malformed
metadata is tested separately from valid optional peers and unused older releases.
The shared policy tests also check malformed tables and exception fields through
actual release selection, alongside the existing age/floor/expiry oracles.

## Boundary guarantees and next improvements

Typed checkpoints now reject malformed records before operational decisions;
resume invalidates prior inspection. The in-memory inspection record is not a
verifier attestation: the launcher remains responsible for running verification
before finalization. The persisted flat schema and dependency identity wire form
remain compatible. Records have frozen fields, with copied mutable collections
inside the transaction state; they are not deeply immutable.

Native input projections now validate npm and pnpm package records, pnpm importer
and snapshot edges, Go manifests/queries and Swift graph nodes before their fields
drive adapter decisions. Go local and
remote replacements use distinct records; Swift declarations use a tagged union.
Go's native `null` list form remains valid; other falsey malformed values no
longer silently mean no dependencies. npm retains the raw lock document for
serialization, so validating a projection does not drop unknown native metadata.
pnpm also retains the full document for normalization graph comparison. Tests
inject malformed output after resolution, normalization and frozen verification:
each must stop before publication and preserve original project inputs. Separate
cases check both retention of unknown metadata and rejection of changes to it
during normalization.
In disposable module copies, five deliberate regressions failed the intended
assertions: omitting optional dependency sections, crossing importer
specifier/version fields, hiding unknown resolution keys, comparing only projected
normalization data, and delaying validation until after normalization. The
unmodified controls passed. Mypy separately rejected four incompatible importer,
snapshot, package and resolved target values.
Go records have immutable fields and tuple collections; Swift graph children are
decoded one level at a time during iterative traversal, not deeply frozen.

Remaining typing work is concentrated in adapter configuration, solver policy,
registry HTTP payloads and adapter implementations outside the gate. Splitting
large modules for size alone is a lower priority than replacing dynamic decision
inputs.

Add differential tests against native resolvers when changing their adapters.
Extend the native fixtures when changing supported resolver behavior. Application
failures deliberately preserve partial results for inspection; the recovery model
does not introduce automatic rollback or authorize resuming over changed inputs.
