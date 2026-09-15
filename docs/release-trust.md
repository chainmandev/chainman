# Release trust and Git pins

[Documentation index](README.md) · [Updates](updates.md) · [Publishing](releasing.md)

`chainman.lock` is exactly one full lowercase 40-character **commit** SHA followed
by a newline. The bootstrap's repository URL is literal. Branch names, tag names,
JSON locks, and fallback revisions are not accepted as runtime pins.

The bootstrap verifies Git object integrity before reading the initial entrypoint
from that commit. It ignores replacement objects and isolates cache Git operations
from caller repository routing and configuration. Cached materialized directories
are not execution authority. Runtime files come from the verified tree, and Nix
imports them into its store with lifetime roots and a check against the verified
source. Uncommitted files in an installation checkout cannot alter that runtime.

A commit pin identifies code; it does not establish that the code is trustworthy.
Initial adoption is the project's explicit decision to trust a selected revision.
Review the repository and the release provenance to the degree your project needs.
The public HTTPS Git repository is the installation source. Loss of public access
can prevent a cold fetch; it cannot silently select substitute code.

For version-based initialization and automatic updates, Chainman resolves a stable,
published GitHub release to its full commit, requires `VERSION` agreement, and
checks the tag again after obtaining the source. Drafts and prereleases are excluded.
Moving a tag during selection fails. Moving a tag after adoption does not alter an
existing pin; resuming an update cannot substitute a different revision.

Automatic updates additionally enforce the configured age policy using the later
of release publication and commit time. Explicit initialization bypasses that age
requirement. Ordinary launches do not call the release API and need neither `gh`
nor a GitHub token. Selection can use an optional `GITHUB_TOKEN` through the explicit
secret environment; it must never be written into project files.

Git is the sole Chainman installation identity. Hashes for Nix inputs, container
images, native backends, and external dependency artifacts remain necessary and
are verified by their existing owners. Removing Chainman's release archive protocol
does not remove those checks.
