# Schema-3 configuration reference

[Guide index](README.md) · [Getting started](getting-started.md) · [Troubleshooting](troubleshooting.md)

A consumer checks in `chainman.toml`, a plain Git SHA in `chainman.lock`,
and the small bootstrap recipe in its existing justfile. **New projects use schema 3.** Profiles select the
environment, setup groups declare frozen installation and readiness, tasks run
commands and acquire services, and recipe bindings expose the public commands.

```toml
schema = 3

[project]
default_profile = "default"

[profiles.default]
flake = ".#default" # Select the project-owned shell.

[tasks.check]
commands = [["python3", "-m", "unittest", "discover", "-s", "tests"]]

[recipes]
verify = ["check"]
verify-lite = ["check"]

[updates]
minimum_age_days = 30
verify_task = "check"
```

Use [manual adoption](adoption.md) to add the pin and bootstrap recipe.
Run `just chainman config validate`, `just chainman config show --json`, or
`just chainman explain check --json` to inspect declarations before executing them.
Schema 3 also supports [configuration modules and reusable templates](composition.md).
Use an explicit root-level `include = ["chainman/tasks.toml", "chainman/setup.toml"]`
to split a growing configuration. Paths remain project-relative; duplicate settings
are errors, and included files participate in readiness and update protection.

## Reference map

- [Profiles and environment](#profiles-arguments-and-environment)
- [Legacy schema-1 compatibility](legacy-configuration.md)
- [Named setup groups and tasks](#named-setup-groups-and-tasks)
- [Services](#services), including readiness and lifecycle ownership
- [Recipe bindings](recipes.md) and [dependency updates](updates.md), including
  [temporary security exceptions and automatic cleanup](updates.md#temporary-security-exceptions)

## Profiles, arguments, and environment

Commands are arrays of argument arrays. `exec --profile NAME -- ARGS...` preserves
literal arguments; shell expansion happens only in an explicitly chosen shell.

`deps-query` also accepts `{"schema":1,"operation":"batch","requests":[...]}`.
Each entry is a normal schema-1 query. The response contains `schema`, `operation`
and an ordered `results` array. A batch uses one eligibility time and credential
context; any failed query fails the whole response. Batches contain 1–128 entries,
cannot nest, and retain the existing 4 MiB input bound and per-query network bounds.
`run NAME` invokes a declared task (or a legacy schema-1 command). `command_profiles.NAME` overrides the project
default for that command. `setup` uses manifest/toolchain fingerprints and declared
artifacts and optional readiness commands. A profile selects a project-relative `path#shell` (also `flake.nix#shell`)
or a built-in `runtime_profile`. Built-ins are core, javascript, rust, python, go,
flutter, swift, compose and browser. Without a named project override, default maps
to core. The special `host` profile runs directly in the already bootstrapped core
context; it is useful for an adapter that subsequently selects the project shell.
It does not provision host language tools.

### Public entry readiness and execution requirements

Profiles may declare `entry_setup = ["javascript"]`, naming existing setup groups.
`exec`, `shell`, and `script` check that closure using `CHAINMAN_SETUP` and retain
its setup-use locks until the command exits. Ordinary tasks use their own explicit
`setup` lists; installers do not recursively require their profile's entry setup.
Omitting `entry_setup` (or declaring `[]`) provides setup-free environment entry,
useful for a deliberately named inspection profile. Explicit `setup` remains exhaustive.

Profiles and tasks may declare `allowed_modes = ["host-nix", "container-nix"]`
and `allowed_platforms = ["Linux", "Darwin"]`. Each supplied list must be nonempty
and contain unique supported values. Mode choices are `host`, `host-nix`, and
`container-nix`; platform choices describe the initiating host, including when its
commands run inside a Linux container. Both task and profile restrictions apply.
Omitting a restriction adds no constraint; bare-host limitations still apply.

Use `just chainman preflight TASK [TASK ...]` before a multiphase wrapper's first
command. It checks all selected task dependencies, service/watch dependencies,
and required setup profiles without installing dependencies or running project
commands. It does not reserve services or guarantee future readiness; execution
checks again. Preflight leaves command stdin untouched. A sequential standard
recipe also checks all its phases before starting its first phase.

```sh
just chainman preflight build check
just chainman run build
just chainman run check
```

These declarations prevent accidental incompatible execution; they are not a
security sandbox for project-owned commands.

Declare imported Nix modules and toolchain pins as profile `inputs`. These are
project-relative file/glob patterns, inherited through profile templates:

```toml
[profiles.default]
flake = "nix/devshell#default"
inputs = ["nix/devshell/*.nix", "nix/devshell/*.toml"]
```

The flake, its lock, project configuration, and chainman pin are included
automatically. File names and contents determine identity, so additions and
removals also invalidate setup and active-profile reuse. Symlinks and paths
outside the project are rejected. Avoid broad globs that include application
source or build outputs. Bare-host mode does not depend on Nix input files.

For `deps-query` with `provider="swift"` and `operation="metadata"`, supply an exact
stable version such as `1.0.0`. chainman reads the bounded release inventory, then
resolves tag and commit-time evidence only for that version, including a matching
`v1.0.0` tag. The response retains the raw tag identity and the later release or
commit publication time; metadata does not claim eligibility. Full version
selection and final artifact audits still require their complete evidence.

GitHub registry metadata can use an explicitly supplied `GITHUB_TOKEN` environment
variable. Absent or empty keeps anonymous requests. Supply it through the caller's
secret environment, never a token literal in project configuration, URLs or command
arguments. chainman does not discover credentials from `gh`, `.netrc`, Git helpers
or credential files. Tokens must be at most 4096 ASCII bearer-token characters
(letters, digits, `-._~+/`, with optional trailing `=`); malformed values fail
without being echoed.

The first registry request fixes the credential context for that command. Changing
`GITHUB_TOKEN` afterward fails before either cached evidence or another request can
be used, including after clearing the response cache. Start a new command to change
credentials. Authorization is attached only to `https://api.github.com`, with no
userinfo and the default HTTPS port. Every authenticated redirect is rejected before
it can dispatch a successor request. Other origins retain anonymous transport;
authentication errors never fall back to anonymous requests. Existing request,
retry, maturity and immutable-source audit bounds remain in force. Authentication
does not guarantee quota availability.

Public chainman asset bytes are requested anonymously from the outset, including
when metadata uses a token. GitHub can redirect those downloads to its asset host;
no credential accompanies either request. Asset IDs, sizes and digests still bind
the downloaded bytes to the validated release. Authenticated metadata requests
retain their no-redirect rule; an authentication failure is never retried anonymously.

Host mode inherits the explicitly supplied variable. Container callers can select
`environment.pass = ["GITHUB_TOKEN"]`; the existing forwarding passes its name,
without putting its value in arguments. This option covers chainman's Python
registry metadata requests. Native Git, Swift and Nix downloads keep their own
credential behavior; this does not qualify general private-repository support.

Service planning keeps project environment files, defaults, values and task context
as data. They are applied inside the selected execution lane, including for setup,
services, probes and watched builds. They do not override the host launcher's or
container engine's environment. Only explicitly forwarded original host inputs
cross that boundary; `source_env` mount declarations select host values.

The optional-module example uses `modules = ["core", "rust"]` and individual files
under `modules/`. Each declares directory, input globs, readiness artifacts, commands
and update outputs. A project may keep its own modules and flake; runtime updates
never replace those application-owned files.

The JavaScript and browser shells bind pnpm to the same Nix Node executable used
by project commands; JavaScript's Prettier wrapper uses it too. Merely putting Node
first in `PATH` does not change an executable's pinned interpreter. The JavaScript
example sets `engineStrict: true` so an incompatible package-manager runtime fails
setup instead of producing a warning that successful builds can hide.

`environment.values` and profile `environment` map names to literal strings; `{root}`
expands to the selected project directory. `environment.unset` removes named inherited
or default cache variables after those values are applied, for example a legacy
Cargo layout that requires no `CARGO_TARGET_DIR`. Managed runtime/lock/cache-server
variables cannot be unset. `cache.preserve_environment` preserves specifically named
caller cache settings. `profiles.NAME.compiler_cache=true` opts a Rust-capable custom
profile into the managed foreground sccache lifecycle; the profile must supply sccache.

Compiler-cache profiles retain the realized Nix environment through a temporary
standard Nix profile for the complete compiler lifetime. The shared native task
owner contains the foreground cache server and its Nix launcher. Startup and stop
failures terminate that owned group; cleanup still requires released kernel leases
and the original socket identity before removing an endpoint. A failed cache stop
remains an error even when bounded termination succeeds.
The stop request calls the executable resolved during preflight directly, so
shutdown does not require another Nix evaluation or wait behind garbage collection.

If a subprocess wrapper closes inherited operation descriptors while retaining
their environment variables, the next command obtains a new operation lease.
Ordinary commands can coexist with their parent; an exclusive update or cleanup
still refuses to run while independent operations are active. Such a wrapper cannot
borrow its parent's exclusive admission through environment variables alone.

An explicit `TMPDIR` remains the temporary base across bootstrap and profile
refreshes, including project/profile overrides. Without one, chainman retains
Nix's first scoped temporary directory. This prevents repeated shell entries from
exceeding browser socket path limits. `CHAINMAN_TEMP_BASE` is internal routing;
configure `TMPDIR` instead. Container mode selects its base inside the container;
host temporary directories are not implicitly forwarded or mounted.

pnpm uses `PNPM_CONFIG_STORE_DIR` for the shared download store. Explicitly preserved
or configured `PNPM_STORE_DIR` and `npm_config_store_dir` values remain supported as
adapter aliases: each environment layer synchronizes all three names. If one layer
provides conflicting aliases, the canonical name takes precedence, followed by
`PNPM_STORE_DIR`. Project values override preserved caller values, and profile values
override project values, as with other environment settings.

Container configuration uses `environment.pass = ["CI", "DEMO_*"]` for selected
variable names and `container.mounts = [{source="relative/cache", target="/cache",
read_only=false}]` for explicit project/SDK/service paths. `container.ports` lists
publish specifications. The normal mount set is the project, the linked-worktree Git
administrative paths where needed, and named Nix/download volumes. Relative sources
resolve from the project; home/root/socket blanket mounts are rejected.

A mount can use `source_env="SDK_DIRECTORY"` instead of `source`. The verified runtime reads that explicitly named host variable as a literal path; it never
executes it or discovers a fallback. If `target` is omitted, the same absolute path
is visible in the container. Declare `environment.pass=["SDK_DIRECTORY"]` when
commands also need its value. Unset or empty variables, socket sources, blanket
host mounts, and bootstrap-shadowing targets fail before project execution.
Read-only remains the default. This supports opt-in SDK or Xauthority mounts
without host Python or project-specific mount scripts; declare them only on the
tasks that need access. Host-Nix execution does not add container mounts.

Dynamic adapters can set `CHAINMAN_FORWARD_ENV` to comma-separated names or narrow
patterns. `CHAINMAN_CONTAINER_OPTIONS_FILE` names a regular file of literal option
and value lines, produced from an argument array rather than a shell command string.
Supported pairs are publish, bind mount/volume, add-host, hostname, label, name,
network (`host` or `bridge`), and platform (`linux/amd64` or `linux/arm64`). Use explicit
platforms only with a working engine/emulation/native builder for that architecture.
Options never authorize privileged mode, Docker socket access or another container's
network namespace. The project adapter owns optional credential/native mounts and
must obtain the same trust/authorization it needed before adopting chainman.
Whole container HOME replacement requires a project-contained directory. Explicit
HOME subdirectory mounts remain available for those credential/SDK adapters; mounts
over HOME or project ancestors are rejected.

Execution controls are `CHAINMAN_MODE` (`container-nix` by default, `host-nix`,
or the discouraged caller-maintained `host` mode), `CHAINMAN_CONTAINER_ENGINE`
(`docker` or `podman`), and `CHAINMAN_NIX_BIN` (an explicit absolute executable
for host Nix). See [execution modes](runtime.md) for capabilities and prerequisites.
`CHAINMAN_SETUP=prompt|auto|error` controls repair before ordinary commands;
`prompt` is the default. Explicit setup always authorizes installation.
`CHAINMAN_DEV_OUTPUT=auto|summary|logs` controls the display of service-backed
development tasks. `auto` uses summary output on an interactive terminal and
streams service logs otherwise. See [development status](services.md#development-display-and-readiness).
Runtime routing variables such as `CHAINMAN_PROJECT_ROOT` are internal; the
consumer recipe selects its project. Paths with spaces and invocation from another working
directory are supported. Newlines and ambiguous container comma-paths are rejected.

## Named setup groups and tasks

Schemas 2 and 3 support named setup groups and tasks. Inputs and artifacts are relative to the project;
`directory` changes only the command working directory. For example:

```toml
schema = 3
[project]
default_profile = "default"
[profiles.default]
flake = "nix#default"
[setup.javascript]
inputs = ["package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "patches/**"]
artifacts = ["node_modules/.pnpm/lock.yaml"]
commands = [["pnpm", "install", "--frozen-lockfile", "--config.confirmModulesPurge=false"]]
readiness = { command = ["pnpm", "exec", "node", "-e", ""], timeout_seconds = 30 }
[tasks.build]
setup = ["javascript"]
commands = [["pnpm", "run", "build"]]
[tasks.test]
depends_on = ["build"]
setup = ["javascript"]
commands = [["pnpm", "test"]]
```

`run test` executes dependency tasks once, then the requested task. Extra arguments
are appended literally to the final command of the requested task. `setup javascript`
ensures one group and its dependencies; `setup` validates all declared groups,
repairs missing or stale installations, and verifies readiness after repair.
The standard `just setup` recipe does this before project setup tasks. Setup groups also support
`depends_on` and `profile`. Task and setup dependency cycles or unknown references
fail before execution. Tasks request setup explicitly; inspection tasks can omit it.


Readiness commands run in the group's profile and working directory, with no stdin.
They must not install dependencies or edit tracked files. A check may refresh its
package manager's validation metadata. `setup-status` uses the same checks but
never installs or records a chainman success stamp. The default timeout is 30
seconds after entering the profile; `timeout_seconds` accepts integers from 1
through 300. Initial Nix profile provisioning can take additional time. Failure diagnostics
name the group and include bounded command output. A failed post-install check
invalidates the old stamp and prevents tasks or services from starting.

`setup-status` reports `changed_inputs` with `added`, `missing`, and `changed`
paths, labelled `setup:` or `profile:`, and the targeted recovery arguments.
The stamp contains file digests, never environment values. Changes to declarations,
dependency groups, environment inputs, or runtime identity can still receive a
general diagnostic; older stamps gain detailed inventories after the next repair.

The pnpm check above asks pnpm itself to validate the installation before an empty
Node command. chainman keeps `verifyDepsBeforeRun=error`: unchanged patch bytes
with a newer timestamp can require a frozen reinstall. Do not disable that check.
Include every workspace manifest and patch file in the group's inputs.
Input patterns ending in `/**` include files directly inside that directory and
all nested directories, on every supported Python version. Exclusions apply to
the resulting file paths; changes, additions, and removals invalidate readiness.

Ordinary commands ask once before repairing their required setup groups, using the
foreground controlling terminal independently of piped stdin. Interactive containers
use their own terminal. Piped commands, managed hooks, and credential-free setup
preflight use a runtime-owned host helper. Private regular-file messages carry
consent across the container boundary; no Unix socket or FIFO must cross a
container VM. Missing or unresponsive participants refuse consent.
Decline, EOF, or no foreground terminal aborts with the exact recovery command. Explicit `just setup`
does not prompt. For CI, run setup first or explicitly opt into automatic repair:

```sh
just setup
just verify
# Alternatively, authorize setup for this unattended operation:
CHAINMAN_SETUP=auto just verify
```

`CHAINMAN_SETUP=error` always refuses implicit repair. Already-ready commands do not
prompt. Verified update and formatting transactions authorize setup in their
isolated candidates. Installation retains exclusive artifact ownership; consent
does not override another task's lease.

A task can declare `context_environment = { APP_WORKERS = "false" }` for values
shared by its setup groups, dependency tasks, services, readiness probes, and watch
builds. These values replace caller inputs; explicit project values and per-profile,
service, or command overrides retain their normal precedence. Ordinary task
`environment` still applies only to that task's command. A dependency closure with
conflicting context declarations is rejected before setup or service acquisition.
Context values use the same literal expansion and reserved-variable checks as other
environment declarations. They participate in setup fingerprints when declared as
`environment_inputs`, and in service reuse compatibility. Container entries forward
only the names explicitly declared by the project. The trusted controller planner
never installs those values in its own process environment.

An artifact can be declared as `{ path = "build/variant.hash", digest = true }` to
check its bytes as well as its existence. Setup records its SHA-256 after successful
generation. A later change invalidates readiness even when source inputs are
unchanged, for example when another build variant has replaced generated assets.
If another task holds the setup lease, regeneration is refused until it finishes.

Finite tasks can opt into owned child cleanup and a deadline:

```toml
[tasks.desktop-e2e]
setup = ["javascript"]
commands = [["./scripts/desktop-e2e.sh"]]
cleanup_children = true
timeout_seconds = 600
shutdown_seconds = 10
```

The timeout applies to that task's complete command sequence after its profile has
been realized. Dependency tasks have their own declared lifetimes. Commands run
in order; the first failure stops the sequence and retains its exit status.
A timeout implies child cleanup and returns 124. Cleanup also runs after success
or cancellation, with a bounded grace period before terminating remaining members
of the owned process group. Commands must not detach into another session.
Setup and operation leases survive the extra process boundary and remain held by
the command's identity anchor if its caller dies.

An optional `timeout_env = "APP_TEST_TIMEOUT_SECONDS"` selects an explicit caller
override for that task's deadline. It resolves through the same environment
precedence as the command and requires an integer from 1 to 86400. An unset
variable retains the declared `timeout_seconds`; malformed overrides fail before
the task starts. Declare the variable in `environment.pass` for container callers.

This option materializes only the native ownership helper in the selected host or
container environment. It does not start Process Compose, require a host engine
adapter, or fetch the service/watch backend binaries. Ordinary finite tasks omit
the option and keep the existing lightweight path.

For a Python virtual environment, declare an artifact such as
`{path=".venv/bin/python", interpreter="python"}`. Its final component may be a
symlink, while all parent directories remain confined to the project. Readiness
requires it to resolve to the selected pinned `UV_PYTHON` in Nix modes; an
interpreter from a different environment is stale. In bare-host mode, select an
absolute executable path with `UV_PYTHON`, or use the caller's `python3` on PATH.
chainman passes that selection to the group's installer and checks it afterward.
Project/profile environment values apply to this selection. Invalid selections
fail before any requested setup group installs. No Python is downloaded or
provisioned by chainman; supply a version compatible with the project's own
requirements. Ordinary and digest artifacts still reject links.

Installed artifacts have shared use leases for task lifetimes. Reinstallation takes
exclusive access and fails visibly while another task uses them. Child commands
inherit those leases. A setup group may list explicit `environment_inputs`, such
as `["DEV_SEED_SUFFIX"]`, when its outputs depend on environment values. chainman
hashes their effective project/profile values (distinguishing unset from empty),
propagates changes through dependent setup groups, and stores only the digest.
Declare host-provided variables in `environment.pass` for container parity. Inputs
must come from the declared environment, rather than changes made by shell hooks.
Missing outputs or changed fingerprints require setup again;
failed installation or inputs changed during installation never receive a fresh
stamp. Setup commands should install from frozen inputs, with generation declared
separately as project tasks.
If generation changes another setup group's inputs, put the next phase in a
separate task entry in `recipes.generate`. The recipe runs each entry with its
own setup lease, allowing the next phase to repair derived artifacts. A single
task graph holds its setup leases throughout, including nested commands; it
cannot reinstall its own artifacts midway through execution.

An optional project or profile `resources` table controls build-job hints:

```toml
[resources]
job_variables = ["CARGO_BUILD_JOBS"]
max_jobs = 4
memory_per_job_gib = 3
```

Profile values override project values. Existing explicit positive job counts are
preserved. Otherwise the budget is bounded by detected CPUs, configured maximum
and memory per job; it is always at least one. Linux considers available memory,
affinity and cgroup-v2 ancestor limits, with v1 memory-limit support. macOS uses
available CPU count and physical memory. These are concurrency hints rather than
memory isolation. Workflows without a resource declaration perform no resource probe.

## Services

Service workflows in schemas 2 and 3 use host orchestration from the verified Git revision. A task's `services`
array selects services and their declared dependencies. Each service declares one
argument-array `command` with a `profile`, or a digest-pinned `container`. Optional
`setup` groups hold shared artifact leases for the entire service lifetime.
Readiness selects exactly one of `command` or `http_get`. `readiness.command`
runs in that service's execution context; its positive
`period_seconds`, `timeout_seconds`, and `failure_threshold` bound startup.
Probes use the service profile without starting a compiler-cache server. Their
command deadline includes bounded descendant cleanup before the backend's fallback
deadline. Probe recovery uses a separate ownership receipt from the application.

For HTTP endpoints, `readiness.http_get = { port = 4444, path = "/status" }`
uses the verified native controller without an interpreter, Nix evaluation or
container exec per request. Process Compose still schedules probes and owns
service readiness. Requests connect only to host `127.0.0.1`; containers must
publish that loopback port, including when another service owns their network
namespace. `path` defaults to `/` and `status_code` to `200` (allowed: 200–299).
Proxies and redirects are disabled. Command probes remain available for endpoints
accessible only inside a container and arbitrary application checks.

Optional `body` matches the complete response; `trim_body = true` removes leading
and trailing Unicode whitespace before comparison. Expected text is limited to
4096 UTF-8 bytes and responses used for body checks to 64 KiB. A wrong status,
wrong/oversized body, network error or timeout fails readiness. No response body
or credential is printed in diagnostics.

```toml
[services.api.readiness]
period_seconds = 2
timeout_seconds = 4
failure_threshold = 60
[services.api.readiness.http_get]
port = 8000
path = "/health/ready"
body = "OK"
trim_body = true
headers_from_environment = { Authorization = "LOCAL_API_AUTHORIZATION" }
```

Header values are resolved once from the service's effective declared project,
profile and service environment, then stored in private controller state. They
never become host execution environment overrides, command arguments or status
output. Missing/empty header variables fail planning. At most 16 headers and 8 KiB
of header names/values are allowed; routing, connection and compression overrides
are rejected.

For Basic authentication, use `basic_auth = { username_env = "LOCAL_USER",
password_env = "LOCAL_PASSWORD" }` instead of an Authorization header mapping.
`optional = true` omits authentication only when both values are empty;
`trim = true` trims surrounding whitespace first. A partial pair always fails.
Credentials must already be declared environment inputs; this feature does not
read credential stores or run a credential command. Existing generation checks,
probe ownership, deadlines and cancellation apply to native HTTP probes too.

`restart` is `no`, `always`, or `on_failure`; `shutdown_seconds` bounds cleanup.
Commands must stay in the foreground so the backend can own their lifetime.

The launcher routes service-bearing tasks through an upstream Process Compose
binary and a native chainman ownership adapter. Both are built/materialized from
the verified runtime only when services are used. The adapter owns compatible
reuse, per-client leases, and identity-checked crash recovery. Process Compose
owns readiness, process supervision, dependency ordering, and restart policy.
Go and the pinned `golang.org/x/sys` dependency are build inputs, not required host
installations. Container-only hosts build/materialize the controller through the
stock Nix container and execute the resulting native binary on the host.
Tasks that request only repository-scoped resources do not start a local service
controller or any unrequested local services.

`services-up TASK` retains the task's service set until an explicit
`services-stop`. `services-status` and `services-stop` use saved ownership data;
they do not parse the current project configuration or run setup. Normal `run`
releases only its own lease. A surviving task retains its lease even if its caller
is killed. After an abrupt controller failure, explicit stop recovers owned
process groups and labeled containers; it never signals an unrelated reused PID.
Project containers receive neither controller state nor an engine socket.

Explicit stop also cancels finite tasks that are using those services, including
their owned process groups and containers after a client crash. Such an interrupted
finite task fails; it cannot report a successful test or build after its services
were stopped. Finite clients also fail when a required service or controller dies.

For a foreground development stack, declare `wait_for_services = true` on its
task. `commands` may be omitted, or can run application preparation after services
are ready. The task then keeps its setup and service leases until interrupted,
explicitly stopped, or a selected service/controller fails. `services-stop` ends
that waiting task successfully; a service failure cancels it with a nonzero exit.
Inspection and stop still work with broken project configuration. For example:

```toml
[tasks.dev]
services = ["server", "frontend"]
wait_for_services = true

[tasks.dev.presentation]
title = "Example development"
urls = { Application = "http://localhost:3000" }
details = { Database = "{env:PROJECT_DATABASE_MODE}" }
```

`presentation` is optional and requires `wait_for_services = true`. It supports a
title and up to 16 labeled `urls` and `details` each. Values use the same literal
expansion as task environment, resolved with the effective project, profile and
task values. Declare any referenced variable (such as `PROJECT_DATABASE_MODE`
above) in project configuration. URLs must be absolute HTTP(S) browser addresses
without credentials. Select details explicitly: never put tokens, passwords or
connection strings here. Labels and values must be printable text. Metadata is
not a probe and does not publish ports or affect service ownership.

Service-bearing task commands also run under the native process-group owner, so
cancelling the client cleans up its foreground command descendants. The native
anchor retains service descriptors across Nix entry; container ownership receipts
cover a surviving daemon-side task as well. Waiting tasks publish application
readiness after successful preparation. `services-up` starts the services and setup only, without
running the task's application preparation commands.

Task leases also record the task process birth identity and, in container mode,
its labeled container. They survive shells that close inherited descriptors and
the death of a host engine client while its task container continues running.
Container inspection and cleanup verify the selected engine identity and ownership
label before addressing an immutable container ID. Engine errors or a changed
daemon fail closed; restore the original engine context to recover those services.
Explicit stop can recover service ownership even if a client lease is corrupt.

Tasks may declare `serial_group = "development-data"` to exclude other command
phases in that group within the same worktree, including across host and container
execution. A busy group fails with a retry message. The kernel lease follows live
children and is released before `wait_for_services`, allowing a maintenance task
to borrow a running development graph without concurrent seed/reference-data
mutations. Groups do not reserve repository-scoped services against other
worktrees; use `exclusive_services` for tests that need that stronger isolation.

Services can declare queued builds with the same task API:

```toml
[services.server.watch]
task = "build-server"
paths = ["server/src", "server/Cargo.toml", "server/Cargo.lock"]
ignore = ["**/target/**"]
debounce_ms = 100
startup_seconds = 300
```

Watchexec watches the declared paths, finishes an in-progress build and queues
one rebuild for edits received while busy. A successful initial build admits the
service; only subsequent successful builds request its replacement through Process
Compose. Failed builds retain the last successful service. The build task must
not recursively request services. Its setup groups are prepared before startup.
Container builds use the same labeled ownership and recovery as service containers.
Watchexec and Process Compose are unmodified, checksum-pinned upstream binaries;
their licenses are retained with the native controller assets.

`services-status` reports the private `services.log` path. Process Compose rotates
service output at 10 MiB with three backups and a seven-day retention window,
and keeps 500 lines per process in memory. Duplicate console and internal debug
logs are discarded. The private build-result receipts record the last build
completion or error; application containers cannot rewrite these receipts.

The default service scope is one canonical worktree, execution mode and host user
cache domain. Declare `scope = "repository"` on self-contained, digest-pinned data
containers to share them across linked worktrees and host/container execution modes.
The canonical Git common directory identifies the repository. Repository services
cannot run worktree commands, bind `{root}`, use worktree setup groups, or depend
on worktree services. Worktree services may depend on repository services; the
shared resource pool becomes ready before local services start.

Each worktree has its own claims on the shared pool. Stopping a worktree removes
only those claims; another worktree's task or persistent `services-up` claim keeps
its resources alive. Shared claims reference the parent task's existing descriptor,
kernel identity and labeled-container witnesses. There is no heartbeat or separate
task daemon. Resource intent is saved before acquisition so interrupted startup can
be recovered through the originating worktree's stop command. Status includes the
shared pool's processes, log path and recovery state.

Shared compatibility covers the verified runtime, data service declarations and
declared volume inputs, independent of worktree path and Nix mode. Incompatible
live users block replacement. The host engine identity is checked before reuse
and cleanup. Stale client records are reaped on the next controller operation.
Full backend/platform release qualification remains a rollout gate.

For tests or maintenance that mutate service data, declare `exclusive_services =
true` on the task. It reserves the selected services and their dependencies for
that task's existing lease, including repository pools. Acquisition fails visibly
if another user already holds any of them; ordinary users likewise cannot borrow
an exclusively held service. Unrelated services remain available. Dead clients
are recovered through the same descriptors, process identities and container
receipts as ordinary service users, without a second lock or timeout protocol.
This does not grant access to undeclared resources or make application mutations
transactional. `exclusive` still denotes project-wide cleanup without services.

Persistent container volumes declare their data format and compatibility inputs:

```toml
[[services.database.container.volumes]]
name = "database-data"
target = "/var/lib/postgresql"
format = "postgres-18-development-schema"
inputs = ["server/migrations", "fixtures/seed.json"]
policy = "preserve"
```

The engine volume carries scope and compatibility labels. chainman creates only
volumes needed by the selected service set. It refuses an existing volume owned by
another scope, missing ownership labels, changed compatibility while users are
active, or missing volumes during active use. Compatibility hashes relative file
paths and bytes, including directory descendants; missing inputs and symlink
escapes fail. Inputs must exist before launch. The project declares what defines
data compatibility and still owns migration/seed completion and application
readiness; a volume label does not certify successful application preparation.

Requested setup groups run before service planning, so a volume input may be a
declared setup artifact containing application-specific compatibility data. Setup
runs in the ordinary project environment, without the private controller export
mount. Planning then records the resulting input hashes. Admission checks them
again before acquiring services; concurrent changes fail visibly. Status and stop
continue to skip project setup and configuration.

`preserve` is the default and refuses incompatible data. `disposable` explicitly
permits recreation after users stop. Removal is unforced, so references from other
containers still prevent it. Ordinary service stop preserves volumes. chainman
never adopts or deletes older project volumes merely because their names look
similar. Image-specific UIDs remain declarations: for example, the qualified
Postgres 18 image runs as `999:999` with all capabilities dropped, using the engine's
normal image-to-volume initialization without a privileged preparation container.

`services-reset TASK --discard-data` explicitly removes the owned volumes needed
by that task's services, including preserved volumes with an older compatibility
hash. It requires current configuration/setup to resolve those declarations and
refuses live users in the worktree or repository pool, a changed engine identity,
foreign/unlabeled volumes, and external container references. Stop users first;
reset does not cancel them. No services or task commands start during reset. The
next service start creates empty volumes. A reset of multiple volumes is not an
atomic engine transaction: an engine failure can leave earlier removals complete.

Project environment handling is shared by tasks, services and direct execution:

```toml
[environment]
pass = ["APP_*", "CI"]
files = [
  { path = "environments/local.env" },
  { path = "environments/images.env", required = true, override = true },
  { path = "environments/provider.env", required = true, when = { APP_AUTH_MODE = "provider" } },
]
[environment.defaults]
APP_HOST = "{host}"
APP_BIND = "{bind}"
[environment.values]
APP_ROOT = "{root}"
APP_TOOL_CACHE = "{cache}/app-sdk"
[environment.modes.container-nix.values]
APP_CONTAINERIZED = "1"
```

Files contain literal `NAME=value` lines, blank lines and comments. Quotes,
dollar expressions and braces in file values are retained literally; files are
never sourced as shell programs. Duplicate names, invalid names, missing required
files and paths escaping the project fail. Ordered files fill unset variables;
`override = true` explicitly makes that file authoritative. Defaults then fill
remaining unset names, with mode defaults overriding project defaults. Project
values, mode values, profile environment and task/service environment apply in
that order. Existing explicit caller values therefore beat defaults, while fixed
values deliberately beat callers. Managed runtime and compiler ownership variables
cannot be replaced through these declarations.

A file can declare a nonempty `when` table of exact literal environment matches.
Conditions read inherited/task context and earlier files in declaration order;
unselected files are not read or required. Later files cannot change a variable
already used to select a file. Service fingerprints include the selected file
bytes, so changing provider inputs invalidates reuse.

Declared TOML values support `{root}`, `{cache}` (shared download cache), `{work}`
(the selected build context), `{host}`, `{bind}` and `{env:VARIABLE}` references.
References within a table resolve independently of key order; missing references
and cycles fail. `{host}` is loopback in host mode and `host.docker.internal` in
container mode. `{bind}` is loopback in host mode and `0.0.0.0` inside a container.
Use `container.host_access = true` to add the host gateway alias when a project
container needs a declared host service. It does not mount an engine socket.

Each profile, task or command service may have its own `transport` table with
`ports`, `mounts`, `host_access` and opt-in `display = "x11"`. Port mappings explicitly bind host loopback. These
options apply only to that task's or service's Nix container; a verification task
does not inherit a frontend service's published ports. Data containers use their
own `container` declaration instead. Project-wide container options remain additive.

Service workflows with containers acquire an ordinary private engine bridge.
Containers join it with stable service DNS aliases; host publications still bind
only loopback. Use `{service:database:5432}` in a declared environment value, for
example `DATABASE_URL = "postgresql://app@{service:database:5432}/app"`. The port is
the service's listening TCP port. Container commands receive its DNS address and
that port; host commands receive the corresponding loopback publication (which
may use a different port). A host command service uses its listening port directly.
Unknown services, invalid ports and ambiguous or missing host publications fail.
A service borrowing another service's namespace uses the owner's DNS alias.

Linked worktrees share the repository bridge and repository-scoped data services;
worktree service aliases remain distinct. Standalone consumers inside another
repository do not inherit that repository's bridge. Existing parent-linked leases
cover network creation, startup and execution. Cleanup stops endpoints first and
removes the network after its last client leaves. It checks engine identity and
ownership labels and removes only the inspected network ID, without forced
disconnection. An unrelated attached container blocks removal. Pure host workflows
with no container services do not acquire a bridge or require an engine.

When a local stack relies on loopback between processes, a task or service may
declare `network_service = "database"`. In container mode it joins that acquired
service's ordinary engine network namespace. Host mode already shares host
loopback. This keeps browsers, emulators and frontends on the same local addresses
without exposing an engine socket, adding a proxy, or selecting host networking.
The owner must be in the task's service set or the service's dependency closure,
cannot itself borrow a network, and cannot automatically restart. Publish all
ports on that owner; borrowers cannot also publish ports or add host aliases.
The host adapter verifies the saved owner label and engine identity, then joins
its immutable container ID. A replaced or stopped owner is refused. Different
network owners cannot be combined within one task graph.

Controller planning receives host environment as bounded NUL-separated data in its
private temporary export directory. It selects only `environment.pass` matches;
those values are never installed in the trusted planner's process environment.
Effective project values affect worktree service compatibility, and resolved data
container values affect shared resource compatibility. Environment-file bytes also
participate in the planning/execution guard. Status and stop remain independent of
current environment-file validity. Plans containing service credentials stay in the
user's private host state outside project-container mounts.

A task with `depends_on` may omit commands to act as an aggregate; dependencies
execute once in order. It does not accept extra command arguments. For declared
application cleanup, `exclusive = true` takes the same project maintenance gate as
shared cache cleanup and updates. It refuses independent active tasks and cannot
acquire or borrow services. Project cleanup commands still declare exactly which
application outputs they own; the exclusive gate supplies concurrency protection.

### Scoped execution transport

Profiles, tasks and command services accept `transport`. chainman combines the
project-wide `container` table, selected profile transport and execution transport.
Identical mounts are deduplicated; conflicting targets or host port bindings fail.
Explicit nested mounts are allowed, for example writable state below read-only
credentials. A service receives its own profile and transport, not its caller's.
Data containers retain their separate `container` declarations.

```toml
[profiles.operations]
flake = "flake.nix#operations"
[profiles.operations.transport]
mounts = [
  { source_env = "APP_CREDENTIAL_DIRECTORY", target = "/credentials" },
  { source_env = "APP_STATE_DIRECTORY", target = "/credentials/state", read_only = false },
  { source_env = "APP_OPTIONAL_KEY", target = "/optional-key", optional = true },
]
```

The same profile access applies to `exec --profile operations`,
`shell --profile operations`, and tasks selecting that profile. `source_env`
uses the named host input as a path, without forwarding its value into the
workload. Optional mounts skip an unset variable or absent path; empty values,
invalid declarations and unsafe existing sources still fail. chainman does not
create these directories. Projects own credential selection and state preparation.

Setup for an entry with transport runs separately without profile/task mounts.
The workload rechecks readiness with installation disabled; a concurrent change
fails with setup recovery guidance. Task graphs share one execution container,
so tasks with commands must declare equivalent effective transport.
Numeric port spelling and an omitted TCP suffix are normalized. Independent
mount ordering does not matter; overlapping mount ordering, source paths, and
access permissions remain significant. Split commands
requiring different access into separate host entries. Nested entry cannot change
container transport; leave the active shell and enter the selected profile from
the host. Containers isolate access paths, not mutually distrustful project code.

### Graphical tasks

```toml
[tasks.browser-interactive]
profile = "browser"
commands = [["playwright", "test", "--headed"]]
transport = { display = "x11" }
```

X11 transport is opt-in and currently supports local Linux X11/Xwayland displays
in container Nix. Set `DISPLAY=:0` (use your actual display number); `XAUTHORITY`
selects an authority file, defaulting to `~/.Xauthority`. The file must be regular
and not a symlink. Remote/TCP display selectors and native Wayland transport are
unsupported. Host modes use the caller's graphical environment without adaptation.

chainman validates display availability before setup or services. A verified
helper reads at most 1 MiB of authority data, selecting the local display's
MIT-MAGIC-COOKIE-1 authentication. The workload receives only the selected socket
and a private, mode-0600 authority file, removed on exit or interruption. The
original file is never mounted into the workload or modified. Cancellation forwards
the signal and allows the container client three seconds to exit, then forces
client exit and removes private files. If the engine is unresponsive, inspect its
container status; forcing a client to exit does not guarantee the engine stopped
its workload. Bootstrap and preflight clients use the same bounded shutdown.
Nested verified preparation helpers receive five seconds so their own client
shutdown can finish; cancellation aborts entry before subsequent setup or tasks.
The final entry owns its private transport files. Abrupt host termination cannot
guarantee cleanup. No `xhost` changes
or host-language installation are needed. A graphical application still has the
access granted by the selected X server; this does not isolate applications from
one another on that desktop.

### Inspecting access

```sh
just chainman explain browser-interactive --json
just chainman explain --profile operations --json
```

Task inspection reports transport separately for tasks and command services.
Profile inspection describes direct shell/exec access. Reports include declaration
origins, field-source files and explicit
container-option contributions. Source environment values and authentication
contents are not printed. Inspection does not prepare credentials, install project
dependencies or start services. Host inspection reports path presence, optional
omissions and unresolved inputs; container inspection labels mounts **not checked
on host**, rather than inferring host presence from its own filesystem. Presence
is not a guarantee that execution will admit a mount. Port placeholders remain
declarative in inspection; execution resolves and validates them against the
selected environment. Both literal and resolved ports must be integers from 1
through 65535, with a loopback binding and optional `/tcp` or `/udp` suffix. This
also applies to explicit container-option port mappings. Host Nix
ignores container transport, and ordinary launches remain headless by default.

## Formatter and hook declarations

`[formatters.NAME]` declares `paths`, optional `exclude`, `profile`, optional
`setup`, and argument-vector `write`/`check` commands. These are the only operations
used by staged formatting; task bindings for full formatting are independent.
`[hooks] enabled=true` opts into formatting-only pre-commit and outgoing-source
pre-push checks through pinned lefthook. `hooks.config` optionally selects an
upstream lefthook override file. See the complete [hook guide](hooks.md).

## Reusable pnpm setup

A setup group can set `pnpm=true` instead of spelling out `commands` and
`readiness`. Keep the project profile, fingerprint inputs and readiness artifacts
explicit:

```toml
[setup.javascript]
pnpm = true
profile = "javascript"
inputs = ["package.json", "**/package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc", "patches/**"]
exclude_inputs = ["**/node_modules/**", ".cache/**", ".chainman/**"]
artifacts = ["node_modules/.modules.yaml"]
```

The helper checks the selected profile's pnpm against the exact `packageManager`
version, disables pnpm's automatic package-manager download, installs with a frozen
lockfile, and asks pnpm to validate dependencies before a harmless Node command.
A mismatch names both versions and requires reconciliation of the project flake
and manifest. Normal dependency age, integrity and build-approval policies still
apply. No global Corepack shims are created. Custom installers can retain explicit
commands/readiness instead; do not combine them with `pnpm=true`.

Complete `setup` also runs `recipes.setup` extensions and installs declared hooks.
`setup --no-hooks` explicitly opts out for CI; selecting individual setup groups
never installs hooks. Ordinary targeted repair remains subject to
`CHAINMAN_SETUP=prompt|auto|error`.
