# Verified dependency updates

`just deps-update` selects current eligible stable releases, including majors, with
a configurable 30-day maturity window. `--preview` performs resolution and verification
in a disposable copy. `--no-commit` leaves the verified changes for a coordinated
checkpoint. `--only-chainman` updates the runtime pin and managed bootstrap;
`--skip-chainman` updates project dependencies without querying runtime releases.
Until the first public Chainman release is published, use `--skip-chainman` explicitly.
A missing release source or eligibility date is an error, never an implicit exemption.

The standalone example uses built-in registry adapters, `dependencies.toml`, module
manifests/lockfiles and `sdk-versions.toml`. Explicit constraints require reasons;
security maturity exceptions need a narrowly scoped advisory, minimum safe version
and expiry. Nix branch pins use commit age; container image pins use the registry
update time bound to the manifest digest. Baseline artifacts do not retrospectively
become release-age-qualified. Newly selected artifacts require eligibility evidence.
Toolchain changes force fresh environment entry before resolution and verification.
The runtime image is updated together with Chainman; consumer Nix inputs and workflow
revision pins remain project-owned update targets.

Existing projects can keep their resolver and verification hooks:

```toml
[updates]
minimum_age_days = 30
eligibility = "resolver"
profile = "default"
resolver = [["sh", "scripts/resolve-dependencies.sh"]]
verify = [["just", "verify"]]
outputs = ["package.json", "pnpm-lock.yaml", "nix/flake.lock"]
```

The explicit `eligibility="resolver"` declaration delegates selection, constraints,
release dates, lock audits and security exceptions to that hook. Chainman passes
`CHAINMAN_MINIMUM_RELEASE_AGE_DAYS`; it cannot prove an arbitrary hook obeys it. The
hook must fail when evidence is unavailable, refresh its environment after changing
inputs, and only resolve/generate files. Git operations belong to the surrounding
transaction. Verification must fully exercise the selected project's requirements.
For a Nix-only project, the `nix-update` internal command reuses the built-in age-aware
Nix input adapter during a managed transaction; configure `updates.nix` explicitly.

Apply requires a clean repository whose top-level directory is the adopted project,
a current branch and an existing commit. A nested example refuses its parent's Git
repository. The updater compares actual file contents/modes with the index and HEAD,
rejects hidden index flags and unexpected paths, and checks for concurrent source,
index or HEAD changes. Verification cannot quietly change the candidate. Only the
exact verified update files are committed, using ordinary Git identity and signing.
It never pushes, creates an empty commit or silently falls back to unsigned commits.
The exact candidate commit is created directly, so arbitrary Git commit hooks do not
run; all required checks belong in verification.

Preview copies visible project files into a disposable Git repository, rebinds the
project root and executes the same update and verification path. Nested launchers
verify/fetch the copied pin. It preserves the original checkout, disables external
Git configuration in the copy, and does not test the operator's signing backend or
filters. It uses the existing development host and shared caches, so it is not a
sandbox for hostile update scripts. Linked submodules require their own transactions.

Self-updates validate release metadata, source revision, SHA-256 identities, source
tree types and version before running a candidate. Locally edited bootstrap files
must be reconciled explicitly. Candidate runtime tests and full project verification
run from refreshed environments. If verification fails, the previous managed pin,
bootstrap and bundled archive are restored only where their current bytes still
match the transaction's candidate; concurrent edits are preserved and reported.
Other failed dependency edits remain visible for diagnosis. Prior installed runtime
generations remain available throughout the transaction.
Recovery assumes cooperating process locks and ordinary caught failures. An
unhandled termination (including SIGTERM, SIGHUP or SIGKILL) while a candidate pin is live can leave visible uncommitted candidate
files; inspect the Git diff and restore the previous managed files before retrying.
The conditional restore does not claim filesystem compare-and-swap against a hostile
writer changing a file between the final check and atomic replacement.
