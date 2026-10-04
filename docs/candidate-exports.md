# Dependency candidate exports

[Documentation index](README.md) · [Updates](updates.md)

Automation can request an update without applying it to the original project:

```sh
just chainman deps-update --export-candidate /absolute/path/to/new-bundle
just chainman candidate-check /absolute/path/to/new-bundle
```

In Chainman's source checkout, omit `chainman` from both commands. Both entrypoints
use the same capture and result implementation. Source development uses host Nix
and just with an exact copy of the current local Git commit; no published release,
global installation or self-pin is required.

The project must be clean and committed. The destination must not exist; its parent
must exist. It must be outside the project, runtime and update cache, without
symlink aliases, traversal, commas or newlines. Export cannot combine with format,
resume, dry-run or commit controls. Other selection options retain their ordinary
meaning. Source updates retain their `targets=all` restriction. Consumers still
include runtime updates by default; `--skip-chainman` selects project-only work.

Export runs normal selection, policy auditing and output inspection, freezes the
accepted output, and then runs normal candidate verification. It never applies
files or changes the original index/ref. Ordinary updates and legacy `--json`
output remain unchanged when export is absent.

## Results and custody

One schema-1 result is written to stdout and `result.json`; command logs go to
stderr. A failed verifier produces a nonzero exit even when accepted dependency
output exists. Failures before export admission, such as malformed options, a
dirty project or an unavailable bootstrap, can produce no bundle.

| `outcome` | Meaning |
|---|---|
| `verified_success` | Accepted changes passed verification without source mutation. |
| `complete_no_change` | Selection/audit completed without changes; ordinary no-change behavior skips verification. |
| `accepted_verification_failed` | Dependencies were accepted, but verification failed or changed candidate sources. |
| `failure_before_acceptance` | No accepted snapshot was established. |
| `unsupported` | The resolver or artifact shape cannot establish this contract. |
| `interrupted_or_unknown` | No final outcome is established. |

Result fields are `schema`, `kind` (`chainman.update-result`), `operation` (UUID),
`outcome`, `stage`, `exit_code` (integer or null), and `snapshot_sha256` (hash or
null). Stages are preparation, resolution, inspection, verification and complete.
The initial result is interrupted/unknown. Progress and final results replace it
atomically. Missing or incomplete results never establish success.

`candidate-check` rejects malformed bundles. Its zero exit means the record is
well formed, **not that the update succeeded**; inspect `outcome` separately.

The coordinator publishes immutable `snapshot.json` and `blobs/<sha256>` before
verification. Only the separate result changes afterward. After a hard
interruption an unreferenced snapshot can exist; directory existence alone does
not establish acceptance. Preserve unknown outcomes for inspection.

Bundles are caller-owned and outside automatic update-cache retention. Failed
temporary workspaces remain disposable. Export transactions cannot resume or
finalize; start a fresh export after a dependency or policy change. Develop source
repairs in a separate workspace.

## Snapshot schema 1

`kind` is `chainman.dependency-candidate`. Other fields are:

| Field | Content |
|---|---|
| `operation` | Same UUID as the result. |
| `base` | Exact `commit`, `tree`, and `object_format` (`sha1`). |
| `runtime_kind` | `source` or `consumer`. |
| `runtimes` | Entry, resolution and verification runtime commit/tree identities. |
| `selection` | Time `at`, resolver `arguments`, selected `adapters`, and runtime selection: `include`, `exclude`, `only`, or source `not_applicable`. |
| `configuration_sha256`, `policy_sha256` | Effective configuration/policy digests: sorted compact UTF-8 JSON with a trailing newline and TOML dates in ISO format. |
| `input_coverage` | `declared_inputs_only`; not arbitrary dependency discovery. |
| `inputs` | Declared adapter, configuration/include, policy, module and runtime inputs at the base. |
| `base_inventory` | Complete file inventory observed in the clean base project. |
| `outputs` | Exact authorized changes, including deletions. |

File entries are sorted by `path` and contain `path`, `sha256`, `mode`, and `size`.
Regular-file modes are `100644` or `100755`. A deletion or absent input has null
hash/mode and size zero. Output hashes locate bytes under `blobs/`; other bytes
come from the admitted base project.

Version 1 supports regular files, 100,000 entries per inventory, 16 MiB per JSON
manifest, 256 MiB per file and 1 GiB total output payload. Symlinks/submodules,
duplicate keys, unknown fields/versions, unsafe paths, inconsistent statuses,
missing payloads and hash mismatches are rejected. Selected opaque resolvers and
custom targets without shared adapter audits cannot export accepted project
updates; their normal human workflows remain available. Runtime-only exports do
not select or audit project adapters.

## Repair integration and trust

1. Run `candidate-check` and independently match base/runtime identities to the
   intended project and trusted invocation. Require an accepted snapshot and an
   appropriate outcome; hashes do not authenticate execution.
2. Materialize a fresh checkout of `base.commit` and verify `base_inventory`.
3. Apply each output's exact blob and executable mode, or delete the stated path.
   Recheck hashes at use; do not copy the mutable failed workspace.
4. Repair source there. Protect declared dependency inputs/outputs and review
   other changes that could affect acquisition. Dependency-bearing changes need
   fresh selection/audit.
5. Independently verify the final source using the accepted project gate and
   required platform lanes. The caller owns this workflow and any publication.

Source control code and Python imports come from an exact Nix-store copy of the
base; project/tool environments and verification address the separate candidate.
Consumers retain their existing selected-runtime resolution and verification
behavior. The bundle reports actual identities; it does not promise that every
stage uses the base-side pin.

This is not a hostile-code sandbox or signature system. Host execution retains
the existing cooperating-process trust model. A service must isolate workloads
from control/evidence storage and credentials, qualify runtime versions, detect
additional dependency-changing repairs, and own retention and publication. This
interface exports runtime facts; it does not supply signed admission receipts.
