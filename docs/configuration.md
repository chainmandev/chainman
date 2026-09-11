# Project configuration

A consumer checks in `chainman.toml`, `chainman.lock`, the two bootstrap companions,
and a small `justfile` adapter. Configuration schema is 1. A minimal existing-project
configuration is:

```toml
schema = 1
[project]
default_profile = "default"
[profiles.default]
flake = "nix#default"
[commands]
setup = [["sh", "scripts/setup-project.sh"]]
verify = [["just", "check"]]
[setup]
inputs = ["nix/flake.nix", "nix/flake.lock", "package.json", "pnpm-lock.yaml"]
artifacts = ["node_modules/.pnpm/lock.yaml"]
```

Commands are arrays of argument arrays. `exec --profile NAME -- ARGS...` preserves
literal arguments; shell expansion happens only in an explicitly chosen shell.

`deps-query` also accepts `{"schema":1,"operation":"batch","requests":[...]}`.
Each entry is a normal schema-1 query. The response contains `schema`, `operation`
and an ordered `results` array. A batch uses one eligibility time and credential
context; any failed query fails the whole response. Batches contain 1–128 entries,
cannot nest, and retain the existing 4 MiB input bound and per-query network bounds.
`run NAME` invokes a declared command. `command_profiles.NAME` overrides the project
default for that command. `setup` uses manifest/toolchain fingerprints and declared
artifacts. A profile selects a project-relative `path#shell` (also `flake.nix#shell`)
or a built-in `runtime_profile`. Built-ins are core, javascript, rust, python, go,
flutter, swift, compose and browser. Without a named project override, default maps
to core. The special `host` profile runs directly in the already bootstrapped core
context; it is useful for an adapter that subsequently selects the project shell.
It does not provision host language tools.

For `deps-query` with `provider="swift"` and `operation="metadata"`, supply an exact
stable version such as `1.0.0`. Chainman reads the bounded release inventory, then
resolves tag and commit-time evidence only for that version, including a matching
`v1.0.0` tag. The response retains the raw tag identity and the later release or
commit publication time; metadata does not claim eligibility. Full version
selection and final artifact audits still require their complete evidence.

GitHub registry metadata can use an explicitly supplied `GITHUB_TOKEN` environment
variable. Absent or empty keeps anonymous requests. Supply it through the caller's
secret environment, never a token literal in project configuration, URLs or command
arguments. Chainman does not discover credentials from `gh`, `.netrc`, Git helpers
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

Host mode inherits the explicitly supplied variable. Container callers can select
`environment.pass = ["GITHUB_TOKEN"]`; the existing forwarding passes its name,
without putting its value in arguments. This option covers Chainman's Python
registry metadata requests. Native Git, Swift and Nix downloads keep their own
credential behavior; this does not qualify general private-repository support.

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

An explicit `TMPDIR` remains the temporary base across bootstrap and profile
refreshes, including project/profile overrides. Without one, Chainman retains
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

Dynamic adapters can set `CHAINMAN_FORWARD_ENV` to comma-separated names or narrow
patterns. `CHAINMAN_CONTAINER_OPTIONS_FILE` names a regular file of literal option
and value lines, produced from an argument array rather than a shell command string.
Supported pairs are publish, bind mount/volume, add-host, hostname, label, name,
network (`host` or `bridge`), and platform (`linux/amd64` or `linux/arm64`). Use explicit
platforms only with a working engine/emulation/native builder for that architecture.
Options never authorize privileged mode, Docker socket access or another container's
network namespace. The project adapter owns optional credential/native mounts and
must obtain the same trust/authorization it needed before adopting Chainman.
Whole container HOME replacement requires a project-contained directory. Explicit
HOME subdirectory mounts remain available for those credential/SDK adapters; mounts
over HOME or project ancestors are rejected.

Bootstrap controls are `CHAINMAN_MODE` (container-nix by default),
`CHAINMAN_CONTAINER_ENGINE` (`docker` or `podman`; `CHAINMAN_ENGINE` is an alias),
`CHAINMAN_NIX_BIN` (an explicit absolute executable), and `CHAINMAN_PROJECT_ROOT`
(an explicit consumer root). Paths with spaces and invocation from another working
directory are supported. Newlines and ambiguous container comma-paths are rejected.
`CHAINMAN_ARCHIVE` selects a local archive override, but its contents must still
match the committed lock hash. Mode changes inside an active shell are rejected.
