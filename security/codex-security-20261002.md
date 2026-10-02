# Codex Security review — 2026-10-02

## Summary

Codex Security reviewed revision `a3d0099e434edb3dcf2f869e9e21be3c5d0ee853`
across the repository's runtime bootstrap, workflow execution, container and
service orchestration, network artifact handling, update transactions,
filesystem state, and supporting tests and documentation.

The review found one reportable vulnerability:

| Severity | Confidence | Finding |
| --- | --- | --- |
| Medium | High | A container service image can be interpreted as Docker or Podman options |

The scan was static. It used one non-destructive declaration-validation probe
through the pinned `just exec` environment and inspected the local Docker CLI
syntax. It did not start a workload container, mount a host path, or run an
external migration worktree. Independent delegated reviewers were unavailable,
so the recorded scan coverage is partial despite reviewing all eight planned
security surfaces.

## Finding: service image argument injection

- Rule: `argument-injection.container-service-image`
- Taxonomy: CWE-88 (argument injection)

Affected code:

- [`scripts/services.py:365`](../scripts/services.py#L365-L368) accepts any
  non-whitespace, non-`@` prefix before an immutable SHA-256 suffix.
- [`scripts/services.py:870`](../scripts/services.py#L870-L873) appends that
  value directly to the container-engine argument vector before the declared
  service command.
- [`nix/control/main.go:237`](../nix/control/main.go#L237-L247) executes the
  exported vector without adding an image boundary or revalidating it.

### Impact

A project-controlled `services.<name>.container.image` value may begin with a
Docker or Podman option. For example, an option-shaped value can encode a bind
mount while retaining the required `@sha256:<digest>` suffix in the mount
destination. The first `container.command` element then occupies the engine's
actual image position.

When an operator starts a task that selects the service, the host controller
passes the crafted vector to Docker or Podman. This bypasses Chainman's normal
container-option parser, including its rejection of blanket root, home, and
engine-socket mounts. A rootful engine running an image as root could expose or
modify nearly any host file through a read-write root bind. Rootless engines or
non-root image users reduce the accessible host files.

### Root cause

The image validation checks only this shape:

```python
r"[^\s@]+@sha256:[a-f0-9]{64}"
```

It does not validate an OCI image reference or reject a leading `-`. The planner
then constructs one flat argument vector:

```python
argv += [
    text(item["image"], "Container image"),
    *strings(item.get("command", []), "Container command"),
]
```

Docker and Podman parse option-looking arguments before `IMAGE` as engine
options. Chainman does not establish an end-of-options boundary before the
repository-controlled value.

### Validation

A disposable probe in the pinned development environment confirmed that
`services.declarations()` accepts an image value shaped as:

```text
--mount=type=bind,src=/,dst=/host@sha256:<64 lowercase hexadecimal characters>
```

Static source tracing confirmed that the value is preserved through plan export
and host-controller execution. The local Docker client reports the relevant
grammar as `docker run [OPTIONS] IMAGE [COMMAND] [ARG...]`.

No live exploit was executed. Fixed `--cap-drop ALL` and
`no-new-privileges` controls remain present, but they do not prevent ordinary
filesystem access through an admitted bind mount.

### Severity rationale

The impact can be high because the resulting service may receive arbitrary host
paths. Overall severity is medium because exploitation requires a repository
configuration change and an operator to start the affected service. Chainman
also documents that container mode limits accidental access but is not a
hostile-code sandbox.

The severity should be raised if projects routinely execute service
configuration from untrusted changes or treat container-Nix as a host
confidentiality boundary. It may be lowered where service declarations are
restricted to trusted maintainers and independently validated.

### Remediation

Validate `container.image` as a canonical OCI image reference that cannot begin
with `-`. Also establish an engine-compatible image boundary, or use an
equivalent typed command representation that prevents repository-controlled
strings from occupying the engine option region.

Recommended regression coverage:

1. Reject service image values beginning with `-`, even when they end in a
   valid SHA-256 digest.
2. Export an adversarial service plan and assert that no repository-controlled
   token can occupy the engine option region.
3. Retain a positive test for canonical registry/repository image references
   pinned by digest.

## Reviewed surfaces

| Surface | Result |
| --- | --- |
| Pinned Git bootstrap and immutable runtime | No separate issue found |
| Container parsing, mounts, environment, ports, and engine authority | Finding reported above |
| Service controller, labels, leases, probes, and volumes | No separate issue found |
| Configuration, tasks, commands, profiles, and hooks | No separate issue found |
| Registries, redirects, credentials, artifacts, and archives | No separate issue found |
| Dependency resolution and isolated update application | No separate issue found |
| Path containment, private state, cleanup, caches, and symlinks | No separate issue found |
| Tests, documentation, templates, examples, and CI | No separate issue found |

"No separate issue found" means the static review did not validate another
reportable vulnerability; it is not a guarantee that the surface is
vulnerability-free.

## Remediation follow-up

The historical finding above describes revision
`a3d0099e434edb3dcf2f869e9e21be3c5d0ee853`. The follow-up patch changes the shared
service-declaration validator to reject image values beginning with `-`, after
the existing digest check. Plan export now inserts `--` immediately before the
image. This establishes the invariant that a project-controlled image cannot
occupy the container engine's option region.

The fix deliberately retains the existing digest-pinned reference grammar and
its error behavior instead of adding a new OCI parser. Short names, registry
ports and paths, tags with digests, and bracketed IPv6 prefixes retain their
existing treatment; final image-reference interpretation remains the engine's
responsibility. Service-command arguments remain literal, including leading
dashes, a standalone `--`, spaces, and empty strings.

Changed implementation and coverage:

- `scripts/services.py`: shared declaration validation and exported image boundary.
- `tests/test_service_export.py`: malformed-digest checks, short/long option
  rejection in both scopes, positive image-reference controls, and exported-plan
  checks across host-Nix/container-Nix, worktree/repository scopes, and Docker/Podman
  executable selections. Fixtures never execute the engine.
- `docs/configuration.md`: documents the image and service-command contract.

The export regressions failed against the original implementation: it accepted
the malicious image and omitted the image boundary. They pass with the patch.
An independent read-only investigator and a separate candidate reviewer traced
the declaration, export, native execution, network-borrowing, and saved-plan
paths. Neither found a surviving route through patched public entry points or
a concrete compatibility regression.

Existing installed runtimes and already running services are not modified in
place. Newly selected runtime paths participate in service fingerprints; callers
must select the patched runtime to receive the fix. Saved plans are not
retroactively sanitized. This patch does not turn container execution into a
hostile-code sandbox.

### Verification outcome

Outcome: **blocked at full native qualification**. The patch is implemented and
the original injection no longer reproduces, but it is not classified as fully
verified while `just control-test` remains unsuccessful. No native Go source was
changed, and no unrelated signal-handling fix is included.

Ordered checks on Linux/aarch64:

| Gate | Command / check | Result |
| --- | --- | --- |
| Diff and syntax/style | `git diff --check`; `just exec ruff --isolated format --no-cache -- scripts/services.py tests/test_service_export.py` | Passed; focused imports also exercised by tests |
| Original trigger before patch | `just exec python3 -B -m unittest discover -s tests -p test_service_export.py -v` | Expected failure: malicious images accepted and image boundary absent; positive controls passed |
| Trigger, alternate forms, legitimate controls, neighboring tests | `CHAINMAN_SETUP=auto just exec python3 -B -m unittest discover -s tests -p 'test_service*.py' -v` | Passed: 91 tests, 78 native opt-in skips |
| Required source gate | `CHAINMAN_SETUP=auto just verify` | Passed: formatting/lint, strict Linux and Darwin typing, generated-file checks, 2 example tests, 1,305 repository tests (194 skips), and example build |
| Native network path | `just exec-in control go -C nix/control test -race -mod=readonly -count=1 -v -run '^TestNetworkBorrowing' ./...` | Passed both tests |
| Native signal diagnostic | `just exec-in control go -C nix/control test -race -mod=readonly -count=1 -v -run '^TestPendingSignalVetoesSpawnedHelper$' ./...` | Passed, including interrupt, termination, and hangup subtests |
| Full Go diagnostic | `just exec-in control go -C nix/control test -race -mod=readonly -count=1 -v ./...` | Passed; does not replace the native qualification command |
| Full native gate | `CHAINMAN_SETUP=auto just control-test` | Failed three attempts, including a sequential retry: the Go test process exited with `signal: interrupt` or `signal: terminated` |

The full native gate passed its formatting and vet steps, but stopped at
`go test -race -mod=readonly ./...`; native hook/service integration tests and
cross-builds in that command were not reached. The isolated and verbose Go runs
did not reproduce that termination. Its cause remains unresolved, so those
passes do not erase the failed qualification attempts.

The first focused-suite and plain `just verify` invocations stopped because
setup was not authorized or its receipt was stale. Explicit `CHAINMAN_SETUP=auto`
allowed the declared setup and disposable test fixtures, after which both
passed. A diagnostic attempted while verification held the managed-operation
lock was refused; diagnostics above were subsequently run sequentially.

No live Docker/Podman workload, host mount, or external migration worktree was
used. Engine-name combinations test exported vectors, not live engine behavior.
Darwin execution, other CPU architectures, and the remaining opt-in platform or
language-adapter lanes were not qualified by this follow-up.
