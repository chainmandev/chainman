# chainman alpha: rolling Git snapshots

chainman coordinates project-owned environments, setup, services, caches, and
verified updates. Container Nix is the default; host Nix is supported explicitly.

Consumers commit a small justfile recipe, a full Git SHA in `chainman.lock`, and
their own configuration. The selected Git revision supplies all implementation.
Ordinary launches stay pinned; no global installation or GitHub release is required.

For a new or empty directory, use
`just init DEST` from a disposable checkout to select the public default branch.
Pass an explicit full SHA to reproduce a particular runtime. Initialization makes
an initial Git commit unless `--no-git` is selected; setup and verification follow. Existing repositories use the manual adoption guide.

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

## Development workflow improvements

- Nested task wrappers can reuse a verified environment through
  `bootstrap/reenter.sh` while retaining setup admission and leases.
- Profile `inputs` cover imported Nix modules and toolchain pins. `setup-status`
  names changed, added, and missing files after a readiness record is refreshed.
- Foreground service workflows stream logs and watched-build status. Inspect
  saved logs with `just chainman services-logs --follow`; detaching does not stop services.
- Standalone preview tasks can publish loopback ports selected by declared
  environment variables. See [services](services.md) for the complete example.

## Shared Git hooks and package-manager setup

Complete `just setup` installs the optional worktree-owned lefthook preset.
Pre-commit formats only staged content, preserving unstaged edits; pre-push scans
all outgoing source history using pinned upstream Trojan Source detection.
Projects declare small formatter environments and compose their own checks.
`just setup --no-hooks` prepares disposable/CI workspaces without Git hooks.
The `pnpm = true` setup declaration validates the profile's exact manager version,
installs frozen dependencies and checks readiness without downloading another pnpm.
See [Git hooks](hooks.md) and [configuration](configuration.md).
