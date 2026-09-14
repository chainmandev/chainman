# Publishing a release

[Guide index](README.md) · [Runtime and trust](runtime.md) · [Contributing](contributing.md)

## Before dispatch

Use one canonical public identity per version. v0.1.0 replaces incompatible local
pre-publication artifacts; those artifacts are not a public release. Migrate any
such consumer explicitly, including its revision and hashes: the normal updater
skips an equal version. Historical pins are not retroactively made installable.

1. Commit the complete source, tests, inventories, and documentation. `VERSION`
   must contain the requested numeric version.
2. Run required local qualification and push that exact commit to
   `chainmandev/chainman`. The selected workflow ref must point to that commit.
3. In repository **Settings → Releases**, enable **release immutability before
   publication**. Ensure Actions can write contents and attestations and request
   an OIDC token. Do not publish a provisional release under the final tag.
4. Record the full source SHA with `git rev-parse HEAD`.

The repository immutability-settings API requires administration access, which
the workflow's normal token does not have. The workflow requires an explicit
operator confirmation of that setting and checks the actual release's immutable
status immediately after publication. Confirming the input does not enable the
setting itself.

## Workflow inputs and behavior

Open **Actions → Publish immutable Chainman release → Run workflow**. Select the
ref at the qualified commit and supply:

| Input | Value |
| --- | --- |
| `version` | `0.1.0` for the first public release |
| `candidate_sha` | Full 40-character source commit at the selected ref |
| `immutability_enabled` | Checked only after enabling the repository setting |

The workflow checks the exact requested source/version, calls the portable and
native qualification matrix, builds twice and compares every artifact, creates
build-provenance attestations, creates a draft, uploads the complete asset set,
then publishes. It verifies GitHub's release attestation and each asset and runs
a fresh public installation. Qualification failure prevents publication.

| Asset | Purpose |
| --- | --- |
| `chainman-0.1.0.tar.gz` | Executable runtime and its Nix/tooling sources |
| `chainman-source-0.1.0.tar.gz` | Source, tests, docs, examples, and starter |
| `chainman-release.json` | Version, exact revision, archive URLs, flat SHA-256 and NAR hashes |
| `SHA256SUMS` | Flat SHA-256 inventory for the other release files |

Ordering, timestamps, permissions, and compression are deterministic. Metadata is
outside the archives to avoid self-referential hashes. There is no separate
publication waiting period. Backend source hashes, provenance, and qualification
remain required; consumers' automatic dependency age policy remains independent.

If upload fails while still a draft, inspect the draft and uploaded asset inventory
before retrying. The workflow does not clobber existing assets. Once published,
corrections receive a **new version**. GitHub immutability applies to releases
published after the setting is enabled and locks their tag and assets.
[GitHub's guarantees](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).

## Public readback and adoption

Read back the published tag's commit, immutable status, release metadata, and all
asset sizes/digests. Compare them to the qualified local build. Then run the README
quickstart in fresh host-Nix and container environments. A local archive override
does not demonstrate public availability.

Only after readback should a migration promote URL-only consumer pins. Record the
version, full revision, flat hashes, and NAR hashes. New release selection rejects
mutable releases, missing/inconsistent evidence, moved tags, and insufficient age.
Ordinary launches use the checked-in pin and Nix hash verification without GitHub
authentication or online attestation checks.

## Optional attestation verification

Maintainers can use the source checkout's pinned release shell; `gh` need not be
installed on the host:

```sh
just exec-in release gh release verify v0.1.0 --repo chainmandev/chainman
just exec-in release gh release verify-asset v0.1.0 dist/release/chainman-0.1.0.tar.gz --repo chainmandev/chainman
just exec-in release gh attestation verify dist/release/chainman-0.1.0.tar.gz --repo chainmandev/chainman
```

GitHub CLI authentication may be required for these optional maintainer commands.
GitHub's release attestation binds the release tag and uploaded assets. The build
attestation additionally records the producing workflow and source. GitHub's
automatically generated source ZIP/tar downloads are not our deterministic source
asset. See [GitHub's attestation verification documentation](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/verify-attestations-offline).
