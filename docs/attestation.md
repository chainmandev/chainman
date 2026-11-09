# Artifact attestation and npm publication

Attestation is optional. Its pinned runtime is loaded only by these commands:

```sh
just chainman attest create path/to/manifest.json
just chainman attest verify path/to/manifest.json --run RUN_ID
just chainman npm-publish path/to/manifest.json --run RUN_ID
```

The first implementation supports public `github.com` repositories, GitHub-hosted
runners, and a direct signing workflow on an explicit branch, triggered by `push`
or `workflow_dispatch`. Unsupported providers fail explicitly. The `attest`
commands accept generic subjects; `npm-publish` adds npm-specific checks and
manual registry upload. Future providers can implement the same boundary without
changing project command names. GitLab is not implemented yet.

Declare the expected identity in `chainman.toml`:

```toml
[attest]
provider = "github-actions"
repository = "example/project"
workflow = ".github/workflows/qualification.yml"
ref = "refs/heads/main"
required_jobs = ["test", "sign"]
artifact_name = "qualified-release-{runId}-{runAttempt}"
tag = "next" # Required only for npm publication; currently the supported tag.
```

Job names support `*` and reusable-workflow prefixes. Every job in the selected
attempt must succeed; during creation only the current signing job may still be
running. Reserve the signing job for the end of qualification. The built-in
`attest` execution profile cannot be replaced by a project profile.

The project supplies a versioned manifest of its own expected files:

```json
{
  "version": 1,
  "sourceCommit": "0123456789012345678901234567890123456789",
  "artifacts": [{
    "path": "example-1.0.0.tgz",
    "subject": "pkg:npm/example@1.0.0",
    "digest": {"sha512": "<128 lowercase hexadecimal characters>"}
  }]
}
```

Paths are relative to the manifest, contained, regular, and free of symlinks.
Version 1 uses SHA-512. Creation recomputes each digest, signs one subject per
standard Sigstore bundle with the public Actions SDK, registers it with GitHub,
and saves `<path>.sigstore.json` beside the file. A manifest can explicitly name
its bundle with `attestation`. Upload only those bundles under the configured
attempt-specific artifact name. The signing job needs `contents: read`,
`actions: read`, `id-token: write`, and `attestations: write`, with `GITHUB_TOKEN`
available to the command. Other jobs need no signing permission.

Verification downloads bundles when they are absent locally. It authenticates the
exact saved bundles with pinned `gh`, including source commit/ref, direct signer,
public repository, hosted runner, run, and attempt. Downloaded manifests and
package archives never determine the expected subjects or bytes.

For publication, a project wrapper should require a clean checkout, snapshot its
committed files, pack them without lifecycle scripts using its pinned packer, and
derive this manifest from those local archives. The manifest's commit must equal
the successful run's commit, and every local digest must match a signed subject.
Chainman freezes those local bytes, preflights every package and registry version,
then uploads exact archives with pinned npm and saved `--provenance-file` bundles.
It rejects CI publication, private packages, conflicting tags, and unsupported
registries. Registry readback checks bytes, provenance, and `next`; a repeated
command can reconcile a partial upload. npm authentication remains local.

Project Actions results, this input manifest, and standard bundles are sufficient;
there is no additional release receipt or bundle index. An npm dry run does not
exercise upload or prove public-registry provenance acceptance.
