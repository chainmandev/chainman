# Git trust and runtime pins

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
Review the repository and its qualification evidence to the degree your project needs.
The public HTTPS Git repository is the installation source. Loss of public access
can prevent a cold fetch; it cannot silently select substitute code.

Initialization without an explicit SHA and runtime updates resolve one snapshot of
Git's advertised default branch. The advertised `HEAD` must name a branch and agree
with its full commit SHA. Missing, malformed, or inconsistent remote information
fails explicitly. No hardcoded branch, tag, or release API is consulted.

Selection is frozen before source acquisition. Branch advances, renames, or history
rewrites do not replace a saved SHA during verification or resume. A future update
may select the new tip, even if its `VERSION` is unchanged or its history is unrelated.
Review the candidate diff and qualification accordingly. Explicit SHA initialization
obtains that exact revision without default-branch discovery.

Runtime selection has no age delay. The configurable 30-day maturity window applies
to project dependencies. Ordinary launches make no update query and need neither
`gh` nor GitHub authentication. An available verified cache supports offline launches;
initialization and updates need public Git access to discover the current tip.

Git is the sole chainman installation identity. Hashes for Nix inputs, container
images, native backends, and external dependency artifacts remain necessary and
are verified by their existing owners. The Git pin identifies chainman; these
additional hashes identify the separate tools and dependencies it operates.

Managed shells use a reviewed NSS certificate bundle with the compromised
eMudhra/emSign roots and Comodo AAA Services root excluded for TLS server trust.
The standard PEM form removes distrusted roots instead of relying on
OpenSSL-specific trust attributes. Python, uv, Node, curl, Git and Nix downloads
receive explicit CA configuration; an empty CA directory prevents an ambient
directory from augmenting it. Node uses OpenSSL's configured store; uv's explicit
certificate paths replace its embedded or native store. The Intel macOS
compatibility channel uses the reviewed current CA and uv package definitions.

`just trust-test` checks actual Python roots, Node's embedded roots and effective
configuration, and the fixed uv release. Root checks use DER SHA-256 fingerprints.
The Linux and Darwin qualification lanes run this check, and bootstrap tests
inspect the effective trust store after host and container handoff. The initial
public Git acquisition and a caller-provided host Nix installation remain trust
inputs managed by the host; chainman does not rewrite global host certificate
configuration. See the [NixOS incident mitigation](https://github.com/NixOS/nixpkgs/issues/576485)
and [uv certificate semantics](https://docs.astral.sh/uv/concepts/authentication/certificates/).
