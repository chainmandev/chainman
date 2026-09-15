# Chainman alpha: rolling Git snapshots

Chainman coordinates project-owned environments, setup, services, caches, and
verified updates. Container Nix is the default; host Nix is supported explicitly.

Consumers commit a small justfile recipe, a full Git SHA in `chainman.lock`, and
their own configuration. The selected Git revision supplies all implementation.
Ordinary launches stay pinned; no global installation or GitHub release is required.

Start with manual adoption in the README. For a new or empty directory, use
`just init DEST` from a disposable checkout to select the public default branch.
Pass an explicit full SHA to reproduce a particular runtime. Initialization makes
an initial Git commit unless `--no-git` is selected; setup and verification follow.

Runtime updates select the current default-branch SHA without a maturity delay,
then run reconciliation and the complete project gate in an isolated candidate.
The SHA stays frozen through failure and resume. Project dependency updates retain
their configurable 30-day policy. Successful updates commit by default; preview
and no-commit modes remain available.

Temporary security age exceptions are removed from TOML during verified dependency
updates once all resolved artifacts in the audited scope are mature and safe, or
the dependency has disappeared. Cleanup retains the original policy as transaction
authority and does not maintain a second inventory of historical security floors.
See the [exception lifecycle](updates.md#temporary-security-exceptions).

`VERSION` is descriptive package metadata. Expect interface changes during alpha;
review candidate changes and preserve each project's acceptance criteria.
