# Explicit configuration composition

Schema 3 adds reusable declarations while retaining explicit task and service
names. Schema 1 and 2 consumers keep their existing behavior. They must opt into
schema 3 before using `templates` or `extends`.

```toml
schema = 3

[templates.tasks.check]
profile = "default"
setup = ["dependencies"]
timeout_seconds = 300

[tasks.unit]
extends = "check"
commands = [["npm", "test"]]

[tasks.lint]
extends = "check"
commands = [["npm", "run", "lint"]]
```

Templates are available under `templates.tasks`, `templates.services`,
`templates.setup`, and `templates.profiles`. Each declaration can name one parent
template of the same kind. Templates can themselves extend another template.
Inheritance cycles, unknown parents, unknown fields and invalid field types are
errors, including in unused templates. Templates can omit fields supplied by
their consumers; the expanded runnable declarations undergo normal validation.

Tables merge recursively. Scalars and arrays replace their inherited value;
arrays never concatenate implicitly. An empty array clears an inherited array.
An empty table does not delete inherited keys. Choose a narrower base template
when consumers need different mutually exclusive fields, such as command and
container services. There are no matrices, remote includes, executable templates,
multiple parents or new interpolation rules.

The configuration loader expands templates before task execution, setup
fingerprinting, service planning and update verification. A template change is a
configuration change and invalidates the existing profile/configuration hashes.
The expanded representation is derived at read time and is never checked in as
another editable configuration.

Schema-3 bootstrap routing and container transport also use this compiler from
the verified runtime. Container planning runs with a read-only project mount,
before applying project-declared mounts or executing project commands. The
legacy bootstrap projection remains available for schema-1/2 consumers and old
runtime archives; it does not implement template inheritance.

## Public inspection contract

Use the project's checked-in launcher:

```sh
scripts/chainman.sh config validate
scripts/chainman.sh config show --json
scripts/chainman.sh explain unit --json
```

These actions validate declarations without executing project commands or
starting project services. The normal immutable runtime bootstrap may still
realize its own packages and cache. Validation returns a nonzero status on errors.
All successful documents carry `schema: 1` for the JSON interface and a separate
`configuration_schema` for the consumer input language.

`config show` reports expanded configuration and per-field template origins.
`explain` selects the task dependency closure, required setup and service graph,
profile declarations, and service ownership defaults. It is a static explanation:
it does not resolve live endpoints, read environment-file contents or run probes.
Environment values are redacted, so these documents are inspection artifacts,
not executable configurations. Literal configured commands remain visible; do
not put credentials directly in command arguments.

Generators should emit TOML and invoke `config validate` through the pinned
launcher. They should not implement inheritance themselves. JSON clients must
check the interface schema, ignore additional object fields, and treat a schema
change as a compatibility boundary. Existing keys retain their meaning within
interface schema 1. New configuration syntax requires upgrading the pinned
runtime before a consumer adopts it.

## Consumer qualification

From Chainman's source checkout, run:

```sh
just consumer-check --release dist/release/chainman-release.json /path/to/consumer
```

The check validates effective declarations, the selected release identity,
bundled archive digest, bootstrap bytes/modes, and every declared runtime copy.
It never executes consumer workflows. `--baselines file.json` additionally
compares effective declarations with an explicit mapping from absolute
`chainman.toml` paths to parsed pre-migration configurations. Only the input
schema number is ignored in that comparison. Application acceptance tests and
actual launcher/platform qualification remain separate gates.

## Diagnostics and timings

`setup-status` retains its existing JSON fields and adds `details`: a reason for
each stale group and a `recovery` argument array to pass to the launcher. It
borrows existing locks without creating caches, refreshing usage timestamps,
installing dependencies or changing readiness records. A concurrent writer can
make the inspection unavailable; retry after that operation finishes.

Set `CHAINMAN_TIMING=1` to emit local JSON records prefixed `CHAINMAN_TIMING ` on
stderr. Records identify bootstrap, profile entry, command execution, setup
validation and service readiness. They contain no argv, environment values or
application output. Correlate Python start/end records by `operation`; unmatched
records indicate interrupted entry/execution and are not successful timings.
Go readiness records include their duration directly. Bootstrap uses portable
whole-second shell timestamps and reports its one-second resolution. Other
durations use monotonic clocks. Timing is disabled by default.

Bootstrap measurements include runtime realization and, for schema 3, trusted
planning. Record cold/warm cache conditions alongside measurements; do not sum
overlapping parent/child phases or treat unpaired records as completed work.
Normal application output continues to use its usual streams independently of
the timing records.

Recovery commands (`services-status` and `services-stop`) use saved ownership state.
They and their nested native-tool export bypass declaration compilation, so a
semantic error such as an unknown template parent cannot block service cleanup.
Ordinary task entry and configuration validation still reject that error.
