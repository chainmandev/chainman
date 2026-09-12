#!/bin/sh
# Copy this launcher and fetch.nix (as chainman-fetch.nix) into consumer scripts/.
# The pinned image and this companion are the bootstrap trust base.
# Child shell programs expand their own positional and environment values.
# shellcheck disable=SC2016
set -eu
fail() {
    printf 'Chainman bootstrap: %s\n' "$*" >&2
    exit 2
}
single_line() {
    case "$1" in *'
'* | *''*) fail 'Newlines are not supported in bootstrap paths or options.' ;; esac
}
script_dir=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
self=$script_dir/$(basename -- "$0")
root=$(CDPATH='' cd -P -- "${CHAINMAN_PROJECT_ROOT:-$script_dir/..}" && pwd)
single_line "$root"
helper=$script_dir/chainman-fetch.nix
[ -f "$helper" ] || helper=$script_dir/fetch.nix
[ -f "$helper" ] && [ ! -L "$helper" ] || fail 'Missing regular chainman-fetch.nix companion.'
if [ "${1:-}" = script ]; then
    shift
    script_profile=
    if [ "${1:-}" = --profile ]; then
        [ "$#" -ge 3 ] || fail 'script --profile requires a profile and a Bash script.'
        script_profile=$2
        [ -n "$script_profile" ] || fail 'script --profile requires a nonempty profile.'
        shift 2
    fi
    [ "$#" -ge 1 ] || fail 'script requires a Bash script and optional arguments.'
    script_file=$1
    shift
    [ -f "$script_file" ] && [ ! -L "$script_file" ] || fail 'script requires a regular Bash script.'
    # Just creates shebang scripts outside the project mount. Carry their code
    # as one literal argument, retaining trailing newlines, $0, argv and stdin.
    script_body=$(cat -- "$script_file" && printf '.') || fail 'Could not read Bash script.'
    script_body=${script_body%.}
    set -- exec -- bash --noprofile --norc -eu -o pipefail -c "$script_body" "$script_file" "$@"
    if [ -n "$script_profile" ]; then
        shift
        set -- exec --profile "$script_profile" "$@"
    fi
fi
CHAINMAN_REQUEST_ACTION=${1:-doctor}
CHAINMAN_REQUEST_TASK=${2:-}
export CHAINMAN_REQUEST_ACTION CHAINMAN_REQUEST_TASK
control_dispatch() {
    if [ "$1" = services-reset ]; then
        [ "$#" = 3 ] && [ "$3" = --discard-data ] || fail 'usage: services-reset TASK --discard-data'
    fi
    case "$1" in
        services-status | services-stop) ;;
        *)
            if [ "$mode" = container-nix ] && [ "${CHAINMAN_CONTAINER_NETWORK_MODE:-bridge}" = host ]; then
                fail 'Service workflows use owned network namespaces; the host-network override is for standalone tasks.'
            fi
            ;;
    esac
    case "$1" in
        services-status | services-stop) ;;
        *)
            # Setup may produce application data identities used by volume
            # compatibility. Run it before planning, in the ordinary project
            # environment, without the private controller export mount.
            case "$1" in
                run | services-run | services-up | services-reset) control_task=${2:-} ;;
                *) control_task=$1 ;;
            esac
            "$self" _service-prepare "$control_task" >&2
            ;;
    esac
    # Only the internal export operation mounts this private output directory.
    # It builds verified tooling and emits JSON; no consumer code executes there.
    control_output=$(mktemp -d "${TMPDIR:-/tmp}/chainman-control.XXXXXXXX")
    trap 'rm -rf -- "$control_output"' EXIT HUP INT TERM
    # Serialized data, never installed in the planner process environment. The
    # trusted planner selects only declared environment.pass names from it.
    (
        umask 077
        env -0 > "$control_output/host-environment"
    )
    case "$(uname -s):$(uname -m)" in
        Linux:aarch64 | Linux:arm64) control_target=linux-arm64 ;;
        Linux:x86_64) control_target=linux-amd64 ;;
        Darwin:arm64) control_target=darwin-arm64 ;;
        Darwin:x86_64) control_target=darwin-amd64 ;;
        *) fail 'Unsupported native service-controller platform.' ;;
    esac
    control_engine=
    control_candidates=${CHAINMAN_CONTAINER_ENGINE:-${CHAINMAN_ENGINE:-}}
    if [ -z "$control_candidates" ]; then control_candidates='docker podman'; fi
    for candidate in $control_candidates; do
        case "$candidate" in docker | podman) ;; *) fail 'Unsupported container engine.' ;; esac
        if command -v "$candidate" > /dev/null 2>&1; then
            control_engine=$(command -v "$candidate")
            break
        fi
    done
    printf '%s\n%s\n' --mount "type=bind,src=$control_output,dst=$control_output" > "$control_output/mounts"
    CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$control_output/mounts "$self" _control-export "$control_output" "$control_target" \
        "${XDG_CACHE_HOME:-$HOME/.cache}/chainman/services" "$control_engine" "$self" "$@"
    case "$1" in
        services-status | services-stop)
            IFS= read -r control_state < "$control_output/state"
            "$control_output/chainman-control" "${1#services-}" "$control_state"
            ;;
        services-up) "$control_output/chainman-control" up "$control_output/plan.json" ;;
        services-reset) "$control_output/chainman-control" reset "$control_output/plan.json" --discard-data ;;
        *) "$control_output/chainman-control" run "$control_output/plan.json" ;;
    esac
    control_result=$?
    rm -rf -- "$control_output"
    trap - EXIT HUP INT TERM
    exit "$control_result"
}
update_dispatch() {
    # Fixed phases only. Host shell orchestration needs neither host Python nor
    # an engine socket in the resolver or verifier containers.
    umask 077
    update_cache=${XDG_CACHE_HOME:-$HOME/.cache}/chainman/updates
    mkdir -p "$update_cache"
    update_output=$(mktemp -d "$update_cache/candidate.XXXXXXXX")
    update_output=$(CDPATH='' cd -P -- "$update_output" && pwd)
    trap 'printf "Chainman: update candidate preserved at %s\n" "$update_output/candidate" >&2' EXIT
    mkdir "$update_output/candidate" "$update_output/control"
    printf '%s\n%s\n' --mount "type=bind,src=$update_output,dst=$update_output" > "$update_output/control/mounts"
    CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
        "$self" _update-prepare "$update_output" "$@" >&2
    if [ -f "$update_output/control/help" ]; then
        rm -rf -- "$update_output"
        trap - EXIT
        exit 0
    fi
    IFS= read -r update_at < "$update_output/control/at"
    update_launcher=$update_output/original-bootstrap/chainman.sh
    update_candidate "$update_launcher" _update-resolve "$update_at" "$@" >&2
    CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
        CHAINMAN_PROJECT_ROOT=$root "$update_launcher" _update-inspect "$update_output" >&2
    IFS= read -r update_changed < "$update_output/control/changed"
    if [ "$update_changed" = yes ]; then
        while IFS= read -r update_action && IFS= read -r update_task; do
            CHAINMAN_UPDATE_ACTIVE=1 update_candidate \
                "$update_output/candidate-bootstrap/chainman.sh" "$update_action" "$update_task" < /dev/null >&2
        done < "$update_output/control/verify"
    fi
    CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
        CHAINMAN_PROJECT_ROOT=$root "$update_launcher" _update-finalize "$update_output"
    rm -rf -- "$update_output"
    trap - EXIT
    exit 0
}
update_candidate() (
    # A disposable checkout must not inherit Git routing, hooks or identity that
    # target the original. Values never become shell code.
    for update_git in $(env | sed -n 's/^\(GIT_[A-Za-z0-9_]*\)=.*/\1/p'); do
        unset "$update_git"
    done
    exec env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_COUNT=0 GIT_TERMINAL_PROMPT=0 \
        CHAINMAN_PROJECT_ROOT="$update_output/candidate" "$@"
)
expression='import (builtins.toPath (builtins.getEnv "CHAINMAN_BOOTSTRAP_HELPER")) {
    root = builtins.getEnv "CHAINMAN_PROJECT_ROOT";
    archive = builtins.getEnv "CHAINMAN_ARCHIVE";
    action = builtins.getEnv "CHAINMAN_BOOTSTRAP_ACTION";
}'
mode=${CHAINMAN_MODE:-container-nix}
case "$mode" in host-nix | container-nix) ;; *) fail 'CHAINMAN_MODE must be host-nix or container-nix.' ;; esac
if [ "$mode" = container-nix ]; then
    case "$root$script_dir" in *,*) fail 'Container mount paths cannot contain commas.' ;; esac
    case "$root" in / | "${HOME:-/}") fail 'Consumer root cannot be the host root or home directory.' ;; esac
    case "${HOME:-/}/" in "$script_dir/"*) fail 'The launcher cannot require a blanket home mount.' ;; esac
fi
if [ -n "${CHAINMAN_ACTIVE_MODE:-}" ] && [ "$CHAINMAN_ACTIVE_MODE" != "$mode" ]; then
    fail 'Start a different mode outside the active development shell.'
fi
cd "$root"
[ -f chainman.lock ] && [ ! -L chainman.lock ] || fail 'Missing regular chainman.lock.'
[ ! -L "$root/.chainman" ] || fail '.chainman must be a real directory.'

case "$CHAINMAN_REQUEST_ACTION" in
    deps-update | chainman-update)
        [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ] || fail 'Start updates through the host launcher so candidate verification can control its own services.'
        [ -z "${CHAINMAN_UPDATE_ACTIVE:-}" ] || fail 'An update hook must not recursively start another update.'
        shift
        if [ "$CHAINMAN_REQUEST_ACTION" = chainman-update ]; then set -- --only-chainman "$@"; fi
        update_dispatch "$@"
        ;;
esac

if [ "$mode" = host-nix ] || [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" = 1 ]; then
    # Nix assigns TMPDIR on every shell entry. Keep the caller's selected base
    # across the bootstrap and later profile refreshes, within this mode only.
    if [ -n "${TMPDIR:-}" ]; then
        export CHAINMAN_TEMP_BASE="$TMPDIR"
    fi
    nix_bin=${CHAINMAN_NIX_BIN:-nix}
    if [ -n "${CHAINMAN_NIX_BIN:-}" ]; then
        case "$nix_bin" in /*) ;; *) fail 'CHAINMAN_NIX_BIN must be an absolute executable path.' ;; esac
        [ -x "$nix_bin" ] || fail 'CHAINMAN_NIX_BIN is not executable.'
    else
        command -v nix > /dev/null 2>&1 || fail 'Host mode requires Nix; no host-language fallback is used.'
    fi
    case "$nix_bin" in /*) ;; *) nix_bin=$(command -v "$nix_bin") ;; esac
    case "$nix_bin" in /*) ;; *) nix_bin=$(CDPATH='' cd -- "$(dirname -- "$nix_bin")" && pwd)/$(basename -- "$nix_bin") ;; esac
    # Probe the evaluator, not a vendor-specific --version display string.
    "$nix_bin" --extra-experimental-features 'nix-command flakes' eval --raw --expr '
      if builtins.compareVersions builtins.nixVersion "2.24" >= 0
      then "compatible" else throw "Chainman requires Nix >= 2.24"
    ' > /dev/null || fail 'Nix compatibility check failed; update the selected host/image Nix. Chainman does not replace it.'
    # Resolve the profile symlinks so forwarding Nix does not also forward every
    # unrelated program installed in the user's global profile.
    selected_nix=$nix_bin
    while [ -L "$selected_nix" ]; do
        target=$(readlink "$selected_nix")
        case "$target" in /*) selected_nix=$target ;; *) selected_nix=$(dirname -- "$selected_nix")/$target ;; esac
    done
    CHAINMAN_RUNTIME_NIX_BIN=$(CDPATH='' cd -P -- "$(dirname -- "$selected_nix")" && pwd)
    export CHAINMAN_RUNTIME_NIX_BIN
    nix_eval() {
        CHAINMAN_BOOTSTRAP_HELPER=$helper CHAINMAN_PROJECT_ROOT=$root CHAINMAN_BOOTSTRAP_ACTION=$1 \
            "$nix_bin" --extra-experimental-features 'nix-command flakes' eval --impure --raw --expr "$expression"
    }
    metadata=$(nix_eval metadata)
    {
        IFS= read -r _content_id
        IFS= read -r nar_hash
        IFS= read -r archive
    } << EOF
$metadata
EOF
    if [ "$archive" != - ]; then
        case "$archive" in /*) ;; *) archive=$root/$archive ;; esac
        single_line "$archive"
        # Every component must be real; a local override cannot escape via links.
        probe=$archive
        while [ "$probe" != / ]; do
            [ ! -L "$probe" ] || fail 'Archive paths must not contain symlinks.'
            probe=$(dirname -- "$probe")
        done
        [ -f "$archive" ] || fail 'Local archive is missing or is not a regular file.'
        CHAINMAN_ARCHIVE=$archive
        export CHAINMAN_ARCHIVE
    fi
    # Fetch and register a normal Nix GC root in the same evaluator process.
    # A bare `nix eval --raw` result loses its temporary root before the next
    # `nix develop`, allowing automatic GC to remove even the runtime scripts.
    if [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" = 1 ]; then
        runtime_roots=/nix/var/nix/chainman-runtime-roots
    else
        runtime_roots=${XDG_CACHE_HOME:-$HOME/.cache}/chainman/runtime-roots
    fi
    single_line "$runtime_roots"
    case "$runtime_roots" in /*) ;; *) fail 'Runtime cache must be an absolute path.' ;; esac
    probe=$runtime_roots
    while [ "$probe" != / ]; do
        [ ! -L "$probe" ] || fail 'Runtime cache directories must not contain symlinks.'
        probe=$(dirname -- "$probe")
    done
    (
        umask 077
        mkdir -p "$runtime_roots"
    )
    runtime_root=$runtime_roots/$_content_id
    [ ! -e "$runtime_root" ] || [ -L "$runtime_root" ] || fail 'Runtime GC root must be a symlink.'
    fetch_runtime() {
        CHAINMAN_BOOTSTRAP_HELPER=$helper CHAINMAN_PROJECT_ROOT=$root CHAINMAN_BOOTSTRAP_ACTION=fetch \
            "$nix_bin" --extra-experimental-features 'nix-command flakes' build --impure --expr "$expression" \
            "$@" --print-out-paths
    }
    # A content-keyed root is immutable. Replacing it on every entry makes Nix's
    # PID-based temporary symlink names collide across container PID namespaces.
    # Still evaluate the selected archive on every entry, including warm starts.
    if [ -L "$runtime_root" ]; then
        store=$(fetch_runtime --no-link)
    elif ! store=$(fetch_runtime --out-link "$runtime_root"); then
        # Another first writer may have installed the same root. Re-evaluate the
        # archive and require the exact link below; never accept a failed fetch
        # merely because some old source is cached.
        [ -L "$runtime_root" ] || fail 'Runtime GC root registration failed.'
        store=$(fetch_runtime --no-link)
    fi
    [ "$(readlink "$runtime_root")" = "$store" ] || fail 'Runtime GC root does not match the verified source.'
    # A process killed between creating the link and registering its indirect
    # root must not leave a permanently unregistered warm-cache entry.
    registered_roots=$("$CHAINMAN_RUNTIME_NIX_BIN/nix-store" --query --roots "$store")
    if ! printf '%s\n' "$registered_roots" | grep -F -x -q -- "$runtime_root -> $store"; then
        store=$(fetch_runtime --out-link "$runtime_root")
    fi
    actual=$("$nix_bin" --extra-experimental-features nix-command hash path "$store")
    [ "$actual" = "$nar_hash" ] || fail 'Runtime store source failed NAR verification.'
    # Archives are source distributions: symlinks are excluded before evaluating
    # even the verified flake, so extraction cannot introduce an outside path.
    [ -z "$(find "$store" -type l -print -quit)" ] || fail 'Runtime archives must not contain symlinks.'
    if [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ] && [ "$(nix_eval route)" = 1 ]; then
        control_dispatch "$@"
    fi
    export CHAINMAN_MODE="$mode" CHAINMAN_ACTIVE_MODE="$mode"
    # Bootstrap entry replaces an external project shell. Its old profile token no
    # longer describes PATH, even when the project inputs themselves are unchanged.
    unset IN_NIX_SHELL CHAINMAN_ACTIVE_PROFILE CHAINMAN_ACTIVE_FINGERPRINT
    exec "$nix_bin" --extra-experimental-features 'nix-command flakes' develop "path:$store/nix#bootstrap" --no-write-lock-file \
        --command python3 -c '
import os, sys
root, store, *args = sys.argv[1:]
os.chdir(root)
os.environ.update(CHAINMAN_RUNTIME=store, CHAINMAN_ROOT=root, CHAINMAN_PROJECT_ROOT=root,
                  PYTHONDONTWRITEBYTECODE="1")
os.execv(sys.executable, [sys.executable,
    store + "/scripts/chainman.py", "--root", root, *args])
' "$root" "$store" "$@"
fi

engine=${CHAINMAN_CONTAINER_ENGINE:-${CHAINMAN_ENGINE:-}}
if [ -z "$engine" ]; then
    for candidate in docker podman; do
        if command -v "$candidate" > /dev/null 2>&1; then
            engine=$candidate
            break
        fi
    done
fi
case "$engine" in docker | podman) ;; *) fail 'Container mode requires Docker or Podman.' ;; esac
image=docker.io/nixos/nix:2.33.3@sha256:c2f7db70a432d00c6759af108ff4fbc74a4c00e2d4517162e72338e7b9449c1f
uid=$(id -u)
gid=$(id -g)
container_uid=$uid
container_gid=$gid
if [ "$engine" = docker ]; then
    security_options=$("$engine" info --format '{{range .SecurityOptions}}{{println .}}{{end}}') || fail 'Cannot determine Docker daemon identity mapping.'
    while IFS= read -r option; do
        if [ "$option" = name=rootless ]; then
            container_uid=0
            container_gid=0
        fi
    done << EOF
$security_options
EOF
fi
volume=${CHAINMAN_NIX_VOLUME:-chainman-nix-$uid}
case "$volume" in '' | *[!A-Za-z0-9_.-]*) fail 'CHAINMAN_NIX_VOLUME must be a container volume name.' ;; esac
case "$volume" in [A-Za-z0-9]*) ;; *) fail 'CHAINMAN_NIX_VOLUME must start with a letter or number.' ;; esac
export CHAINMAN_NIX_VOLUME="$volume"
temporary=$(mktemp -d "${TMPDIR:-/tmp}/chainman-bootstrap.XXXXXXXX")
trap 'rm -rf -- "$temporary"' EXIT HUP INT TERM
# Select an explicit architecture before initializing or evaluating in the image.
# Its Nix store volume must not inherit the default architecture's profile links.
platform=${CHAINMAN_CONTAINER_PLATFORM:-}
case "$platform" in '' | linux/amd64 | linux/arm64) ;; *) fail 'CHAINMAN_CONTAINER_PLATFORM must be linux/amd64 or linux/arm64.' ;; esac
network_mode=${CHAINMAN_CONTAINER_NETWORK_MODE:-bridge}
case "$network_mode" in host | bridge) ;; *) fail 'CHAINMAN_CONTAINER_NETWORK_MODE must be host or bridge.' ;; esac
if [ -n "${CHAINMAN_CONTAINER_OPTIONS_FILE:-}" ]; then
    options_file=$CHAINMAN_CONTAINER_OPTIONS_FILE
    case "$options_file" in /*) ;; *) options_file=$root/$options_file ;; esac
    [ -f "$options_file" ] && [ ! -L "$options_file" ] || fail 'Container options file must be a regular file.'
    {
        cat "$options_file"
        printf '\n'
    } > "$temporary/extra"
    options_file=$temporary/extra
    while IFS= read -r option || [ -n "$option" ]; do
        [ -n "$option" ] || continue
        IFS= read -r value || [ -n "$value" ] || fail 'Container option lacks a value.'
        if [ "$option" = --platform ]; then
            case "$value" in linux/amd64 | linux/arm64) ;; *) fail 'Container platform must be linux/amd64 or linux/arm64.' ;; esac
            [ -z "$platform" ] || [ "$platform" = "$value" ] || fail 'Conflicting container platform selections.'
            platform=$value
        elif [ "$option" = --network ]; then
            case "$value" in host | bridge) ;; *) fail 'Explicit container network must be host or bridge.' ;; esac
            [ -z "${CHAINMAN_CONTAINER_NETWORK_MODE:-}" ] || [ "$network_mode" = "$value" ] || fail 'Conflicting container network selections.'
            network_mode=$value
        fi
    done < "$options_file"
fi
export CHAINMAN_CONTAINER_PLATFORM="$platform" CHAINMAN_CONTAINER_NETWORK_MODE="$network_mode"
if [ -n "$platform" ]; then volume=$volume-${platform#linux/}; fi
downloads_volume=${volume}-downloads
run() {
    if [ -n "$platform" ]; then set -- --platform "$platform" "$@"; fi
    if [ "$engine" = podman ]; then "$engine" run --userns=keep-id "$@"; else "$engine" run "$@"; fi
}
# Capability-free UID 0 still owns /. Keep Nix's nonexistent build HOME from
# being created accidentally, without changing writable mounts or disk-backed /tmp.
container_init='
    if [ "$(id -u)" = 0 ]; then chmod 0555 /; fi
    mkdir -p "$HOME"
    mkdir -p /nix/tmp
    export TMPDIR=/nix/tmp CHAINMAN_TEMP_BASE=/nix/tmp
    exec "$@"
'
daemon_name=$volume-daemon
validate_daemon() {
    daemon_identity=$("$engine" container inspect --format '{{index .Config.Labels "dev.chainman.store.schema"}}
{{index .Config.Labels "dev.chainman.store.volume"}}
{{.Config.Image}}
{{.Config.User}}
{{.HostConfig.Privileged}}
{{.HostConfig.ReadonlyRootfs}}
{{.HostConfig.CapDrop}}
{{.HostConfig.SecurityOpt}}
{{range .Mounts}}{{.Type}}:{{.Name}}:{{.Destination}}:{{.RW}};{{end}}
{{len .HostConfig.PortBindings}}
{{.HostConfig.NetworkMode}}
pid={{.HostConfig.PidMode}}' "$daemon_name")
    expected_identity="1
$volume
$image
$container_uid:$container_gid
false
true
[ALL]
[no-new-privileges]
volume:$volume:/nix:true;
0
bridge
pid="
    [ "$daemon_identity" = "$expected_identity" ] || fail "Nix store daemon $daemon_name has incompatible identity or isolation. Stop its clients and remove that daemon container before changing its configuration; retain the Nix volume."
}
if "$engine" container inspect "$daemon_name" > /dev/null 2>&1; then validate_daemon; fi
volume_clients=$("$engine" ps --filter "volume=$volume" --format '{{.ID}} {{.Label "dev.chainman.store.schema"}}')
while IFS= read -r client; do
    case "$client" in '' | *' 1') ;; *) fail 'Stop existing containers using this Nix volume before migrating from independent local-store writers to the shared daemon.' ;; esac
done << EOF
$volume_clients
EOF
# Only the named Nix store is prepared as root. No host directory is mounted here.
run --rm --user 0:0 --label dev.chainman.store.schema=1 --mount "type=volume,src=$volume,dst=/nix" --mount "type=volume,src=$downloads_volume,dst=/chainman-downloads" "$image" sh -eu -c '
    mkdir -p /nix/store /nix/var
    chown "$1:$2" /nix /nix/store
    [ ! -d /nix/store/.links ] || chown "$1:$2" /nix/store/.links
    if [ "$(stat -c %u:%g /nix/var)" != "$1:$2" ]; then chown -R "$1:$2" /nix/var; fi
    chown "$1:$2" /chainman-downloads
' sh "$container_uid" "$container_gid"
# Local-store writers assume one PID namespace. A single upstream Nix daemon
# owns this volume's store state; isolated project containers are daemon clients.
# Its Unix socket and managed temporary roots are visible through the Nix volume.
if ! "$engine" container inspect "$daemon_name" > /dev/null 2>&1; then
    run --detach --name "$daemon_name" --init --read-only --network bridge \
        --user "$container_uid:$container_gid" --security-opt no-new-privileges --cap-drop ALL \
        --label dev.chainman.store.schema=1 --label "dev.chainman.store.volume=$volume" \
        --mount "type=volume,src=$volume,dst=/nix" \
        --env HOME=/nix/var/nix/chainman-daemon-home --env TMPDIR=/nix/tmp \
        --env 'NIX_CONFIG=build-users-group =
trusted-users = *' \
        "$image" sh -eu -c 'mkdir -p "$HOME" "$TMPDIR"; exec nix-daemon --daemon' \
        > /dev/null 2> "$temporary/daemon-create" || {
        # Container creation is atomic; a concurrent bootstrap can win the name.
        "$engine" container inspect "$daemon_name" > /dev/null 2>&1 || {
            cat "$temporary/daemon-create" >&2
            fail 'Could not create the shared Nix store daemon.'
        }
    }
fi
validate_daemon
"$engine" container start "$daemon_name" > /dev/null
run --rm --user "$container_uid:$container_gid" --label dev.chainman.store.schema=1 --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=volume,src=$volume,dst=/nix" --mount "type=bind,src=$root,dst=$root,readonly" \
    --mount "type=bind,src=$script_dir,dst=/chainman-bootstrap,readonly" --env HOME=/tmp/chainman-home \
    --env 'NIX_CONFIG=build-users-group =
store = daemon' --env NIX_REMOTE=daemon \
    --env "CHAINMAN_BOOTSTRAP_HELPER=/chainman-bootstrap/$(basename -- "$helper")" \
    --env "CHAINMAN_PROJECT_ROOT=$root" --env CHAINMAN_BOOTSTRAP_ACTION=options \
    --env CHAINMAN_REQUEST_ACTION --env CHAINMAN_REQUEST_TASK \
    "$image" sh -eu -c "$container_init" \
    sh sh -eu -c '
        attempt=0
        until nix --extra-experimental-features nix-command store ping > /dev/null 2>&1; do
            attempt=$((attempt + 1))
            [ "$attempt" -lt 30 ] || { echo "Shared Nix store daemon did not become ready." >&2; exit 2; }
            sleep 1
        done
        exec nix --extra-experimental-features "nix-command flakes" eval --impure --raw --expr "$1"
    ' sh "$expression" > "$temporary/options"
if grep -q -- '^--controller$' "$temporary/options"; then
    rm -rf -- "$temporary"
    trap - EXIT HUP INT TERM
    control_dispatch "$@"
fi

# A selected name is passed to the engine without its value in the argument list.
if [ -n "${CHAINMAN_CONTAINER_NETWORK:-}" ]; then
    case "$CHAINMAN_CONTAINER_NETWORK" in *[!a-f0-9]*) fail 'Invalid owned network container identity.' ;; esac
    [ "${#CHAINMAN_CONTAINER_NETWORK}" = 64 ] || fail 'Invalid owned network container identity.'
    printf '%s\n' --network "container:$CHAINMAN_CONTAINER_NETWORK" >> "$temporary/options"
elif [ -n "${CHAINMAN_CONTAINER_BRIDGE:-}" ] && [ "$network_mode" != host ]; then
    bridge_key=${CHAINMAN_CONTAINER_BRIDGE#chainman-}
    case "$bridge_key" in *[!a-f0-9]*) fail 'Invalid owned bridge identity.' ;; esac
    [ "${#bridge_key}" = 24 ] && [ "$CHAINMAN_CONTAINER_BRIDGE" = "chainman-$bridge_key" ] || fail 'Invalid owned bridge identity.'
    printf '%s\n' --network "$CHAINMAN_CONTAINER_BRIDGE" >> "$temporary/options"
    if [ -n "${CHAINMAN_CONTAINER_ALIAS:-}" ]; then
        alias_key=${CHAINMAN_CONTAINER_ALIAS#cm-}
        case "$alias_key" in *[!a-f0-9]*) fail 'Invalid service DNS alias.' ;; esac
        [ "${#alias_key}" = 24 ] && [ "$CHAINMAN_CONTAINER_ALIAS" = "cm-$alias_key" ] || fail 'Invalid service DNS alias.'
        printf '%s\n' --network-alias "$CHAINMAN_CONTAINER_ALIAS" >> "$temporary/options"
    fi
else
    printf '%s\n' --network "$network_mode" >> "$temporary/options"
fi
if [ -n "$platform" ]; then printf '%s\n' --platform "$platform" >> "$temporary/options"; fi
env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' > "$temporary/names"
printf '%s' "${CHAINMAN_FORWARD_ENV:-}" | tr ',' '\n' > "$temporary/patterns"
printf '\n' >> "$temporary/patterns"
if [ -n "${CHAINMAN_CONTAINER_OPTIONS_FILE:-}" ]; then
    # Keep the public options-file allowlist separate from helper-only records.
    while IFS= read -r option; do
        [ -n "$option" ] || continue
        case "$option" in --publish | -p | --mount | -v | --volume | --add-host | --hostname | --label | --name | --network | --platform) ;; *) fail "Unsupported container option: $option" ;; esac
        IFS= read -r value || fail 'Container option lacks a value.'
        # These were normalized before planning and are already emitted above.
        case "$option" in --platform | --network) continue ;; esac
        printf '%s\n%s\n' "$option" "$value" >> "$temporary/options"
    done < "$temporary/extra"
fi
set -- "$image" sh -eu -c "$container_init" sh "$self" "$@"
while IFS= read -r option; do
    [ -n "$option" ] || continue
    IFS= read -r value || fail 'Container option lacks a value.'
    single_line "$value"
    [ -n "$value" ] || fail 'Empty container option value.'
    case "$option" in
        --env-pattern)
            printf '%s\n' "$value" >> "$temporary/patterns"
            continue
            ;;
        --mount-env)
            source_name=${value%%:*}
            remainder=${value#*:}
            target=${remainder%:*}
            access=${remainder##*:}
            case "$source_name" in '' | [!A-Za-z_]* | *[!A-Za-z0-9_]*) fail 'Invalid mount environment variable.' ;; esac
            source=$(printenv "$source_name" && printf '.') || fail "Mount environment variable is unset: $source_name"
            source=${source%.}
            # Remove printenv's delimiter, preserving any newline in the value.
            source=${source%?}
            single_line "$source"
            [ -n "$source" ] || fail "Mount environment variable is empty: $source_name"
            case "$source" in /*) ;; *) source=$root/$source ;; esac
            [ -n "$target" ] || target=$source
            case "$access" in
                ro) value="type=bind,src=$source,dst=$target,readonly" ;;
                rw) value="type=bind,src=$source,dst=$target" ;;
                *) fail 'Invalid mount access mode.' ;;
            esac
            ;;
        --mount)
            case "$value" in type=bind,src=*,dst=*) ;; *) fail 'Only explicit bind mounts are accepted.' ;; esac
            source=${value#type=bind,src=}
            source=${source%%,dst=*}
            target=${value#*,dst=}
            target=${target%%,*}
            suffix=${value#*,dst=}
            suffix=${suffix#"$target"}
            case "$suffix" in '' | ,readonly | ,ro) ;; *) fail 'Unsupported mount option.' ;; esac
            ;;
        -v | --volume)
            source=${value%%:*}
            remainder=${value#*:}
            target=${remainder%%:*}
            case "$remainder" in "$target" | "$target:ro" | "$target:rw") ;; *) fail 'Unsupported volume option.' ;; esac
            ;;
        --network)
            case "$value" in
                host | bridge) ;;
                container:*)
                    [ -n "${CHAINMAN_CONTAINER_NETWORK:-}" ] && [ "$value" = "container:$CHAINMAN_CONTAINER_NETWORK" ] || fail 'Container network is not an owned service namespace.'
                    ;;
                chainman-*)
                    [ -n "${CHAINMAN_CONTAINER_BRIDGE:-}" ] && [ "$value" = "$CHAINMAN_CONTAINER_BRIDGE" ] || fail 'Container network is not an owned private bridge.'
                    ;;
                *) fail 'Container network must be host, bridge or a verified service network.' ;;
            esac
            set -- "$option" "$value" "$@"
            continue
            ;;
        --network-alias)
            [ -n "${CHAINMAN_CONTAINER_ALIAS:-}" ] && [ "$value" = "$CHAINMAN_CONTAINER_ALIAS" ] || fail 'Container alias is not an owned service alias.'
            set -- "$option" "$value" "$@"
            continue
            ;;
        --platform)
            case "$value" in linux/amd64 | linux/arm64) ;; *) fail 'Container platform must be linux/amd64 or linux/arm64.' ;; esac
            set -- "$option" "$value" "$@"
            continue
            ;;
        --publish | -p)
            if [ "$network_mode" != host ]; then set -- "$option" "$value" "$@"; fi
            continue
            ;;
        --add-host | --hostname | --label | --name)
            set -- "$option" "$value" "$@"
            continue
            ;;
        *) fail "Unsupported container option: $option" ;;
    esac
    case "$source" in /*) ;; *) source=$root/$source ;; esac
    case "$source$target" in *,*) fail 'Container mount paths cannot contain commas.' ;; esac
    [ -e "$source" ] && [ ! -S "$source" ] || fail 'Mount source must exist and cannot be a socket.'
    if [ -d "$source" ]; then
        source=$(CDPATH='' cd -P -- "$source" && pwd)
    else source=$(CDPATH='' cd -P -- "$(dirname -- "$source")" && pwd)/$(basename -- "$source"); fi
    case "$source" in / | "${HOME:-/}" | /run | /var/run | */docker.sock | */podman.sock) fail 'Blanket host or socket mounts are not supported.' ;; esac
    case "${HOME:-/}/" in "$source/"*) fail 'Blanket host or socket mounts are not supported.' ;; esac
    case "$target" in /*) ;; *) fail 'Mount target must be absolute.' ;; esac
    case "$target/" in *'/../'* | *'/./'* | *'//'*) fail 'Mount target must be normalized.' ;; esac
    case "$target" in / | /tmp | /nix | /nix/* | /chainman-bootstrap | /chainman-downloads | /chainman-downloads/* | "$root" | "$root/.chainman" | "$root/.chainman/"*) fail 'Mount shadows a bootstrap directory.' ;; esac
    case "$root/" in "$target/"*) fail 'Mount shadows the project through an ancestor.' ;; esac
    if [ "$target" = /tmp/chainman-home ]; then
        case "$source" in "$root"/*) ;; *) fail 'Persistent container HOME must be a project-contained directory.' ;; esac
        [ -d "$source" ] || fail 'Persistent container HOME must be a directory.'
    fi
    # Normalize volume forms so source paths are interpreted on the host.
    readonly=
    case "$value" in *,readonly | *,ro | *:ro) readonly=,readonly ;; esac
    set -- --mount "type=bind,src=$source,dst=$target$readonly" "$@"
done < "$temporary/options"
while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    case "$pattern" in [!A-Za-z_]* | *[!A-Za-z0-9_\*\?]*) fail 'Invalid forwarded environment pattern.' ;; esac
    while IFS= read -r name; do
        # These validated values intentionally select names with shell globs.
        # shellcheck disable=SC2254
        case "$name" in $pattern)
            case "$name" in CHAINMAN_* | TOOLCHAIN_CONTAINER | HOME | PATH | GIT_CONFIG_* | NIX_*) continue ;; esac
            set -- --env "$name" "$@"
            ;;
        esac
    done < "$temporary/names"
done < "$temporary/patterns"

# A linked worktree needs only its Git administrative directory, not its other
# checkout. Identity and signing policy cross the boundary as effective settings.
count=0
policy_unavailable=0
if command -v git > /dev/null 2>&1; then
    git_owner=$(git -C "$root" rev-parse --show-toplevel 2> /dev/null) || git_owner=
    if [ -n "$git_owner" ]; then
        git_owner=$(CDPATH='' cd -- "$git_owner" && pwd -P)
    fi
    if [ "$git_owner" = "$root" ]; then
        admin=$(git -C "$root" rev-parse --path-format=absolute --git-common-dir)
        case "$admin" in "$root"/*) ;; *)
            single_line "$admin"
            case "$admin" in / | "${HOME:-/}" | *,*) fail 'Unsafe Git administrative mount.' ;; esac
            set -- --mount "type=bind,src=$admin,dst=$admin" "$@"
            ;;
        esac
        gitdir=$(git -C "$root" rev-parse --absolute-git-dir)
        case "$gitdir" in "$root"/* | "$admin" | "$admin"/*) ;; *)
            single_line "$gitdir"
            case "$gitdir" in / | "${HOME:-/}" | *,*) fail 'Unsafe Git administrative mount.' ;; esac
            set -- --mount "type=bind,src=$gitdir,dst=$gitdir" "$@"
            ;;
        esac
    fi
    for key in user.name user.email user.signingkey commit.gpgsign gpg.format gpg.program gpg.openpgp.program gpg.ssh.program gpg.ssh.defaultKeyCommand gpg.x509.program; do
        status=0
        if [ "$git_owner" = "$root" ]; then
            value=$(git -C "$root" config --get "$key" 2> /dev/null) || status=$?
        else
            # Preserve system/global policy without borrowing enclosing repo config.
            value=$(git -C "$root" --git-dir=/dev/null config --get "$key" 2> /dev/null) || status=$?
        fi
        case "$status" in
            0)
                set -- --env "GIT_CONFIG_KEY_$count=$key" --env "GIT_CONFIG_VALUE_$count=$value" "$@"
                count=$((count + 1))
                ;;
            1) ;;
            *) policy_unavailable=1 ;;
        esac
    done
elif [ -f "${GIT_CONFIG_GLOBAL:-${HOME:-/}/.gitconfig}" ]; then
    policy_unavailable=1
fi
if [ -n "${CHAINMAN_ARCHIVE:-}" ]; then
    archive=$CHAINMAN_ARCHIVE
    case "$archive" in /*) ;; *) archive=$root/$archive ;; esac
    single_line "$archive"
    case "$archive" in "$root"/*) ;; *)
        [ -f "$archive" ] && [ ! -L "$archive" ] || fail 'Local archive override must be a regular file.'
        set -- --mount "type=bind,src=$archive,dst=$archive,readonly" "$@"
        ;;
    esac
    set -- --env "CHAINMAN_ARCHIVE=$archive" "$@"
fi
case "$self" in "$root"/*) ;; *) set -- --mount "type=bind,src=$script_dir,dst=$script_dir,readonly" "$@" ;; esac
if [ -n "${CHAINMAN_CONTAINER_NAME:-}" ]; then
    case "$CHAINMAN_CONTAINER_NAME" in chainman-[A-Za-z0-9_-]*) ;; *) fail 'Invalid owned container name.' ;; esac
    case "${CHAINMAN_CONTAINER_OWNER:-}" in '' | *[!a-f0-9]*) fail 'Invalid container ownership token.' ;; esac
    [ "${#CHAINMAN_CONTAINER_OWNER}" = 32 ] || fail 'Invalid container ownership token.'
    set -- --name "$CHAINMAN_CONTAINER_NAME" --label "dev.chainman.owner=$CHAINMAN_CONTAINER_OWNER" "$@"
fi
project_mount="type=bind,src=$root,dst=$root"
if [ "$CHAINMAN_REQUEST_ACTION" = _control-export ]; then project_mount=$project_mount,readonly; fi
set -- --rm --init --interactive --user "$container_uid:$container_gid" --label dev.chainman.store.schema=1 --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=volume,src=$volume,dst=/nix" --mount "$project_mount" \
    --mount "type=volume,src=$downloads_volume,dst=/chainman-downloads" --env TOOLCHAIN_DOWNLOAD_CACHE=/chainman-downloads \
    --workdir "$root" \
    --env HOME=/tmp/chainman-home --env CHAINMAN_MODE=container-nix --env CHAINMAN_BOOTSTRAP_CONTAINER=1 \
    --env CHAINMAN_CONTAINER_PLATFORM --env CHAINMAN_CONTAINER_NETWORK_MODE --env CHAINMAN_NIX_VOLUME \
    --env 'NIX_CONFIG=build-users-group =
store = daemon' --env NIX_REMOTE=daemon \
    --env "CHAINMAN_PROJECT_ROOT=$root" --env TOOLCHAIN_CONTAINER=1 --env "GIT_CONFIG_COUNT=$count" \
    --env "TOOLCHAIN_GIT_POLICY_UNAVAILABLE=$policy_unavailable" --env CI --env TERM \
    --env GIT_AUTHOR_NAME --env GIT_AUTHOR_EMAIL --env GIT_COMMITTER_NAME --env GIT_COMMITTER_EMAIL "$@"
if [ -t 0 ] && [ -t 1 ]; then set -- --tty "$@"; fi
rm -rf -- "$temporary"
trap - EXIT HUP INT TERM
if [ "$engine" = podman ]; then exec "$engine" run --userns=keep-id "$@"; fi
exec "$engine" run "$@"
