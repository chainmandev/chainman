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
