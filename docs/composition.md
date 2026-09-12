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
