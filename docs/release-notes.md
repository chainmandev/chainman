# Chainman v0.1.0

Chainman's first public release is an experimental alpha for project-owned
development environments, setup, services, caches, and verified updates behind
`just` commands. It supports container Nix by default and host Nix, with no global
Chainman install or host language interpreter requirement.

Start with the repository README and `just init DEST 0.1.0`. New projects use
schema 3 and a URL-only, hash-pinned runtime. Existing projects keep their own
toolchain locks, commands, adapters, and acceptance tests.

Expect breaking changes during alpha. Explicit initial adoption can select this
release immediately; automatic updates retain the configurable 30-day age policy
and must pass project verification. Successful updates commit by default.

The release includes deterministic runtime and source archives, metadata, flat
checksums, NAR hashes, and build provenance attestations. GitHub release immutability
locks the published assets and tag. See the release trust and testing guides for
verification details and platform limits. Platform SDKs and application-specific
acceptance remain the consuming project's responsibility.
