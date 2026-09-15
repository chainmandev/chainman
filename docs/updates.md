# Verified dependency updates

[Guide index](README.md) · [Getting started](getting-started.md) · [Troubleshooting](troubleshooting.md)

`just chainman deps-check` validates the configured adapter names and
ordered update steps without resolving dependencies, running hooks, or requiring
a clean Git checkout. It accepts the same selection arguments as the resolver,
for example `deps-check --targets core`. Every declared resolution step is checked,
including adapters that are not selected. Use it when testing generated consumer
configuration against the pinned runtime. This is a configuration contract check,
not dependency eligibility auditing or application verification.

`just chainman deps-update` selects current eligible stable **project dependency**
releases, including majors, with a configurable 30-day maturity window. Resolution and verification run in a
disposable checkout. The host launcher sequences preparation, resolution,
inspection, verification and finalization; it needs neither host Python nor a
container-engine socket inside project containers. `mode=dry-run` stops before applying
the verified changes. `commit=off` applies them without committing, for a coordinated
checkpoint. Untargeted updates and `targets=all` include the Chainman runtime pin,
and its explicitly declared generated copies. The bootstrap recipe is unchanged. Explicit application targets
retain the runtime pin; `--skip-chainman` also selects project-only updates.
`just chainman chainman-update` updates only the runtime pin and its declared copies.
Runtime selection happens before project resolution. Resolution, reconciliation
and verification use the selected candidate runtime; the original checkout keeps
its previous runtime until the combined candidate has passed verification.
The Chainman source repository has no self-pin and updates only its declared tools.
Runtime selection resolves the public repository's advertised default branch to one
exact SHA, without a version or age filter. A different SHA is a candidate even if
`VERSION` is unchanged; an identical SHA retains normal no-change handling.
Missing or inconsistent Git information fails explicitly. `--skip-chainman` remains
available for project-only updates. Missing project dependency eligibility evidence
is still an error, never an implicit exemption.

Checkpoints retain the resolved runtime selection when resumed, regardless of later
CLI defaults. A failed runtime preparation can be explicitly retried with `resume=`
while the candidate still matches its original contents. Selection is saved before
fetching, so an interrupted download also retries the same SHA. After preparation succeeds,
resume retains that runtime, verifies its exact Git objects, and
re-audits/reverifies the candidate without rerunning project resolution.

Schema 3 consumers declare `updates.verify_task = "verify"` (or another finite
task). The launcher runs that ordinary task against the candidate's updated Nix
lock and verified runtime. Its setup groups, service readiness, container namespaces
and cleanup are identical to an ordinary task invocation. Start updates through the
host launcher, including on container-only machines, outside an active project
container. Legacy command/module verification remains available for schema 1.

Projects with mutually exclusive service configurations can instead declare
`updates.verify_tasks = ["verify-postgres", "verify-spanner"]`. The host launcher
runs each finite task in order and releases its service claims before the next
task starts. This differs from one task's `depends_on`, whose service graph is
acquired together. The first failure stops verification and preserves the
candidate; all tasks must pass against the same frozen candidate before apply.
Update verification is noninteractive and receives closed input (`/dev/null`).
Declare exactly one verification form.

Resolvers and verifiers can write the disposable checkout, but only trusted runtime
phases mount the private transaction metadata and original checkout. Candidate
launches use a separate read-only export of the original configuration, runtime
pin and verified source. Resolution and reconciliation cannot replace the runtime,
forwarded environment policy, host mounts or service declarations used by their
next launch. Candidate launches receive a dedicated writable directory through
`CHAINMAN_WORKSPACE_TRANSACTION_ROOT` for workspace tools that require atomic
sibling staging. The root is ignored inside the candidate checkout, so staged
files and their destinations remain on the same rename domain even in container
mode; the private control directory and original checkout remain unavailable. It
exists only for the retained update transaction and is removed with it after
success. The candidate
Git directory is mounted read-only, and its metadata never selects host administrative
mounts or signing policy. Automatic Git maintenance is disabled in disposable
checkouts so concurrent inspection cannot race packfile replacement. Inspection
freezes the allowed candidate files before verification and exports a bootstrap from
the verified runtime for host execution; it never executes the candidate's mutable
justfile on the host. Finalization checks the original HEAD, index and raw source
snapshot again before applying anything. A resolver failure, verification failure,
out-of-scope change or concurrent original edit leaves the original unchanged and
preserves the candidate under the host's Chainman update cache for inspection.
Candidate source changes during verification are failures. Failures while applying
or committing already verified files preserve the resulting files/index for review.
Each destination's raw contents and full mode are checked again immediately before
writing or deleting it. A detected edit to a later output stops application and
preserves that edit together with any earlier completed writes.
No reset, stash, push, background updater or automatic retry is involved.
Staged formatting also compares the complete resulting index against the verified
selected bytes/modes plus the original unrelated staging. A clean filter or ignored
executable-bit change that produces a different staged result fails and preserves
the files and index for inspection.
Registry metadata responses are bounded to 64 MiB, including complete package
histories; a larger response fails explicitly rather than dropping release evidence.
Crates.io API requests are serialized within each process with a conservative
one-second gap after each response, including retries and fresh reads, following its
[data-access policy](https://crates.io/data-access). This preserves spacing even if a
thread pauses just before sending; response time adds to the interval between requests.
Cached reads and separate sparse-index/CDN hosts do not incur that delay. This is
process-local pacing, not an aggregate limit across projects or machines sharing an
IP address; run large API update lanes serially when they share that allowance.
Registry requests retain three attempts and a 30-second network timeout per attempt.
Retryable HTTP failures honor valid `Retry-After` seconds or HTTP dates using current
UTC, with a maximum 60-second wait. Longer valid waits fail explicitly rather than
retrying early. Missing or malformed hints use the existing one-/two-second backoff.
Interrupted waits propagate; exhausted requests fail without cached substitute evidence.
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

The minimal starter declares its project-owned Nix inputs. The larger repository
examples demonstrate ecosystem adapters and module manifests. Explicit constraints require reasons;
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
verify_task = "verify"
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

`just chainman deps-update targets=javascript,assets` selects these
targets. For JavaScript, `--policy compatible` preserves original caret/tilde and
complex dependency ranges, including their lower and `0.x` compatibility bounds.
Simple exact versions are update templates bounded by the original version's caret
range; held versions and explicit policy/override constraints remain stricter.
Final declarations preserve their original range form, with only a simple version
advance within that bound; reconcile other declaration changes separately.
Selection, fallback and final lock audits use the original recorded requirements.
The default is `aggressive`, subject to every explicit constraint. `--target-policy name=compatible`
sets an individual target's mode. `updates.target_groups` maps aliases to target
lists. Command-only targets are declared in `updates.targets` and require a matching
command step. An adapter with `explicit_only = true` is excluded from default and
`all` selection; select it by name or a declared group. This is useful for optional
SDK source refreshes that require additional upstream evidence. Hooks receive JSON `CHAINMAN_UPDATE_TARGETS` and
`CHAINMAN_UPDATE_POLICIES`, plus the frozen `CHAINMAN_UPDATE_AT` timestamp.

Publication times are validated against host UTC when immutable artifact and release
evidence is constructed. This metadata-observation clock is separate from both HTTP
receipt time and the frozen update timestamp. Releases published after the update
timestamp remain ineligible, including with a zero-day age window or an exact security
exception. Later observations and cached evidence never advance the release-age cutoff
or the timestamp used to check exception expiry.

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

Swift discovery supports literal public GitHub `from:` and `exact:` release
requirements and contained literal local package paths, including named locals.
Compatible `from:` selection uses Swift's next-major interval, including
`0.63.2..<1.0.0`; aggressive selection may update the release literal beyond the
original interval. Native manifest evaluation must agree with the complete supported
declaration inventory and its source kinds. Automatic discovery rejects computed,
ambiguous, branch/revision and unsupported dependency forms. Existing explicit
Swift regex pins may instead own an exact argument's literal or a named `let`/`var`
initializer containing a stable version literal, optionally typed `Version` or
`PackageDescription.Version`. The URL argument may be a literal or a named constant.
The owning field, manifest, canonical repository and selected version must match the
complete native inventory. Unused or overlapping fields and unowned computed calls
fail explicitly. This bounded compatibility route rejects computed expressions,
range arguments and local paths. Local paths retain lexical
containment and symlink checks and are never treated as registry packages.

Swift resolution temporarily narrows selected direct releases to exact requirements,
then restores the intended public `from:`/`exact:` syntax and full file modes before
audit. Restoration only overwrites expected temporary bytes; conflicting changes
are preserved and reported. Selected direct versions and immutable identities must
survive native resolution and later hooks. Every actual remote lock artifact,
including transitives reached through local packages, retains the ordinary release
age, source/revision and package-policy checks. An impossible native graph fails;
local package support does not exempt its remote dependencies from audit.
The read-only Swift input guard follows the reachable contained local manifest
closure without adding those manifests to configured pin selection. Final audits
run a forced-resolved native dependency graph in a separate command-root scratch
cache under `TOOLCHAIN_WORK`. Its remote repository/version inventory must match
that command root's `Package.resolved`; descendant locks cannot substitute for it.
The frozen graph command may refresh repository objects: a shared cache populated
before an update can lack the newly locked commits. It keeps
`--force-resolved-versions`, and the input guard rejects any lock rewrite.
Native graph success alone does not prove lock completeness. An absent lock is
valid only for a complete all-local graph. Manifest closure, lock bytes and full
modes must remain unchanged by validation; unexpected changes are preserved and
reported. Native graph paths never replace lexical project-local source checks.


JavaScript `peer_exceptions` require an exact `manifest`, source package `source`,
`peer` package and nonempty `reason`. They exempt only that owning importer's
edge from peer-range syntax and membership checks, including transitive copies
in its final npm or pnpm graph. Peer names, containers and string values remain
validated; another importer does not inherit the exception. Use explicit package
constraints and real project verification to bound any replacement compatibility
contract. These exceptions never waive release age, artifact identity, direct
dependency constraints or security floors.

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
The configured Go source adapter rejects retracted public requirements, including
unchanged direct and indirect requirements. Declared local modules are excluded.
In this adapter, unchanged historical checksum entries alone do not trigger
retraction or publication-age lookups; new checksum identities retain the full
retraction, age and hash audit. The legacy generic lock audit remains stricter
over existing checksum identities.

For the ordinary Rust `cargo update` command, selected direct dependency fields
are temporarily narrowed to their exact planned releases. Cargo still enforces
all parent ranges, features and target constraints. Chainman then repairs newly
ineligible registry artifacts through conservative, source-qualified Cargo
`update --precise` attempts, trying eligible releases in descending order with a
default maximum of 64 repair attempts. A Rust adapter can explicitly set
`cargo_max_attempts` to an integer from 1 through 512 for a larger graph or a
smaller effort allowance. This setting is rejected for other adapters and custom
resolver commands, and invalid values fail before dependency selection or edits.
The budget counts native repair calls across every directory and search branch
in that adapter; initial ordinary `cargo update` calls do not consume it.
It is a finite effort bound, not a wall-clock deadline.
Recorded lockfile dependency edges prioritize ineligible
parents before the ineligible children they constrain, including paths through
eligible intermediates. Cyclic groups retain deterministic ordering and the same
bound; graph ordering never changes the eligible release set or native constraints.
If one precise attempt encounters a native version conflict, Chainman can retry
that same version while also unlocking registry packages that share an exact
direct dependency with it in the same workspace. Eligible peers are included;
exact versions already chosen by earlier repairs in that workspace stay locked.
Other versions and independent workspaces retain their own search choices.
Unrelated packages, local paths and other registries are excluded. Cargo receives
the requested target first, followed by the peers in deterministic order. This
uses Cargo's current same-registry precise-hint behavior; the resulting target
must match exactly. Cargo can also consolidate duplicate versions without
materializing the requested version. Chainman accepts that outcome only when the
old target disappears and the remaining identities for that package form a
nonempty subset of those already present in the same workspace, with identical
versions, sources and checksums. Complete package disappearance and new substitute
identities are rejected. A merge preserves earlier repair choices without freezing
other surviving versions; ineligible survivors must still be repaired. Every
resulting artifact receives the normal audit.
If Cargo nevertheless moves an earlier choice, that branch is still rejected.
Both individual and coordinated attempts count toward the same configured limit.
Native version conflicts may cause bounded backtracking;
unrelated native failures, missing evidence and unexpected input edits stop the
update. This is a bounded eligible-graph search, not a complete solver or a proof
of a globally newest dependency graph. Custom resolver commands keep their literal
behavior and the existing fail-closed audit; they receive no invented repair flags.

Temporary direct constraints change only existing selected fields, never add
synthetic transitive dependencies or source overrides. Public post-planning
manifest bytes and permissions are restored on completion and caught failures.
Only still-identical owned lock postimages may be rolled back; unexpected edits
are preserved and reported. Uncatchable termination has no restoration guarantee
and is never a successful or automatically committed update. Git transactions
retain the whole visible-project input guard. Non-Git public resolution guards
reachable local Cargo manifests, ancestor Cargo configuration, local `src`,
`build.rs` and explicitly declared target paths; this is not complete build-input
discovery. Source symlinks are recorded without following them; mutable manifests,
resolution configuration and owned lock paths must remain regular and contained.
The resolver does not run application build scripts.

Final audits retain the original exact-baseline, age, compatibility, security-floor,
exception and checksum rules. Later project hooks must preserve the selected Cargo
artifact graph in each declared workspace. Restoring public ranges does not rerun
Cargo update, and verification cannot repair or regenerate the frozen lock graph.

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

Pub discovery uses the effective native resolution group. A sibling
`pubspec_overrides.yaml` replaces each attribute it declares, including an empty
`dependency_overrides` or `workspace`; absent attributes keep their inline values.
Contained literal workspace members and member invocations share their overrides,
while independent adapter roots and packages used only as dependencies do not.
Duplicate workspace overrides, partial manifest selections, nested/glob workspace
forms and unsupported resolution/source descriptors fail explicitly. A member can use an effective
`resolution: null` to resolve independently.

Effective contained path and Flutter SDK dependencies receive no hosted lookup or
version pin. Their shadowed publishable ranges remain unchanged. Local package
names and target manifests are checked, and resolved lock sources must be present
and agree with the effective declarations. Only actual workspace members may be
omitted from their shared lock. Effective overrides require their resolved nodes
even when no ordinary dependency names them. Hosted overrides must retain the
selected name, version and pub.dev source in both native output and the final
post-hook lock; changing them to local or SDK entries does not bypass the audit.
Effective hosted overrides also keep their original bytes and permissions:
selection stays within that authoritative range and policy,
and resolution temporarily narrows only the owning override to the highest
eligible selected version. A numeric token in an override range is not treated as
an installed version to retain. The shadowed ordinary range is not pinned. A native
result that ignores that selection fails; an ineligible exact override cannot be
rewritten to escape the policy. Manifest, override and local-target manifest
changes remain guarded through resolution and the final post-hook audit.

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
The JavaScript evidence cache retains the complete version inventory, publication
dates, artifact identities and dependency/peer ranges. It discards unrelated parsed
release manifest payloads after validation so transitive checks do not accumulate every
historical README, script and development dependency declaration. This does not
change registry transport limits or eligibility rules.

A common `updates.constraints["npm:name"]` rule's `range` may also be a nonempty
array of range strings; every member must match. Shared peer scopes use this
conjunction directly instead
of expanding combinations of alternatives. Each range keeps its own prerelease
rules. Arrays are limited to 128 members, 128 total `||` alternatives and 65,536
characters; malformed members fail even when another member already rejects the
version. Scalar ranges retain their existing behavior, and other providers require
scalar ranges. The same conjunction governs selection, artifact audits and security
exception retirement. The separate `javascript` catalog, package, prefix and
override policy rules continue to require scalar ranges.

For centrally governed pnpm projects, `reconcile_policy=true` applies
`javascript.catalog_constraints`, `package_constraints` and `override_constraints`
before solving. Each rule contains a `range` and `reason`. Default catalog rules
replace direct registry declarations with `catalog:`; explicit named catalog and
local workspace references remain intact. Per-manifest rules retain intentional
range exceptions. Final audit checks the same declarations for drift.

Policy reconciliation writes pnpm overrides to `pnpm-workspace.yaml`, which is
supported by current pnpm 10 and 11 releases. It moves existing root
`package.json` pnpm overrides there, preserving selectors outside the configured
rules and other pnpm settings. Equal duplicates are coalesced; conflicting root
and workspace declarations fail for explicit reconciliation. The final audit
rejects a still-required migration without writing files. This migration requires
`reconcile_policy=true`; it does not migrate other pnpm settings or npm projects.
Compatible updates retain each moved override's original range through its exact
selector and package identity. Final audit rejects widened override declarations
even when the current lock still falls inside the original range.

After exact resolution, pnpm normalizes the restored declarations before the
candidate is frozen. Only importer/catalog specifiers and override metadata may
change; every selected identity, artifact and dependency edge must stay unchanged.
Normalization failure or input drift aborts the update. The subsequent frozen
lock check and full audits never regenerate files or retry verification.
pnpm's frozen check alone does not prove that transitive versions satisfy their
parents. Chainman also compares registry dependency edges with published ranges
and declared overrides, and rejects missing required children. Optional omissions
and explicit dependency removal overrides remain valid. Bundled children belong
to the parent's hashed archive and do not require separate registry lock entries.

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

Native integrations use `just chainman deps-query`: one JSON request on stdin,
one schema-versioned JSON response on stdout. Schema 1 supports `select`, `metadata`
and `audit`; providers include npm, PyPI, crates, pub, GitHub, Docker, Go, Swift and
Maven. Selection accepts `provider`, `package`, optional `current`, and an optional
`constraint={range,reason}` (including the npm conjunction array described above).
A retained current version is labelled `retained` and
does not acquire eligibility evidence from an older candidate. Retention still
requires both the request's compatibility bound and the effective configured
package bound. A conflicting current version fails for explicit reconciliation;
the query never silently downgrades it. Go selection first applies the native
unretracted inventory, configured constraints and any compatibility bound, then
checks release metadata in descending version order until it proves the highest
mature safe candidate. Missing evidence for a potentially winning version fails;
irrelevant lower versions do not block selection. Exact metadata and added artifact
audits still require their own age and immutable identity evidence. `metadata` requires
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

Each update copies visible project files into a disposable Git repository, rebinds the
project root and executes the same update and verification path. Nested launchers
verify/fetch the admitted entry pin. Refreshed subprocesses bind that copied project
explicitly, even when their executable belongs to the original immutable runtime.
Clean-source updates preserve the current commit identity and branch in a shallow
copy, so version checks see the same baseline revision. Previews may include dirty
sources and create a disposable baseline commit instead. Neither copies remotes,
hooks or older history.
Before candidate code runs, entry authority freezes the main configuration,
secondary dependency policy, runtime pin and verified source outside the writable checkout.
Reconciliation task selection and its transports use those frozen inputs. Candidate
Git administration, including every initialized nested submodule, is frozen as raw
bytes and mounted read-only in containers. Trusted inspection checks that complete
metadata inventory before invoking Git, and compares source bytes without executing
Git clean filters. A changed policy or administrative file cannot authorize its own
inspection or broader host access.
Re-audit reconstructs the baseline from immutable Git blobs, including deleted
files, and obtains fresh dependency eligibility evidence before resuming acceptance.
Staged formatting restores excluded paths before verification, so the checked
candidate is the exact partial change that will be applied.
Source symlinks must be relative and remain within
their copied project or submodule; absolute, escaping and cyclic links are rejected
before update hooks execute. It preserves the original checkout, disables external
Git configuration in the copy, and does not test the operator's signing backend or
filters. It uses the existing development host and shared caches, so it is not a
sandbox for hostile update scripts. Linked submodules require their own transactions.

Runtime updates resolve the public default branch once, with Git as the sole runtime
identity. Branch movement during qualification does not invalidate the snapshot.
Resume uses the saved SHA; it never substitutes a newer tip. A later update discovers
the newer default branch. `VERSION` is descriptive metadata, and release tags,
publication dates, and GitHub release immutability are not selection requirements.
Corrupt Git objects and unavailable selected revisions fail explicitly. Ordinary
launches remain pinned and do not discover updates.
Consumer verification runs from refreshed environments under the selected runtime.
Until verification passes, the original pin remains
untouched in the original checkout. Failed candidate files remain available for
diagnosis. Prior installed runtime generations remain available throughout the
transaction. Partial runtime publication inside the candidate restores only
still-identical managed outputs; concurrent edits are preserved. Final application
assumes cooperating process locks, not filesystem compare-and-swap against a hostile
same-user writer. Termination during final application can leave verified but
partially applied files; inspect the original diff and preserved candidate before
retrying.
Resume requires the original HEAD, index and source snapshot to match preparation.
After partial application, reconcile the original checkout explicitly before
resuming; previous candidate inspection is discarded and verification runs again.
If interruption happened after branch publication, the verified commit may already
exist even though no success response was printed. Inspect HEAD and its diff before
starting another update.

Runtime revisions are qualified in the Chainman source project. Consumer runtime
upgrades validate the candidate configuration and run the declared project verifier
using the candidate runtime; they do not run Chainman's development test suite.
Git supplies the complete revision, but project acceptance does not invoke the
Chainman development suite.

## Runtime copies in generated projects

A repository that embeds the same runtime in templates or example projects can
list their relative roots under `[runtime]`, for example
`copies = ["templates/common", "examples/demo"]`. Each copy must contain identical
plain `chainman.lock` bytes and Git executable identity at its relative root. Customized or missing copies are rejected
before replacement; reconcile their ownership explicitly.
Ordinary checkout umasks, immutable store permissions and canonical export modes
may differ without changing that identity. Other mode flags must still match.
Transaction snapshots retain each file's full mode: changes during preparation,
verification, application or rollback are still detected and preserved.

Updates that include Chainman prepare every declared copy from the verified runtime in the
same candidate transaction. The normal project gate verifies the complete change
before any original files are applied. The original declaration fixes the output
boundary, and ordinary dependency resolvers cannot change these runtime files.
Only the declared pin files are copied; project configuration and source
remain owned by their project or generator.

See [standard recipes](recipes.md) for formatting, staged hooks, candidate resumption,
coverage and vulnerability audits.
