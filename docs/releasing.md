# Publishing a release

[Documentation index](README.md) · [Contributing](contributing.md) · [Release trust](release-trust.md)

Installation uses Git. A release is a qualified commit, a lightweight `vVERSION`
tag pointing to that commit, and published GitHub release notes. There are no
custom runtime archives, checksums, or generated asset inventories.

## Prepare

1. Set `VERSION`, update the release notes, and commit implementation, tests, and docs.
2. Run the required qualification described in [testing](testing.md). Record the
   exact commit, test evidence, platform omissions, and consumer blockers.
3. Ensure the intended version has neither a remote tag nor a release. Review the
   full source commit; the workflow refuses to replace an existing identity.
4. Push the qualified commit to `chainmandev/chainman` using the operator's credentials.

The manually dispatched **Publish Chainman Git release** workflow takes:

- `candidate_sha`: the full commit at the workflow ref you select.
- `version`: its numeric `VERSION`, initially `0.1.0`.

The workflow and checkout must both resolve to that exact SHA. Qualification runs
before publication. Publication creates the lightweight tag atomically, publishes
the notes, then clones the public tag, verifies its identity, and initializes and
checks a fresh starter through public Git.

GitHub release immutability can remain enabled as publisher policy. Installation
and release selection do not depend on it. No attestation client is required to
launch Chainman. Dependency hashes and backend provenance still belong to their
respective verification policies.

## Read back before promoting consumers

Use a fresh download cache. Check the public tag commit, `VERSION`, and release's
published/non-prerelease status. Qualify fresh host-Nix and container-Nix launches
with no local Chainman checkout dependency. Verify each consumer's recorded SHA
against that identity, including generated copies, then run its full declared gate.

Promote a rewritten consumer candidate only when those checks pass. Keep its
recovery refs and historical mapping; never merge the original archive-bearing
history into the replacement. Consumer pushes are a separate operator action.

A failed publication is not permission to overwrite a tag. Inspect whether ref
creation or release publication succeeded before retrying. Once a public release
exists, corrections use a new version and a new commit.
