# Verified dependency updates

`just deps-update` selects current eligible stable releases, including majors, with
a configurable 30-day maturity window. `--preview` performs resolution and verification
in a disposable copy. `--no-commit` leaves the verified changes for a coordinated
checkpoint. `--only-chainman` updates the runtime pin and managed bootstrap;
`--skip-chainman` updates project dependencies without querying runtime releases.
Until the first public Chainman release is published, use `--skip-chainman` explicitly.
A missing release source or eligibility date is an error, never an implicit exemption.
Registry metadata responses are bounded to 64 MiB, including complete package
histories; a larger response fails explicitly rather than dropping release evidence.
`--message TEXT` supplies the verified commit's message. Native callers can use
`--json` for exactly one schema-1 JSON result on stdout, with command output on
stderr. The result includes `changed` (an array of project-relative paths), `commit`
(an identity or null), and `verification`; previews additionally include `preview`.

Tracked submodules remain frozen, read-only inputs. Uninitialized submodules stay
empty; Chainman never fetches them. Initialized inputs must match their recorded
commit, index, raw source bytes and executable modes, without hidden index flags
or untracked files. A preview copies only each current commit and source tree into
an independent shallow repository, without old objects, remotes, configuration or
hooks. It preserves nested submodules by the same rules. Submodule source, pin,
initialization and `.gitmodules` changes require a separate transaction, even when
the project's output patterns contain a wildcard.

The standalone example uses built-in registry adapters, `dependencies.toml`, module
manifests/lockfiles and `sdk-versions.toml`. Explicit constraints require reasons;
security maturity exceptions need a narrowly scoped advisory, minimum safe version
and expiry. Nix branch pins use commit age; container image pins use the registry
update time bound to the manifest digest. Baseline artifacts do not retrospectively
become release-age-qualified. Newly selected artifacts require eligibility evidence.
Docker Hub discovery retains dated legacy tags whose digest is absent. An unselected
legacy tag cannot block a newer eligible image; a selected tag without a digest fails
instead of falling back. Missing dates and malformed nonempty digests remain errors.
Public Docker queries require a digest for the exact selected or requested record.
Toolchain changes force fresh environment entry before resolution and verification.
The runtime image is updated together with Chainman; consumer Nix inputs and workflow
revision pins remain project-owned update targets.

Projects declare shared adapters and ordered application hooks:

```toml
[updates]
minimum_age_days = 30
profile = "default"
verify = [["just", "verify"]]
outputs = ["nix/flake.lock", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "generated/labels.json"]
targets = ["assets"]

[updates.adapters.nix]
adapter = "nix"
inputs = [{ directory = "nix", input = "nixpkgs", repository = "NixOS/nixpkgs", branch = "nixos-unstable" }]

[updates.adapters.javascript]
adapter = "javascript"
directory = "."
profile = "default"
manager = "pnpm"

[[updates.steps]]
resolve = "nix"
[[updates.steps]]
resolve = "javascript"
[[updates.steps]]
targets = ["javascript", "assets"]
commands = [["node", "scripts/generate-labels.mjs"]]
```

`just deps-update --skip-chainman -- --targets javascript,assets` selects these
targets. `--policy compatible` retains the original dependency ranges; the default
is `aggressive`, subject to every explicit constraint. `--target-policy name=compatible`
sets an individual target's mode. `updates.target_groups` maps aliases to target
lists. Command-only targets are declared in `updates.targets` and require a matching
command step. An adapter with `explicit_only = true` is excluded from default and
`all` selection; select it by name or a declared group. This is useful for optional
SDK source refreshes that require additional upstream evidence. Hooks receive JSON `CHAINMAN_UPDATE_TARGETS` and
`CHAINMAN_UPDATE_POLICIES`, plus the frozen `CHAINMAN_UPDATE_AT` timestamp.

Adapters snapshot their original identities before any mutation. Nix steps precede
toolchain synchronization, which precedes package resolution. Each command enters
the current Nix profile afresh. Reconciliation finishes before all selected adapters
audit the final identities; the surrounding transaction then freezes, verifies and
commits those exact files. Verification must report drift, never regenerate and retry.

Supported adapters are `javascript` (pnpm/npm), `rust`, `python`, `go`, `flutter`,
`swift`, `gradle`, `actions`, `oci`, `nix`, `toolchain` and `artifact`. Package adapters accept a
project-relative `directory` (native adapters also accept `directories`) and a Nix
`profile`. Native `manifests` explicitly bound discovery; Gradle `catalogs` locate
version catalogs. Each adapter's optional `policy` adds scoped rules to the global
policy. A TOML `updates.policy_file` keeps larger policy tables outside the primary
configuration. Toolchain adapters probe Nix-supplied versions and synchronize
declared JSON/TOML/YAML pointers or exact regex pins only after dated release evidence
passes. Before Nix resolution, SDK snapshots record the actual tool version and
its immutable provider evidence. An unchanged version with identical pins and
artifact URLs, hashes and dates (or the same GitHub commit) may remain below the
age window. Changed identities require normal eligibility; constraints, safe
floors and exception expiry remain enforced. Final SDK audit probes the tools
again and refreshes registry observations. Application generators and
deployment-specific reconciliation remain hooks.

Actions release selection orders the complete GitHub Git tag advertisement before
consulting release metadata. A first-page release batch only saves requests; it
does not limit the candidate inventory. Remaining candidates use exact release-by-tag
metadata. An advertised tag with no published release (HTTP 404) is not selectable;
missing dates, contradictory positive release evidence, other HTTP errors, malformed
or incomplete ref framing, and the explicit response/ref bounds fail the update.
All released aliases of one version use the latest release/immutable-commit date
before maturity or security-exception retirement. Metadata for lower versions need
not be fetched after a higher compatible, safe, mature version is established.
When a configured version constraint, security floor or major hold is operative,
retaining a newer current SHA also requires its highest positively published version
identity to satisfy that limit. A proven incompatible current release yields to the
already qualified candidate; an unknown current version fails explicitly. A tracking
comment alone is not version evidence. Compatible unchanged baseline revisions keep
the existing age exemption.

An npm tool may instead declare `source_pin = { file = "nix/sources.json",
pointer = ["packageManager"] }`. That pointer holds exactly `version`, `url` and
`hash` strings. JSON, TOML and YAML are supported; the URL must be that release's
canonical npm tarball and the hash its registry SHA-256, SHA-384 or SHA-512 SRI.
The project's Nix derivation must consume these fields. Chainman selects the latest
eligible stable source, writes the complete record, refreshes Nix, and only then
probes the binary and renders its normal `pins`. The default permits major updates;
the selected Nix derivation must support the release or verification fails.
`mode = "compatible"` bounds source candidates to the existing version's semver
caret range, before maturity and security-exception retirement are evaluated.
An unchanged source may retain its existing age only with identical pre-update
binary, mirrors and immutable registry evidence. New hashes require normal age
eligibility even at the same version. Final audit binds the exact selected source
record as well as the actual binary and registry evidence. Source documents must
be regular files inside the project; writes preserve unrelated fields and file
permissions and reject observed concurrent changes. Disable package-manager
self-switching so the declared Nix source remains the runtime authority.

Rust, Python and Flutter may adopt an initially missing lockfile, but their final
audit requires the resolved Cargo, uv or pub lock. A resolver or reconciliation
hook cannot turn a deleted lock into an empty artifact inventory. Go and Swift
retain their native rules for dependency-free projects that legitimately omit sums
or resolved pins.

During Flutter resolution, selected direct dependencies are temporarily bound to
their exact eligible releases across all declared workspaces. If Pub selects a
newly ineligible transitive artifact, Chainman tries eligible releases through
ordinary temporary root dependency constraints, with at most 64 native solver
states. Parent ranges and declared overrides remain authoritative; the repair
never adds a dependency override. Conflicts without an eligible native graph fail.
Public manifest ranges, comments and file permissions are restored after each
attempt. Unexpected manifest or override edits are preserved and fail the update.
After a successful repair, offline Pub resolution normalizes dependency roles
against the restored manifests and must retain the exact selected artifact graph.
The final lock audit still enforces publication age, constraints, security floors,
exception expiry and immutable evidence. Verification then uses that frozen graph;
temporary pins grant no audit exemption.

Gradle resolution additionally visits every resolvable project and buildscript
configuration, requiring failures to stop the update. A root `dependencies` report
alone does not cover child projects. Exact `local_projects` coordinate-to-directory
bindings keep first-party composite/project dependencies out of registry selection;
each binding must remain inside the adopted project and contain its Gradle build
source. Gradle 8.3 or later reports actual build-tree identities and directories,
including included builds. Module substitutions must resolve to the exact declared
directory; undeclared and outside-project selections fail. After reconciliation,
an offline native inspection with strict locks and artifact verification repeats
that check without regenerating locks. Corresponding external lock or verification artifacts are rejected, so this
is not a publication-age exemption for artifacts from Maven Local or a registry.
The project owns its actual `includeBuild`/dependency-substitution configuration.

JavaScript discovery includes workspaces, catalogs, aliases, scoped overrides and
actual resolved peer relationships. Documented package/catalog/prefix constraints
remain effective during bounded resolution retries. Version-scoped overrides retain
their explicit compatibility ranges. Exact SDK-owned dependencies can be declared
as `held_dependencies` with a manifest, package and reason; changed artifacts still
require age and identity evidence. Registry and native lock audits cover transitive
artifacts as well as direct declarations. Expired security exceptions fail while
still needed; a mature constrained safe alternative retires an otherwise valid
exception. Retirement never exempts a newly selected young artifact.
Every declared security safe floor also applies to mature and unchanged artifacts;
peer constraints cannot force a fallback below it. Existing npm prerelease identities
may remain only with unchanged registry artifact evidence and valid constraints.
Stable candidate selection never introduces a prerelease.
An existing deprecated npm artifact may also remain only with the same locked
identity and matching current registry hash/URL evidence. Deprecation never admits
a new or changed artifact, including through a security maturity exception. Its
metadata remains available to audit unchanged direct and transitive dependencies;
constraints, safe floors, peer compatibility and exception expiry still apply.

For centrally governed pnpm projects, `reconcile_policy=true` applies
`javascript.catalog_constraints`, `package_constraints` and `override_constraints`
before solving. Each rule contains a `range` and `reason`. Default catalog rules
replace direct registry declarations with `catalog:`; explicit named catalog and
local workspace references remain intact. Per-manifest rules retain intentional
range exceptions. Final audit checks the same declarations for drift.

Local `file:`, `link:` and `workspace:` dependencies must bind their package names
to included workspace manifests within the adopted project. Directory lock entries
are validated against those manifests, and their resolved dependency and peer graph
is audited. Archives, undeclared directories, symlink traversal and escapes fail.
The optional `retained_sources` list supports dependency-free GitHub source packages
with exact `manifest`, `package`, `repository`, full `commit`, archive `sha256` and
`reason` fields. Actual dated archive bytes and native lock integrity must agree;
mutable references and undeclared source graphs are rejected.
For an existing transitive source, add its exact registry `parent="package@version"`
and original `parent_specifier`. The owning manifest must reference that parent,
which remains held at the declared version. An exact parent-scoped pnpm override
must pin the configured GitHub commit before any resolution; an upstream mutable
declaration is compared as evidence and is never resolved. This form permits bounded
registry dependency and peer graphs from the hashed archive. Normal identity, age,
compatibility and peer audits cover every child, including nested registry children;
source retention does not exempt new child artifacts. Bundled dependencies and
nested remote or local sources remain unsupported.

Native integrations use `scripts/chainman.sh deps-query`: one JSON request on stdin,
one schema-versioned JSON response on stdout. Schema 1 supports `select`, `metadata`
and `audit`; providers include npm, PyPI, crates, pub, GitHub, Docker, Go, Swift and
Maven. Selection accepts `provider`, `package`, optional `current`, and an optional
`constraint={range,reason}`. A retained current version is labelled `retained` and
does not acquire eligibility evidence from an older candidate. `metadata` requires
an exact `version` and returns evidence without claiming eligibility. `audit` takes
`artifacts`, an array of `[provider,package,version,url,digest]` identities; caller
baseline claims never bypass maturity. `artifact-metadata` and `artifact-audit`
accept an exact HTTPS `url` and `sha256:...` digest, download and hash the bytes, and
label their age basis as origin artifact modification time, not release publication.
Artifact evidence uses direct HTTPS with verified host certificates. Every DNS
answer and redirect must resolve only to public addresses; the connection uses
those checked addresses without a second lookup. Ambient HTTP proxies are ignored
for these requests, so a proxy-only network cannot qualify this lane.
Missing, future or inconsistent evidence fails. `deps-resolve NAME` exposes a
configured adapter inside an active update transaction. Consumer code must not
import runtime Python modules.

Legacy projects can keep an explicitly owned resolver during migration:

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
`updates.nix.directory` selects the project-relative directory containing `flake.nix`
and `flake.lock`, defaulting to `"nix"`. Use `directory = "."` for a root flake.
Absolute paths, parent traversal, `.git` components and symlinked path components
are rejected. Declare the matching lockfile path in `updates.outputs`.

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
verify/fetch the copied pin. Refreshed subprocesses bind that copied project
explicitly, even when their executable belongs to the original immutable runtime.
Source symlinks must be relative and remain within
their copied project or submodule; absolute, escaping and cyclic links are rejected
before update hooks execute. It preserves the original checkout, disables external
Git configuration in the copy, and does not test the operator's signing backend or
filters. It uses the existing development host and shared caches, so it is not a
sandbox for hostile update scripts. Linked submodules require their own transactions.

Self-updates validate release metadata, source revision, SHA-256 identities, source
tree types and version before running a candidate. The maturity window applies to
the commit and both required release assets' creation/modification dates, as well
as release publication. Downloads use asset IDs and must match GitHub's recorded
SHA-256 and size; missing evidence and a tag moving during download fail before
candidate evaluation. Locally edited bootstrap files
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
