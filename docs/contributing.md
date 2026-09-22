# Contributing

[Documentation index](README.md) · [Qualification](testing.md) · [Publishing](releasing.md)

Source development uses Git, just, and host Nix. The source justfile enters its
pinned development shell directly; it does not depend on an already published
chainman release.

```sh
just setup
just format-write
just type-check
just verify
```

Use focused tests while iterating, then run the full declared gate:

```sh
just exec python3 -B -m unittest discover -s tests -p test_git_bootstrap.py -v
just exec python3 -B -m unittest discover -s tests -p test_self_update.py -v
just exec python3 -B -m unittest discover -s tests -p test_update_staging.py -v
just hooks-test    # focused host-Git hooks and isolated formatter fixtures
just control-test
just bootstrap-test docker
just bootstrap-test podman
```

Native adapter gates use their pinned environments:

```sh
just javascript-test
just python-test
just rust-test
just swift-test
just gradle-test
just module go verify
```

Only run available platform lanes, and report omissions explicitly. Keep dangerous
Git/filesystem tests in disposable checkouts. Do not run qualification against a
consumer's migration worktree. Preserve application gates and platform-specific
adapters when migrating consumers.

## Implementation boundaries

Use the lowercase wordmark **chainman** in prose and headings, including at the
start of a sentence. Preserve the spelling of executable identifiers and verbatim
code examples, including `CHAINMAN_*` variables and the stable bootstrap recipe.

- `bootstrap/chainman.just` is the small consumer contract. Keep it readable and at
  most 25 nonblank shell lines. It only handles pinning, Git objects, and dispatch.
- `bootstrap/git-entry.sh` comes from the verified commit and materializes source.
- Runtime-owned execution, environment selection, services, and update staging
  belong in chainman's selected revision, never copied into consumer repositories.
- `scripts/git_runtime.py` provides verified Git source handling inside the runtime.
- The project owns configuration, flakes, task commands, adapters, and verification.

Production Python modules enter strict mypy automatically. Validate external data
at the boundary; do not add blanket typing exemptions. Changes to service or
transaction lifetime need failure and interruption coverage. An update must not
execute a candidate's mutable justfile to recover trusted host orchestration.

## Git fixtures and publication

Tests can substitute transport with disposable Git repositories. Do not add production archive overrides or a second installation
architecture for test convenience. Verify exact Git objects, checkout independence,
source modes, and restricted host prerequisites independently of higher-level tests.

Once a commit is public, a new or empty fixture can be generated without Git setup:

```sh
just example /absolute/path/to/new-fixture FULL_COMMIT_SHA
```

Replace `FULL_COMMIT_SHA` with the full selected commit. Use the normal initializer
for rolling default-branch adoption. There is no local release packaging step or distribution
inventory to update when adding runtime source files.

Commit and qualify the exact revision before publication. Use the publication workflow
and public readback described in [publishing](releasing.md). Consumer promotion and
remote replacement remain separate, guarded operations.
