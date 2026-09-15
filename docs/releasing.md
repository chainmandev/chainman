# Publishing a rolling revision

[Documentation index](README.md) · [Contributing](contributing.md) · [Git trust](release-trust.md)

New installations and runtime updates select the public default branch's current
SHA. Publication means advancing that branch to a qualified commit. Tags, GitHub
releases, and a changed `VERSION` are optional descriptive records, never installation
requirements. Qualify before advancing the default branch: consumers can select
its new tip immediately.

## Prepare and qualify

1. Commit the implementation, tests, and documentation. `VERSION` is package metadata;
   record the full Git SHA as the authoritative identity.
2. Run [qualification](testing.md) in disposable fixtures. Record commands, results,
   unavailable platforms, and consumer blockers against that exact source commit.
3. Push the commit to a temporary **staging branch**, keeping the default branch at
   its current qualified revision. Use a new staging branch name for each candidate.
4. Dispatch **Publish rolling Chainman** (`release.yml`) at that staging ref, with
   `candidate_sha` set to its full SHA and `expected_default_sha` set to the observed
   public default-branch SHA. Both are required; the workflow code and checked-out
   candidate must agree exactly.

For an already committed candidate, these operator commands discover the current
public default branch without naming it:

```sh
candidate=$(git rev-parse HEAD)
base=$(git ls-remote --exit-code https://github.com/chainmandev/chainman.git HEAD | cut -f1)
staging="qualification/$candidate"
git push origin "$candidate:refs/heads/$staging"
gh workflow run release.yml --repo chainmandev/chainman --ref "$staging" \
  -f candidate_sha="$candidate" -f expected_default_sha="$base"
```

`gh` is an operator convenience for workflow dispatch; the GitHub Actions UI accepts
the same inputs. It is not a consumer prerequisite. The workflow runs all required
lanes before publishing. It discovers the default branch at publication, requires
its expected old SHA and fast-forward ancestry, and uses an exact lease to reject
concurrent movement. It then proves fresh public host/container initialization.

Repository Actions must permit the publication job's `contents: write`. Branch
protection must allow that qualified workflow to advance the default branch. If
policy requires a maintainer push, first run **Deliberate toolchain verification**
at the exact staging SHA, inspect every lane, then perform a guarded fast-forward
publication as the maintainer. Do not weaken required gates to bypass a failure.

## Read back before promoting consumers

After publication, use a fresh download cache to confirm that public Git advertises
the expected SHA. Qualify fresh host-Nix and container-only installs in paths with
spaces, without a permanent Chainman checkout. Record `VERSION` descriptively and
verify every root/generated consumer pin against the exact Git identity.

Promote each rewritten consumer candidate only after public readback and its full
project gate pass. Keep recovery refs and historical mappings; do not merge the
original archive-bearing history into a replacement. Consumer pushes are a separate
operator action.

If the branch moved during qualification or publication was interrupted, inspect
public Git before retrying. Qualify any revised candidate as a new exact commit.
Published corrections use new commits; existing pins and retained update candidates
continue to identify their original snapshots. Release immutability may remain a
publisher policy, but does not govern installation or runtime selection.
