# Python reliability and test evidence

## Development gates

Use `just verify` for the complete core gate. `just type-check` runs its mypy
portion. To run the generated contracts alone:

```sh
just exec python3 -B -m unittest discover -s tests -p test_properties.py -v
```

The configuration compiler and resource policy module enforce the pinned mypy
strict flags, plus rejection of explicit `Any` and unreachable code. Decoded
values enter as `object` and are narrowed by validation. Resource execution uses
an immutable policy with typed fields. The other modules listed in `mypy.ini`
check unannotated bodies but still permit untyped calls and dynamic payloads.
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
- Resource budgets against an exact rational capacity calculation, including the
  CPU/configuration caps, minimum one job and monotonicity with more resources.

These have deliberately bounded vocabularies. They do not establish complete
SemVer/PEP 440 correctness, lock graph correctness or platform behavior.

## What the existing tests establish

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

The public bootstrap suite includes real host-Nix update/recovery lifecycles.
Container-engine and native controller tests have separate prerequisites/gates;
skipped tests provide no evidence about those paths. Linux success does not
establish macOS success.

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

## Next improvements

Prioritize typed transaction state and dependency evidence/lock identities over
splitting large modules for size alone. Put decoding and validation at their
boundaries, then pass typed values through decision logic. A `dict[str, Any]`
annotation would preserve most of the current blind spots.

Add differential tests against native resolvers when changing their adapters.
Expand transaction fault injection around interrupted application, rollback and
resume using real disposable Git state. A generated state machine is useful only
if its oracle independently describes committed bytes, index state and allowed
transitions; a second copy of the production algorithm adds little confidence.
