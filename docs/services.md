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
command = ["sh", "-eu", "-c", "exec python3 -m http.server 8000 --bind \"$PREVIEW_BIND\""]
environment = { PREVIEW_BIND = "{bind}" }
restart = "no"
shutdown_seconds = 5

[services.preview.transport]
ports = ["127.0.0.1:8000:8000"]

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

The command probe runs in the service's execution context. `{bind}` selects host
loopback in host-Nix and the container interface in container-Nix. The explicit
transport publishes port 8000 on host loopback in container mode; it does not
expose the host service to other machines.
For services exposing host loopback HTTP endpoints, the native `http_get` probe
avoids a shell/Nix entry per probe. It supports bounded body checks and headers
from declared environment variables, including Basic authentication. See the
[configuration reference](configuration.md#services) for port and credential rules.

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
Finite tasks and shells retain their ordinary output. A finite service-backed
task that fails during service acquisition also prints bounded startup diagnostics.

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

The human view includes shared repository services (such as a database), network
bridges, and recovery warnings under their own resource scopes. Concurrent status
inspection and development launches tolerate completed records being pruned.

Inspection never waits for a service-ownership lock or removes abandoned leases.
The native controller shares a two-second probe budget across the worktree and
its shared resources. During startup/shutdown it returns the available snapshot:
`complete=false`, `busy=true` for scopes with a held ownership lock, and
`inspection_errors` explaining unavailable observations. Unavailable fields are
`null`, not evidence that a service is healthy or stopped. Root `complete` also
reflects incomplete shared resources. Application preparation status remains
separate from service health. Retry status after the transition; recovery and
cleanup remain the responsibility of start/stop operations. Runtime bootstrap
and provisioning time are outside this native inspection budget.

When a service exits before readiness, the error identifies the service, backend
status, exit code when supplied, and log path. Finite tasks also show up to 16 KiB
per service scope from the end of this startup attempt; older log history is
excluded. Cancellation does not produce this failure diagnostic. Full retained
output remains available through `services-logs`; logs from concurrent clients of
a shared scope can overlap, so the excerpt is a time window, not exclusive
attribution to one client.

An observed failed watched build or lost service readiness makes the application
**degraded**, even if the last successful server remains available. A later
successful build/healthy observation clears that condition. Fatal service or
preparation failures end the operation. Summary mode prints up to 16 KiB of recent
service diagnostics from this operation; it never labels all stderr as errors or
claims to detect every application error. Foreground preparation failures stay in
the foreground output. Saved status is observational and cannot start, stop or
lease services. A future viewer can use it without taking execution authority.

`CHAINMAN_DEV_CHANNEL`, `CHAINMAN_DEV_OPERATION` and `CHAINMAN_DEV_TASK` are internal
runtime coordination variables. Users configure only `CHAINMAN_DEV_OUTPUT` and
the task's presentation metadata. The private channel carries bounded progress
records and mounts no controller state or service-control socket into workloads.

Owned foreground commands receive the controlling terminal for their lifetime;
the caller restores its foreground group and terminal settings afterward. Ctrl-C
cancels the task and releases its services, retaining resources used by another
client and preserving persistent volumes. Piped input remains a pipe. Background
services and readiness probes never acquire the caller's terminal.
Planning and setup-admission phases receive empty stdin so container clients
cannot consume the application's input early. Setup consent still uses its
separate controlling-terminal channel; foreground application commands retain
the caller's stdin.

The verified Git entrypoint forwards INT, TERM and HUP to its owned runtime and
waits for bounded cleanup before removing its temporary export. Automation should
retain the invocation and wait for it to exit after cancellation. If an external
runner forcibly kills processes or disconnects from a container engine, inspect
`services-status` and use `services-stop` to release the repository's owned work;
an outer process exit alone is not proof that all work has stopped.
For programmatic cancellation of `just`, send TERM and wait, or signal a process
group owned by the caller. Do not rely on HUP sent only to the outer `just` PID:
Just can leave its recipe running without forwarding that signal. Terminal Ctrl-C
and hangup target foreground process groups; they are different from signalling
only the command runner.

Shell entry preserves inherited operation/service lease descriptors. It needs
one unused descriptor from 3 through 9 to preserve stdin across portable POSIX
background execution. If all seven are occupied, entry fails before launching
the command; close unused inherited descriptors or start from a fresh host shell.
It never closes a caller's lease to make room.

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
