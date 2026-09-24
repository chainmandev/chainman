# Development services

[Guide index](README.md) · [Configuration reference](configuration.md#services) · [Composition](composition.md)

chainman starts only the services requested by a task, waits for readiness, and
releases that task's ownership when it finishes. Shared services can remain alive
while another task owns them. Process Compose handles readiness, supervision, and
restart behavior; chainman's native controller tracks clients, leases, and crash
recovery. Neither requires host Python or Go.

## A small local server

Add these declarations to a schema-3 project with a `core` profile:

```toml
[services.preview]
profile = "core"
command = ["python3", "-m", "http.server", "8000", "--bind", "127.0.0.1"]
restart = "no"
shutdown_seconds = 5

[services.preview.readiness]
command = ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000', timeout=2).close()"]
period_seconds = 1
timeout_seconds = 3
failure_threshold = 30

[tasks.preview]
services = ["preview"]
wait_for_services = true

[tasks.preview.presentation]
title = "Local preview"
urls = { Browser = "http://localhost:8000" }
```

Run the task through the project entrypoint:

```sh
just chainman run preview
```

From another terminal in the project:

```sh
just chainman services-status --human
just chainman services-logs --follow
just chainman services-stop
```

The command probe runs in the service's execution context. In container mode,
publishing a browser-accessible port requires the project's explicit container
port configuration; host loopback and container loopback are different networks.
For services exposing host loopback HTTP endpoints, the native `http_get` probe
avoids a shell/Nix entry per probe. See the reference for namespace and port rules.

## Application ownership

Keep commands in the foreground. Declare service `depends_on` and `setup` groups
for ordering and installation leases, and put readiness checks on the actual
service interface. Do not hide an untracked background daemon in setup.

A finite acceptance task names its services and commands. It succeeds only when
the commands succeed and required services remain available. A development task
uses `wait_for_services = true` to retain ownership until stop or interruption.
Inspection and stop use saved ownership state even if the current configuration
is invalid. Stop also cancels finite tasks using those services.

### Development display and readiness

Tasks with `wait_for_services = true` show their declared URLs when service startup
begins, marked **starting**, then **preparing** while their commands run. Only the
verified workflow's successful completion of preparation reports **ready**. An
HTTP response alone does not establish application readiness. Setup and any
project-owned preflight wrapper still run before this operation is admitted.

`CHAINMAN_DEV_OUTPUT=auto` (the default) selects a concise summary when stdin and
stderr are terminals, and streams service logs otherwise. Summary mode prints
lifecycle transitions, startup/preparation reminders every 15 seconds, and the
ready summary. Routine service logs remain collected. Preparation commands keep
their ordinary stdin/stdout/stderr; chainman does not capture or silence them.
Finite tasks and shells are unchanged.

```sh
CHAINMAN_DEV_OUTPUT=summary just chainman run preview
CHAINMAN_DEV_OUTPUT=logs just chainman run preview
# In a second terminal:
just chainman services-status --human
just chainman services-status
just chainman services-logs --follow
```

Status JSON retains the service fields and adds `applications`: distinct operation
IDs, task presentation, owner identity, phase, timestamps, elapsed seconds, outcome,
and `reached_ready` (a historical fact, not current health). Active phases are
starting, preparing, ready, degraded and stopping; final phases are stopped or
failed. Dead owners and explicit cancellation never remain currently ready.
Records are scoped by worktree and execution mode, separate from shared-resource
ownership. Up to 20 completed records are kept when a new operation starts,
alongside active clients.

An observed failed watched build or lost service readiness makes the application
**degraded**, even if the last successful server remains available. A later
successful build/healthy observation clears that condition. Fatal service or
preparation failures end the operation. Summary mode prints up to 16 KiB of recent
service diagnostics from this operation; it never labels all stderr as errors or
claims to detect every application error. Foreground preparation failures stay in
the foreground output. Saved status is observational and cannot start, stop or
lease services. A future viewer can use it without taking execution authority.

Owned foreground commands receive the controlling terminal for their lifetime;
the caller restores its foreground group and terminal settings afterward. Ctrl-C
cancels the task and releases its services, retaining resources used by another
client and preserving persistent volumes. Piped input remains a pipe. Background
services and readiness probes never acquire the caller's terminal.

New service logs identify `stdout` and `stderr` explicitly. These are output
streams, not severity levels: PostgreSQL and compilers write ordinary progress
to stderr. Application warnings and errors retain their original text. Older
saved logs keep their original presentation.

`services-logs` shows the latest
64 KiB per log; `--follow` continues across log rotation and truncation. It uses
saved worktree/mode state and its associated repository resources, even when the
current configuration is broken. A log viewer acquires no service lease: Ctrl-C
detaches the viewer without stopping services. Logs can contain application output
and secrets; treat them as local development data.

For a standalone development task that owns its server and child watchers, declare
`cleanup_children = true` and publish its port explicitly:

```toml
[environment]
pass = ["PREVIEW_PORT"]

[environment.defaults]
PREVIEW_PORT = "4321"

[tasks.preview]
profile = "core"
commands = [["sh", "-c", "exec python3 -m http.server \"$PREVIEW_PORT\" --bind 0.0.0.0"]]
cleanup_children = true
transport = { ports = ["127.0.0.1:{env:PREVIEW_PORT}:{env:PREVIEW_PORT}"] }
```

Port references use effective project/profile/task environment values. Each must
be a decimal port from 1 through 65535. Container publication always binds host
loopback; caller overrides must appear in `environment.pass`. Stop a standalone task with Ctrl-C; `services-stop` addresses declared
service graphs.

Graceful stop sends one initial termination signal to the service, allowing its
shutdown handler to close resources within `shutdown_seconds`. Remaining
foreground descendants are terminated after that grace period. Services must
clean up children that create independent sessions; keep their main supervisor
in the foreground.

Projects own database reset/migration semantics, application credentials, adapters,
and specialized cleanup. A shared lifecycle manager cannot decide whether a
database is safe to reset. Declare narrow mounts and forwarded environment names;
project containers do not receive the engine socket or controller state.

For sequential database acceptance lanes, bind `recipes.verify` and
`updates.verify_tasks` to the same ordered task list. Each lane releases its
service claims before the next starts. Task dependencies have different semantics:
they form a shared dependency graph. See [recipes](recipes.md) for examples.
