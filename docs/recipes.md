# Standard project recipes

[Guide index](README.md) · [Getting started](getting-started.md) · [Troubleshooting](troubleshooting.md)

Run standard behavior with `just chainman recipe NAME …`. Bind application tasks
in `chainman.toml`. All dispatch code lives in the selected Git revision; there is
no generated recipe facade.

Projects can keep familiar commands with their own short forwarding recipes:

```just
[positional-arguments]
verify *args:
    #!/bin/sh
    exec just chainman recipe verify "$@"
```

Ordinary runtime updates change the pin
and declared pin copies; they leave these project-owned recipes unchanged.

```toml
[recipes]
setup = ["prepare"]
generate = ["generate-artifacts"]
format-write = ["format-sources"]
format-check = ["check-format"]
format-hygiene = ["check-hygiene"]
verify = ["verify-postgres", "verify-spanner"]
verify-lite = ["check-types", "test-units"]
clean = ["clean-artifacts"]

[updates]
verify_tasks = ["verify-postgres", "verify-spanner"]
```

Bindings name existing tasks. Lists execute sequentially, releasing each task's
service ownership before the next begins. A task's `depends_on` retains its shared
service-graph semantics. `verify` must select the same gate as update acceptance.
Missing required application bindings fail explicitly. Setup, diagnostics, cache
maintenance, service status, stop, inspection and dependency operations have shared
implementations; project doctor and clean tasks supplement those defaults.

| Recipe | Contract |
|---|---|
| `setup` | Prepare every setup group, run setup extensions and install declared Git hooks; `--no-hooks` opts out |
| `hooks` | Manage or inspect the pinned lefthook preset |
| `generate` | Generate declared outputs in place, without committing |
| `format` | Generate, format/autofix, check formatting and hygiene in an isolated candidate, then commit the exact verified result |
| `format-write` | Run the declared formatter/autofix tasks in place |
| `format-check` | Check formatting without changing source |
| `format-staged` | Format fully staged changes; refuse partial files that need formatting |
| `verify` | Run the complete declared project gate |
| `verify-lite` | Run the declared smaller gate |
| `deps-update` | Resolve, audit, reconcile, verify and commit dependency updates |
| `deps-update-TARGET` | Select a declared adapter or target group |
| `chainman-update` | Update the runtime and its managed companions, then run the project gate |
| `deps-check` | Validate adapter selection and ordering without resolving |
| `deps-coverage` | Report managed, explicitly excluded and unmanaged dependency inputs |
| `deps-policy-report` | Report effective selection, policies, constraints, exceptions and coverage |
| `deps-audit` | Run pinned vulnerability scanners and report unsupported ecosystems explicitly |
| `config validate` / `config show --json` | Validate or inspect effective configuration |
| `explain TASK --json` / `setup-status` | Inspect task requirements or setup readiness |
| `preflight TASK ...` | Check the complete selected workflows' mode/platform requirements before any project work |
| `doctor` | Shared runtime diagnostics followed by declared project diagnostics |
| `stop` / `services-status` | Stop or inspect saved service ownership |
| `logs [--follow]` | Read recent saved service output; optionally follow without acquiring services |
| `clean` | Stop services, clean project artifacts and prune managed build contexts |
| `cache-status` / `cache-prune` | Inspect or prune managed caches |

Update options use `commit=auto|off`, `mode=apply|dry-run`, `targets=js,rust`,
`policy=aggressive|compatible`, `js_policy=compatible`, and `message=TEXT`.
Values remain arguments, never shell code. The defaults are apply and auto-commit.
Untargeted updates and `targets=all` update chainman together with project
dependencies. Explicit application targets retain the runtime; `--skip-chainman`
also opts out of runtime updates. Both generations remain separate immutable
runtimes, and the combined candidate must pass verification before application.
`chainman-update` does not accept dependency selection options. `format commit=off`
runs the complete generate/format/check/hygiene sequence in place; `format-write`
is the narrower formatter-only operation. Native API flags remain available to
programmatic callers; there are no compatibility recipe aliases.

Automatic commits require a clean starting checkout. They bypass Git hooks because
the candidate has already passed its declared gate, retain configured commit signing
and identity, and never push. All intended generated and formatted additions,
modifications and deletions are eligible; runtime files and submodules have separate
ownership. The original HEAD, index and source bytes must remain unchanged throughout.
Verification cannot mutate the frozen candidate. Staged formatting has a separate
[formatter-only transaction](hooks.md), supports partial staging and `commit -a`,
and never commits. It requires explicit formatter declarations and never falls
back to the repository-wide format tasks.

A failed transaction retains its candidate and prints its transaction directory.
Reconcile there, including project-required semantic review, then use
`just chainman deps-update resume=/absolute/transaction/directory`. Resume checks the original
checkout and candidate Git identity again, reruns declared reconciliation, obtains
fresh dependency eligibility evidence, freezes the candidate again and reruns the
complete original gate. Previous verification is never reused. `updates.reconcile_tasks`
names mechanical tasks that must precede freezing; `reconcile_outputs` extends the
runtime-update output scope for generated metadata. Hash refresh is not human review.

Dependency coverage discovers tracked manifests and declared adapter inputs.
`dependencies.exclusions` requires a `pattern` and technical `reason`; it cannot
turn an unmanaged input into managed coverage. `dependencies.pins` can register
additional pin files with their actual `owner` and `reason`. Policy reports also include
package-manager declarations, pnpm catalogs/overrides, Cargo advisory and duplicate
exceptions, and Gradle distribution and wrapper hashes. Native scanners retain their
project policy files. Optional reference
modules use their actual shared adapters when enabled.

Vulnerability auditing is distinct from release-age and immutable-source auditing.
JavaScript includes development dependencies. Cargo, Go and Python use cargo-deny,
govulncheck and pip-audit supplied by the pinned runtime Nix inputs. Tools are fetched
or built locally on demand, not installed globally. `audits.exceptions.ADAPTER` for JavaScript
accepts exact `id`, `package`, `reason`, and `review_after` entries; stale, mismatched
or expired exceptions fail. Flutter, Swift and Gradle currently report unsupported
vulnerability scanning and make the aggregate audit incomplete and nonzero; a
project can document the limitation in `audits.unsupported`. Unsupported does not
mean vulnerability-free. Application gates choose whether to require this separate
network-dependent audit (or select `targets=js` for a narrower lane); release-age and source checks remain mandatory for updates.

Use `just chainman config validate` to check the bindings. Keep generated project
copies synchronized through the project's generator and acceptance gate.
