#!/bin/sh
# Runtime-owned execution machinery, loaded only from the pinned Git tree.
# Child shell programs expand their own positional and environment values.
# shellcheck disable=SC2016
set -eu
if [ "${CHAINMAN_TIMING:-0}" = 1 ] && { [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ] || [ -z "${CHAINMAN_TIMING_BOOTSTRAP_STARTED:-}" ]; }; then
    CHAINMAN_TIMING_BOOTSTRAP_STARTED=$(date +%s)
    export CHAINMAN_TIMING_BOOTSTRAP_STARTED
fi
fail() {
    printf 'Chainman bootstrap: %s\n' "$*" >&2
    exit 2
}
single_line() {
    case "$1" in *'
'* | *"$(printf '\r')"*) fail 'Newlines are not supported in bootstrap paths or options.' ;; esac
}
develop_runtime() {
    develop_action=$1
    shift
    set -- "$nix_bin" --extra-experimental-features 'nix-command flakes' develop "path:$store/nix#bootstrap" --no-write-lock-file --command "$@"
    # The primary nixpkgs input no longer supplies Intel macOS Bash. Make the
    # compatibility input's Bash available before Nix executes its shell script.
    if [ "$(uname -s)-$(uname -m)" = Darwin-x86_64 ]; then
        set -- "$nix_bin" --extra-experimental-features 'nix-command flakes' shell "path:$store/nix#bash" --no-write-lock-file --command "$@"
    fi
    if [ "$develop_action" = exec ]; then exec "$@"; else lifetime_run "$@"; fi
}
script_dir=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
# shellcheck source=bootstrap/lifetime.sh
. "$script_dir/lifetime.sh"
self=$script_dir/$(basename -- "$0")
root=$(CDPATH='' cd -P -- "${CHAINMAN_PROJECT_ROOT:-$script_dir/..}" && pwd)
single_line "$root"
# Transaction launchers carry frozen entry authority outside the writable tree.
# A nested launch inherits it, but it applies only to its declared candidate.
if [ -f "$script_dir/authority-root" ]; then CHAINMAN_ENTRY_AUTHORITY=$script_dir; fi
authority=${CHAINMAN_ENTRY_AUTHORITY:-$root}
if [ "$authority" != "$root" ]; then
    single_line "$authority"
    case "$authority" in /*) ;; *) fail 'Entry authority must be absolute.' ;; esac
    [ -f "$authority/authority-root" ] && [ ! -L "$authority/authority-root" ] || fail 'Missing regular entry authority.'
    IFS= read -r authority_project < "$authority/authority-root"
    if [ "$authority_project" = "$root" ]; then
        case "$authority/" in "$root/"*) fail 'Entry authority must be outside the candidate.' ;; esac
        case "$root/" in "$authority/"*) fail 'Entry authority cannot contain the candidate.' ;; esac
        CHAINMAN_ENTRY_AUTHORITY=$authority
        export CHAINMAN_ENTRY_AUTHORITY
    else
        authority=$root
        unset CHAINMAN_ENTRY_AUTHORITY
    fi
fi
source_root=$(CDPATH='' cd -P -- "$script_dir/.." && pwd)
helper=$script_dir/fetch.nix
export CHAINMAN_SOURCE_ROOT="$source_root"
if [ -n "${CHAINMAN_ACTIVE_PROFILE:-}" ] && [ -n "${CHAINMAN_ROOT:-}" ]; then
    case "${1:-}" in
        exec | shell | script | run | preflight)
            exec "$script_dir/reenter.sh" "$root" --entry "$@"
            ;;
    esac
fi
if [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ]; then
    CHAINMAN_HOST_PLATFORM=$(uname -s)
    export CHAINMAN_HOST_PLATFORM
fi
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
case "$CHAINMAN_REQUEST_ACTION" in
    exec | shell)
        CHAINMAN_REQUEST_PROFILE=
        if [ "${2:-}" = --profile ]; then CHAINMAN_REQUEST_PROFILE=${3:-}; fi
        # Internal compiler/installer entry deliberately carries no profile mounts.
        if [ "${2:-}" = --reuse-operation ]; then CHAINMAN_REQUEST_PROFILE=; fi
        export CHAINMAN_REQUEST_PROFILE
        ;;
    _bootstrap-options | _display-prepare) ;;
    *) unset CHAINMAN_REQUEST_PROFILE ;;
esac
# Do not let a validation container drain the following command's input.
if [ "$CHAINMAN_REQUEST_ACTION" = preflight ]; then
    exec < /dev/null
    CHAINMAN_PREFLIGHT_TASKS=$(
        first=1
        for task in "$@"; do
            if [ "$first" = 1 ]; then first=0; else printf '%s\n' "$task"; fi
        done
    )
    export CHAINMAN_PREFLIGHT_TASKS
fi
control_dispatch() {
    if [ "$1" = services-logs ]; then
        [ "$#" = 1 ] || { [ "$#" = 2 ] && [ "$2" = --follow ]; } || fail 'usage: services-logs [--follow]'
    fi
    if [ "$1" = services-reset ]; then
        [ "$#" = 3 ] && [ "$3" = --discard-data ] || fail 'usage: services-reset TASK --discard-data'
    fi
    case "$1" in
        services-status | services-stop | services-logs) ;;
        *)
            if [ "$mode" = container-nix ] && [ "${CHAINMAN_CONTAINER_NETWORK_MODE:-bridge}" = host ]; then
                fail 'Service workflows use owned network namespaces; the host-network override is for standalone tasks.'
            fi
            ;;
    esac
    case "$1" in
        services-status | services-stop | services-logs) ;;
        *)
            # Setup may produce application data identities used by volume
            # compatibility. Run it before planning, in the ordinary project
            # environment, without the private controller export mount.
            case "$1" in
                run | services-run | services-up | services-reset) control_task=${2:-} ;;
                *) control_task=$1 ;;
            esac
            lifetime_helper "$self" _service-prepare "$control_task" >&2
            ;;
    esac
    # Only the internal export operation mounts this private output directory.
    # It builds verified tooling and emits JSON; no consumer code executes there.
    control_output=$(mktemp -d "${TMPDIR:-/tmp}/chainman-control.XXXXXXXX")
    lifetime_directory=$control_output
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
    CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$control_output/mounts lifetime_helper "$self" _control-export "$control_output" "$control_target" \
        "${XDG_CACHE_HOME:-$HOME/.cache}/chainman/services" "$control_engine" "$self" "$@"
    case "$1" in
        services-status | services-stop | services-logs)
            IFS= read -r control_state < "$control_output/state"
            control_action=${1#services-}
            shift
            "$control_output/chainman-control" "$control_action" "$control_state" "$@"
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
    update_resume=0
    case "${1:-}" in
        resume=*)
            [ "$#" = 1 ] || fail 'Resume accepts only resume=/path/to/transaction.'
            update_output=${1#resume=}
            [ -d "$update_output/control" ] && [ ! -L "$update_output" ] || fail 'Missing real update transaction.'
            update_output=$(CDPATH='' cd -P -- "$update_output" && pwd)
            case "$update_output" in "$update_cache"/candidate.*) ;; *) fail 'Resume must select a retained update transaction.' ;; esac
            update_resume=1
            ;;
        *) update_output=$(mktemp -d "$update_cache/candidate.XXXXXXXX") ;;
    esac
    update_output=$(CDPATH='' cd -P -- "$update_output" && pwd)
    trap 'printf "Chainman: candidate preserved at %s/candidate; resume with: just deps-update resume=%s\n" "$update_output" "$update_output" >&2' EXIT
    if [ "$update_resume" = 0 ]; then
        mkdir "$update_output/candidate" "$update_output/control"
    fi
    printf '%s\n%s\n' --mount "type=bind,src=$update_output,dst=$update_output" > "$update_output/control/mounts"
    : > "$update_output/control/candidate-mounts"
    if [ "$update_resume" = 1 ]; then
        CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
            "$self" _update-resume "$update_output" >&2
        set --
        while IFS= read -r update_argument; do set -- "$@" "$update_argument"; done < "$update_output/control/resume-arguments"
        if [ -f "$update_output/control/retry-runtime" ]; then
            IFS= read -r update_retry_runtime < "$update_output/control/retry-runtime"
            if [ "$update_retry_runtime" = yes ]; then update_resume=0; fi
        fi
    else
        CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
            "$self" _update-prepare "$update_output" "$@" >&2
    fi
    if [ -f "$update_output/control/help" ]; then
        rm -rf -- "$update_output"
        trap - EXIT
        exit 0
    fi
    workspace_transactions=$update_output/candidate/.chainman-workspace-transactions
    [ -d "$workspace_transactions" ] && [ ! -L "$workspace_transactions" ] || fail 'Workspace transaction root must be a real candidate directory.'
    [ "$(CDPATH='' cd -P -- "$workspace_transactions" && pwd)" = "$workspace_transactions" ] || fail 'Workspace transaction root must not contain symlinks.'
    IFS= read -r update_at < "$update_output/control/at"
    update_launcher=$update_output/original-bootstrap/chainman.sh
    if [ "$update_resume" = 0 ]; then
        CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
            CHAINMAN_PROJECT_ROOT=$root "$update_launcher" _update-runtime "$update_output" >&2
    fi
    update_resolver=$update_launcher
    if [ -f "$update_output/resolution-bootstrap/chainman.sh" ]; then
        update_resolver=$update_output/resolution-bootstrap/chainman.sh
    fi
    if [ "$update_resume" = 0 ]; then
        update_candidate "$update_resolver" _update-resolve "$update_at" "$@" >&2
    fi
    update_candidate "$update_resolver" _update-tasks "$@" > "$update_output/control/tasks"
    while IFS= read -r update_task; do
        CHAINMAN_SETUP=auto CHAINMAN_UPDATE_ACTIVE=1 update_candidate "$update_resolver" run "$update_task" < /dev/null >&2
    done < "$update_output/control/tasks"
    if [ "$update_resume" = 1 ] || [ -s "$update_output/control/tasks" ]; then
        update_candidate "$update_resolver" _update-reaudit "$update_at" "$@" >&2
    fi
    CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$update_output/control/mounts \
        CHAINMAN_PROJECT_ROOT=$root "$update_launcher" _update-inspect "$update_output" >&2
    IFS= read -r update_changed < "$update_output/control/changed"
    if [ "$update_changed" = yes ]; then
        while IFS= read -r update_action && IFS= read -r update_task; do
            CHAINMAN_SETUP=auto CHAINMAN_UPDATE_ACTIVE=1 update_candidate \
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
    exec env GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_COUNT=4 \
        GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false GIT_CONFIG_KEY_1=core.hooksPath GIT_CONFIG_VALUE_1=/dev/null \
        GIT_CONFIG_KEY_2=gc.auto GIT_CONFIG_VALUE_2=0 GIT_CONFIG_KEY_3=maintenance.auto GIT_CONFIG_VALUE_3=false \
        GIT_TERMINAL_PROMPT=0 GIT_OPTIONAL_LOCKS=0 CHAINMAN_CONTAINER_OPTIONS_FILE="$update_output/control/candidate-mounts" \
        CHAINMAN_WORKSPACE_TRANSACTION_ROOT="$workspace_transactions" CHAINMAN_PROJECT_ROOT="$update_output/candidate" "$@"
)
expression='import (builtins.toPath (builtins.getEnv "CHAINMAN_BOOTSTRAP_HELPER")) {
    root = builtins.getEnv "CHAINMAN_PROJECT_ROOT";
    authority = let selected = builtins.getEnv "CHAINMAN_ENTRY_AUTHORITY";
      in if selected == "" then builtins.getEnv "CHAINMAN_PROJECT_ROOT" else selected;
    source = builtins.getEnv "CHAINMAN_SOURCE_ROOT";
    action = builtins.getEnv "CHAINMAN_BOOTSTRAP_ACTION";
}'
mode=${CHAINMAN_MODE:-container-nix}
case "$mode" in host | host-nix | container-nix) ;; *) fail 'CHAINMAN_MODE must be host, host-nix or container-nix.' ;; esac
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
if [ -z "${CHAINMAN_WORKSPACE_TRANSACTION_ROOT:-}" ]; then
    CHAINMAN_WORKSPACE_TRANSACTION_ROOT=$root/.chainman/workspace-transactions
fi
case "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" in
    "$root/.chainman/workspace-transactions" | "$root/.chainman-workspace-transactions") ;;
    *) fail 'Workspace transaction root must be owned by the selected project.' ;;
esac
[ ! -L "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" ] || fail 'Workspace transaction root must not be a symlink.'
if [ -e "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" ]; then
    [ -d "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" ] || fail 'Workspace transaction root must be a directory.'
    [ "$(CDPATH='' cd -P -- "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" && pwd)" = "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" ] || fail 'Workspace transaction root must not contain symlinks.'
elif [ "$CHAINMAN_WORKSPACE_TRANSACTION_ROOT" != "$root/.chainman/workspace-transactions" ]; then
    fail 'Candidate workspace transaction root must already exist.'
fi
export CHAINMAN_WORKSPACE_TRANSACTION_ROOT

if [ "$CHAINMAN_REQUEST_ACTION" = recipe ]; then
    shift
    [ "$#" -ge 1 ] || fail 'usage: just chainman recipe NAME [ARGUMENTS...]'
    recipe_name=$1
    shift
    recipe_plan=$("$self" _recipe-plan "$recipe_name")
    exec sh -eu -c "$recipe_plan" chainman "$self" "$@"
fi

if [ "$mode" = host ]; then
    IFS= read -r revision < "$authority/chainman.lock"
    [ "$revision" = "${CHAINMAN_SOURCE_REVISION:-}" ] || fail 'Pin changed after verified runtime selection.'
    command -v python3 > /dev/null 2>&1 || fail 'Host execution requires caller-installed Python 3.12+.'
    python3 -E -s -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || fail 'Host execution requires Python 3.12+.'
    export CHAINMAN_MODE=host CHAINMAN_ACTIVE_MODE=host TOOLCHAIN_MODE=host TOOLCHAIN_CONTAINER=0
    export CHAINMAN_ROOT="$root" CHAINMAN_RUNTIME="$source_root"
    exec python3 -E -s -B "$source_root/scripts/chainman.py" --root "$root" "$@"
fi

if [ "$CHAINMAN_REQUEST_ACTION" = format ] && [ "${2:-}" = --staged ]; then
    [ "$#" = 2 ] || fail 'usage: format --staged'
    set -- format-staged
    CHAINMAN_REQUEST_ACTION=format-staged
    export CHAINMAN_REQUEST_ACTION
fi

# Host Git owns repository hooks; managed phases never translate host policy.
case "$CHAINMAN_REQUEST_ACTION" in
    hooks | format-staged | trojan-source)
        lifetime_grace=15
        lifetime_is_helper=1
        lifetime_run sh "$script_dir/hooks.sh" "$self" "$root" "$@"
        exit $?
        ;;
    setup)
        if [ "$#" = 1 ] && [ -z "${CHAINMAN_UPDATE_ACTIVE:-}" ]; then
            lifetime_grace=15
            lifetime_is_helper=1
            lifetime_run sh "$script_dir/hooks.sh" "$self" "$root" "$@"
            exit $?
        fi
        ;;
esac

case "$CHAINMAN_REQUEST_ACTION" in
    deps-update | chainman-update | format)
        [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ] || fail 'Start updates through the host launcher so candidate verification can control its own services.'
        [ -z "${CHAINMAN_UPDATE_ACTIVE:-}" ] || fail 'An update hook must not recursively start another update.'
        shift
        case "${1:-}" in resume=*) update_dispatch "$@" ;; esac
        if [ "$CHAINMAN_REQUEST_ACTION" = chainman-update ]; then set -- --only-chainman "$@"; fi
        if [ "$CHAINMAN_REQUEST_ACTION" = format ]; then
            for format_option in "$@"; do
                if [ "$format_option" = commit=off ]; then
                    [ "$#" = 1 ] || fail 'In-place format accepts only commit=off.'
                    format_tasks=$("$self" _format-plan)
                    while IFS= read -r format_task; do
                        "$self" run "$format_task"
                    done << EOF
$format_tasks
EOF
                    exit 0
                fi
            done
            set -- --format "$@"
        fi
        update_dispatch "$@"
        ;;
esac

# Piped container commands still need the caller's terminal for setup consent.
# A native hook already supplies this channel. Interactive sessions use their
# attached terminal, but credential-free setup preflight temporarily detaches
# stdin and needs the same host channel before the final container starts.
if [ "$mode" = container-nix ] && [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ] \
    && [ "${CHAINMAN_SETUP:-prompt}" = prompt ] && [ -z "${CHAINMAN_SETUP_CHANNEL:-}" ] \
    && { [ ! -t 0 ] || [ ! -t 1 ]; } && (: < /dev/tty) 2> /dev/null; then
    case "$CHAINMAN_REQUEST_ACTION" in
        _transport-prepare | _service-prepare | [!_]*)
            consent_output=$(mktemp -d "${TMPDIR:-/tmp}/chainman-consent-export.XXXXXXXX")
            lifetime_directory=$consent_output
            consent_output=$(CDPATH='' cd -P -- "$consent_output" && pwd)
            lifetime_directory=$consent_output
            case "$(uname -s):$(uname -m)" in
                Linux:aarch64 | Linux:arm64) consent_target=linux-arm64 ;;
                Linux:x86_64) consent_target=linux-amd64 ;;
                Darwin:arm64) consent_target=darwin-arm64 ;;
                Darwin:x86_64) consent_target=darwin-amd64 ;;
                *) fail 'Unsupported native setup-consent platform.' ;;
            esac
            printf '%s\n%s\n' --mount "type=bind,src=$consent_output,dst=$consent_output" > "$consent_output/mounts"
            CHAINMAN_SETUP=error CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$consent_output/mounts \
                lifetime_helper "$self" _consent-export "$consent_output" "$consent_target" < /dev/null >&2
            lifetime_grace=10
            lifetime_run "$consent_output/chainman-control" setup-consent "$self" "$@"
            exit $?
            ;;
    esac
fi

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
    host_temporary=$(mktemp -d "${TMPDIR:-/tmp}/chainman-host-bootstrap.XXXXXXXX")
    lifetime_directory=$host_temporary
    # Probe the evaluator, not a vendor-specific --version display string.
    lifetime_run "$nix_bin" --extra-experimental-features 'nix-command flakes' eval --raw --expr '
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
            lifetime_run "$nix_bin" --extra-experimental-features 'nix-command flakes' eval --impure --raw --expr "$expression"
    }
    IFS= read -r revision < "$authority/chainman.lock"
    case "$revision" in '' | *[!0-9a-f]*) fail 'Invalid Git revision pin.' ;; esac
    [ "${#revision}" = 40 ] || fail 'Expected a full Git revision pin.'
    [ "$revision" = "${CHAINMAN_SOURCE_REVISION:-}" ] || fail 'Pin changed after verified runtime selection.'
    # Import the verified Git export and register a normal Nix GC root in the same evaluator process.
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
    runtime_root=$runtime_roots/$revision
    [ ! -e "$runtime_root" ] || [ -L "$runtime_root" ] || fail 'Runtime GC root must be a symlink.'
    fetch_runtime() {
        CHAINMAN_BOOTSTRAP_HELPER=$helper CHAINMAN_PROJECT_ROOT=$root CHAINMAN_BOOTSTRAP_ACTION=fetch \
            lifetime_run "$nix_bin" --extra-experimental-features 'nix-command flakes' build --impure --expr "$expression" \
            "$@" --print-out-paths
    }
    # A content-keyed root is immutable. Replacing it on every entry makes Nix's
    # PID-based temporary symlink names collide across container PID namespaces.
    # Still evaluate the verified Git export on every entry, including warm starts.
    if [ -L "$runtime_root" ]; then
        fetch_runtime --no-link > "$host_temporary/store"
        store=$(cat "$host_temporary/store")
    elif fetch_runtime --out-link "$runtime_root" > "$host_temporary/store"; then
        store=$(cat "$host_temporary/store")
    else
        # Another first writer may have installed the same root. Re-evaluate the
        # source and require the exact link below; never accept a failed fetch
        # merely because some old source is cached.
        [ -L "$runtime_root" ] || fail 'Runtime GC root registration failed.'
        fetch_runtime --no-link > "$host_temporary/store"
        store=$(cat "$host_temporary/store")
    fi
    [ "$(readlink "$runtime_root")" = "$store" ] || fail 'Runtime GC root does not match the verified source.'
    # A process killed between creating the link and registering its indirect
    # root must not leave a permanently unregistered warm-cache entry.
    # Concurrent Nix root inventories can race while marking stale temporary
    # roots. An unavailable inventory leaves registration unconfirmed, just like
    # a missing entry. Re-fetch and register successfully before dispatching.
    if ! lifetime_run "$CHAINMAN_RUNTIME_NIX_BIN/nix-store" --query --roots "$store" > "$host_temporary/roots" \
        || ! grep -F -x -q -- "$runtime_root -> $store" "$host_temporary/roots"; then
        fetch_runtime --out-link "$runtime_root" > "$host_temporary/store"
        store=$(cat "$host_temporary/store")
    fi
    lifetime_run "$nix_bin" --extra-experimental-features nix-command hash path "$source_root" > "$host_temporary/hash"
    expected=$(cat "$host_temporary/hash")
    lifetime_run "$nix_bin" --extra-experimental-features nix-command hash path "$store" > "$host_temporary/hash"
    actual=$(cat "$host_temporary/hash")
    [ "$actual" = "$expected" ] || fail 'Runtime store differs from the verified Git source.'
    if [ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ]; then
        nix_eval schema > "$host_temporary/schema"
        if [ "$(cat "$host_temporary/schema")" = 3 ]; then
            develop_runtime run python3 -B "$store/scripts/bootstrap_plan.py" "$root" route > "$host_temporary/route"
        else
            nix_eval route > "$host_temporary/route"
        fi
        route=$(cat "$host_temporary/route")
        rm -rf -- "$host_temporary"
        lifetime_directory=
        if [ "$route" = 1 ]; then control_dispatch "$@"; fi
    fi
    rm -rf -- "$host_temporary"
    lifetime_directory=
    export CHAINMAN_MODE="$mode" CHAINMAN_ACTIVE_MODE="$mode"
    # Bootstrap entry replaces an external project shell. Its old profile token no
    # longer describes PATH, even when the project inputs themselves are unchanged.
    unset IN_NIX_SHELL CHAINMAN_ACTIVE_PROFILE CHAINMAN_ACTIVE_FINGERPRINT
    develop_runtime exec python3 -c '
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
temporary=$(mktemp -d "${TMPDIR:-/tmp}/chainman-bootstrap.XXXXXXXX")
lifetime_directory=$temporary
image=docker.io/nixos/nix:2.33.3@sha256:c2f7db70a432d00c6759af108ff4fbc74a4c00e2d4517162e72338e7b9449c1f
uid=$(id -u)
gid=$(id -g)
container_uid=$uid
container_gid=$gid
if [ "$engine" = docker ]; then
    lifetime_run "$engine" info --format '{{range .SecurityOptions}}{{println .}}{{end}}' > "$temporary/identity" || fail 'Cannot determine Docker daemon identity mapping.'
    security_options=$(cat "$temporary/identity")
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
    if [ "$engine" = podman ]; then lifetime_run "$engine" run --userns=keep-id "$@"; else lifetime_run "$engine" run "$@"; fi
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
    expected_image=$image
    expected_pid=''
    capability_fields='{{.HostConfig.CapDrop}}'
    expected_capabilities='[ALL]'
    if [ "$engine" = podman ]; then
        # Podman canonicalizes tag@digest to digest and resolves --cap-drop ALL
        # into its capability sets. Compare its resulting empty sets directly.
        expected_image=${image%@*}
        expected_image=${expected_image%:*}@${image#*@}
        expected_pid=private
        capability_fields='{{.EffectiveCaps}} {{.BoundingCaps}}'
        expected_capabilities='[] []'
    fi
    lifetime_run "$engine" container inspect --format '{{index .Config.Labels "dev.chainman.store.schema"}}
{{index .Config.Labels "dev.chainman.store.gc"}}
{{index .Config.Labels "dev.chainman.store.volume"}}
{{.Config.Image}}
{{.Config.User}}
{{.HostConfig.Privileged}}
{{.HostConfig.ReadonlyRootfs}}
'"$capability_fields"'
{{.HostConfig.SecurityOpt}}
{{range .Mounts}}{{.Type}}:{{.Name}}:{{.Destination}}:{{.RW}};{{end}}
{{len .HostConfig.PortBindings}}
{{.HostConfig.NetworkMode}}
pid={{.HostConfig.PidMode}}' "$daemon_name" > "$temporary/identity"
    daemon_identity=$(cat "$temporary/identity")
    expected_identity="1
1
$volume
$expected_image
$container_uid:$container_gid
false
true
$expected_capabilities
[no-new-privileges]
volume:$volume:/nix:true;
0
bridge
pid=$expected_pid"
    [ "$daemon_identity" = "$expected_identity" ] || fail "Nix store daemon $daemon_name has incompatible identity or isolation (including its GC policy). Stop its clients and remove that daemon container before changing its configuration; retain the Nix volume."
}
if lifetime_run "$engine" container inspect "$daemon_name" > /dev/null 2>&1; then validate_daemon; fi
if [ "$engine" = podman ]; then
    # Podman 4.x has no Docker-compatible .Label template accessor. Its negative
    # label filter selects the same incompatible clients in one engine snapshot.
    lifetime_run "$engine" ps --filter "volume=$volume" --filter 'label!=dev.chainman.store.schema=1' --format '{{.ID}}' > "$temporary/clients"
else
    lifetime_run "$engine" ps --filter "volume=$volume" --format '{{.ID}} {{.Label "dev.chainman.store.schema"}}' > "$temporary/clients"
fi
volume_clients=$(cat "$temporary/clients")
while IFS= read -r client; do
    case "$client" in '' | *' 1') ;; *) fail 'Stop existing containers using this Nix volume before migrating from independent local-store writers to the shared daemon.' ;; esac
done << EOF
$volume_clients
EOF
# Only the named Nix store is prepared as root. No host directory is mounted here.
run --rm --user 0:0 --label dev.chainman.store.schema=1 --mount "type=volume,src=$volume,dst=/nix" --mount "type=volume,src=$downloads_volume,dst=/chainman-downloads" "$image" sh -eu -c '
    mkdir -p /nix/store /nix/var
    chown "$1:$2" /nix /nix/store
    # An empty engine volume inherits root-owned paths from the upstream image.
    # Old image paths become garbage after upgrades; a mapped-user daemon cannot
    # chmod/delete them unless ownership was normalized too. Inspect top-level
    # entries on warm starts, traversing the store only for initialization/repair.
    # Never dereference store symlinks into other locations.
    if [ -n "$(find /nix/store -mindepth 1 -maxdepth 1 ! -user "$1" -print -quit)" ]; then
        chown -hR "$1:$2" /nix/store
    fi
    if [ "$(stat -c %u:%g /nix/var)" != "$1:$2" ]; then chown -R "$1:$2" /nix/var; fi
    chown "$1:$2" /chainman-downloads
' sh "$container_uid" "$container_gid"
# Local-store writers assume one PID namespace. A single upstream Nix daemon
# owns this volume's store state; isolated project containers are daemon clients.
# Its Unix socket and managed temporary roots are visible through the Nix volume.
if ! lifetime_run "$engine" container inspect "$daemon_name" > /dev/null 2>&1; then
    run --detach --name "$daemon_name" --init --read-only --network bridge \
        --user "$container_uid:$container_gid" --security-opt no-new-privileges --cap-drop ALL \
        --label dev.chainman.store.schema=1 --label "dev.chainman.store.volume=$volume" \
        --label dev.chainman.store.gc=1 \
        --mount "type=volume,src=$volume,dst=/nix" \
        --env HOME=/nix/var/nix/chainman-daemon-home --env TMPDIR=/nix/tmp \
        --env 'NIX_CONFIG=build-users-group =
trusted-users = *
min-free = 8589934592
max-free = 17179869184' \
        "$image" sh -eu -c 'mkdir -p "$HOME" "$TMPDIR"; exec nix-daemon --daemon' \
        > /dev/null 2> "$temporary/daemon-create" || {
        # Container creation is atomic; a concurrent bootstrap can win the name.
        lifetime_run "$engine" container inspect "$daemon_name" > /dev/null 2>&1 || {
            cat "$temporary/daemon-create" >&2
            fail 'Could not create the shared Nix store daemon.'
        }
    }
fi
validate_daemon
lifetime_run "$engine" container start "$daemon_name" > /dev/null
plan_options() {
    # The verified planner reads declared host inputs as data, never as its own
    # execution environment. Do not expose this snapshot to project commands.
    (
        umask 077
        env -0 > "$temporary/host-environment"
    )
    set --
    if [ "$authority" != "$root" ]; then
        set -- --mount "type=bind,src=$authority,dst=$authority,readonly" --env "CHAINMAN_ENTRY_AUTHORITY=$authority" "$@"
    fi
    if [ "${display_prepare:-0}" = 1 ]; then
        set -- --mount "type=bind,src=$xauthority,dst=/chainman-x11-source,readonly" \
            --mount "type=bind,src=$temporary/x11,dst=/chainman-x11-output" \
            --env DISPLAY --env "CHAINMAN_X11_HOSTNAME=$(uname -n)" --env CHAINMAN_DISPLAY_PREPARE=1 "$@"
    fi
    run --rm --user "$container_uid:$container_gid" --label dev.chainman.store.schema=1 --security-opt no-new-privileges --cap-drop ALL \
        --mount "type=volume,src=$volume,dst=/nix" --mount "type=bind,src=$root,dst=$root,readonly" \
        --mount "type=bind,src=$source_root,dst=$source_root,readonly" --env HOME=/tmp/chainman-home \
        --mount "type=bind,src=$temporary/host-environment,dst=$temporary/host-environment,readonly" --env "CHAINMAN_BOOTSTRAP_INPUTS=$temporary" \
        --env 'NIX_CONFIG=build-users-group =
store = daemon' --env NIX_REMOTE=daemon \
        --env "CHAINMAN_BOOTSTRAP_HELPER=$helper" --env "CHAINMAN_SOURCE_ROOT=$source_root" --env CHAINMAN_SOURCE_REVISION \
        --env "CHAINMAN_PROJECT_ROOT=$root" --env CHAINMAN_BOOTSTRAP_ACTION=options \
        --env CHAINMAN_REQUEST_ACTION --env CHAINMAN_REQUEST_TASK --env CHAINMAN_REQUEST_PROFILE --env CHAINMAN_HOST_PLATFORM --env CHAINMAN_PREFLIGHT_TASKS \
        "$@" "$image" sh -eu -c "$container_init" \
        sh sh -eu -c '
        attempt=0
        until nix --extra-experimental-features nix-command store ping > /dev/null 2>&1; do
            attempt=$((attempt + 1))
            [ "$attempt" -lt 30 ] || { echo "Shared Nix store daemon did not become ready." >&2; exit 2; }
            sleep 1
        done
        if [ "${CHAINMAN_DISPLAY_PREPARE:-0}" = 1 ]; then
            exec env CHAINMAN_BOOTSTRAP_CONTAINER=1 CHAINMAN_MODE=container-nix "$2" _display-prepare
        fi
        schema=$(CHAINMAN_BOOTSTRAP_ACTION=schema nix --extra-experimental-features "nix-command flakes" eval --impure --raw --expr "$1")
        if [ "$schema" = 3 ]; then
            # Reuse the normal verified runtime/GC-root path in this read-only
            # planning container. No consumer code or declared mounts run here.
            exec env CHAINMAN_BOOTSTRAP_CONTAINER=1 CHAINMAN_MODE=container-nix \
                "$2" _bootstrap-options "$CHAINMAN_REQUEST_ACTION" "$CHAINMAN_REQUEST_TASK"
        fi
        exec nix --extra-experimental-features "nix-command flakes" eval --impure --raw --expr "$1"
    ' sh "$expression" "$self"
}
plan_options > "$temporary/options"
if grep -q -e '^--display$' -e '^--display-check$' "$temporary/options"; then
    [ "$CHAINMAN_HOST_PLATFORM" = Linux ] || fail 'X11 container transport requires a Linux host; select host-nix for a native display.'
    display=${DISPLAY:?X11 transport requires DISPLAY}
    case "$display" in :* | unix/:* | unix:*) ;; *) fail 'X11 transport requires a local DISPLAY such as :0.' ;; esac
    display_number=${display#*:}
    display_number=${display_number%%.*}
    case "$display_number" in '' | *[!0-9]*) fail 'Invalid X11 display number.' ;; esac
    x_socket=/tmp/.X11-unix/X$display_number
    [ -S "$x_socket" ] || fail "Missing X11 display socket: $x_socket"
    xauthority=${XAUTHORITY:-${HOME:?}/.Xauthority}
    case "$xauthority" in /*) ;; *) xauthority=$root/$xauthority ;; esac
    single_line "$xauthority"
    case "$xauthority" in *,*) fail 'X11 authority path cannot contain commas.' ;; esac
    [ -f "$xauthority" ] && [ ! -L "$xauthority" ] || fail 'X11 transport requires a regular XAUTHORITY file (or ~/.Xauthority).'
    mkdir -m 700 "$temporary/x11"
    display_prepare=1 plan_options > /dev/null
fi
if grep -q -- '^--controller$' "$temporary/options"; then
    rm -rf -- "$temporary"
    lifetime_directory=
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
# Freeze the request before replacing positional arguments with engine options.
prepare_action=$CHAINMAN_REQUEST_ACTION
prepare_task=$CHAINMAN_REQUEST_TASK
prepare_profile=
transport_readiness=
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
        --transport-declaration)
            set -- --env "CHAINMAN_ACTIVE_TRANSPORT=$value" "$@"
            continue
            ;;
        --transport-readiness)
            transport_readiness=error
            continue
            ;;
        --transport-prepare)
            prepare_profile=$value
            continue
            ;;
        --display-check) continue ;;
        --display)
            set -- --mount "type=bind,src=$x_socket,dst=$x_socket,readonly" \
                --mount "type=bind,src=$temporary/x11/authority,dst=/chainman-x11-authority,readonly" "$@"
            continue
            ;;
        --mount-env | --mount-env-optional)
            source_name=${value%%:*}
            remainder=${value#*:}
            target=${remainder%:*}
            access=${remainder##*:}
            case "$source_name" in '' | [!A-Za-z_]* | *[!A-Za-z0-9_]*) fail 'Invalid mount environment variable.' ;; esac
            if ! source=$(printenv "$source_name" && printf '.'); then
                [ "$option" != --mount-env-optional ] || continue
                fail "Mount environment variable is unset: $source_name"
            fi
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
        --mount | --mount-optional)
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
            printf '%s\n' "$value" | grep -Eq '^127[.]0[.]0[.]1:[0-9]{1,5}:[0-9]{1,5}(/tcp|/udp)?$' || fail 'Published ports require loopback 127.0.0.1:HOST:CONTAINER[/tcp|/udp].'
            numbers=${value#127.0.0.1:}
            numbers=${numbers%/*}
            host_port=${numbers%%:*}
            container_port=${numbers#*:}
            [ "$host_port" -ge 1 ] && [ "$host_port" -le 65535 ] && [ "$container_port" -ge 1 ] && [ "$container_port" -le 65535 ] || fail 'Published ports must be integers between 1 and 65535.'
            protocol=${value##*/}
            [ "$protocol" != "$value" ] || protocol=tcp
            host_port=$(printf '%s' "$host_port" | sed 's/^0*//')
            container_port=$(printf '%s' "$container_port" | sed 's/^0*//')
            value=127.0.0.1:$host_port:$container_port/$protocol
            binding=127.0.0.1:$host_port/$protocol
            existing=
            if [ -f "$temporary/ports" ]; then
                while IFS= read -r old_binding && IFS= read -r old_port; do
                    [ "$old_binding" != "$binding" ] || [ "$old_port" = "$value" ] || fail "Conflicting port binding: $binding"
                    if [ "$old_port" = "$value" ]; then existing=yes; fi
                done < "$temporary/ports"
            fi
            [ -z "$existing" ] || continue
            printf '%s\n%s\n' "$binding" "$value" >> "$temporary/ports"
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
    if [ ! -e "$source" ] && [ ! -L "$source" ]; then
        case "$option" in --mount-optional | --mount-env-optional) continue ;; esac
    fi
    { [ -d "$source" ] || { [ -f "$source" ] && [ ! -L "$source" ]; }; } || fail 'Mount source must be a directory or regular nonsymlink file.'
    if [ -d "$source" ]; then
        source=$(CDPATH='' cd -P -- "$source" && pwd)
    else source=$(CDPATH='' cd -P -- "$(dirname -- "$source")" && pwd)/$(basename -- "$source"); fi
    case "$source" in / | "${HOME:-/}" | /run | /var/run | */docker.sock | */podman.sock) fail 'Blanket host or socket mounts are not supported.' ;; esac
    case "${HOME:-/}/" in "$source/"*) fail 'Blanket host or socket mounts are not supported.' ;; esac
    case "$target" in /*) ;; *) fail 'Mount target must be absolute.' ;; esac
    case "$target/" in *'/../'* | *'/./'* | *'//'*) fail 'Mount target must be normalized.' ;; esac
    case "$target" in / | /tmp | /nix | /nix/* | /chainman-bootstrap | /chainman-x11-authority | /chainman-x11-source | /chainman-x11-output | /chainman-inspection-options | /chainman-downloads | /chainman-downloads/* | "$root" | "$root/.chainman" | "$root/.chainman/"*) fail 'Mount shadows a bootstrap directory.' ;; esac
    case "$root/" in "$target/"*) fail 'Mount shadows the project through an ancestor.' ;; esac
    if [ "$authority" != "$root" ]; then
        case "$target/" in "$authority/"*) fail 'Mount shadows update entry authority.' ;; esac
        case "$authority/" in "$target/"*) fail 'Mount shadows update entry authority.' ;; esac
    fi
    if [ -d "$temporary/x11" ]; then
        case "$target" in /tmp/.X11-unix | "$x_socket") fail 'Mount conflicts with the selected display socket.' ;; esac
    fi
    if [ "$target" = /tmp/chainman-home ]; then
        case "$source" in "$root"/*) ;; *) fail 'Persistent container HOME must be a project-contained directory.' ;; esac
        [ -d "$source" ] || fail 'Persistent container HOME must be a directory.'
    fi
    # Reject conflicts after host environment paths and option files resolve.
    # Normalize volume forms so source paths are interpreted on the host.
    readonly=
    case "$value" in *,readonly | *,ro | *:ro) readonly=,readonly ;; esac
    existing=
    if [ -f "$temporary/mounts" ]; then
        while IFS= read -r old_target && IFS= read -r old_source; do
            if [ "$old_target" = "$target" ]; then
                existing=$old_source
                break
            fi
        done < "$temporary/mounts"
    fi
    if [ -n "$existing" ]; then
        [ "$existing" = "$source$readonly" ] || fail "Conflicting mount target: $target"
        continue
    fi
    printf '%s\n%s\n' "$target" "$source$readonly" >> "$temporary/mounts"
    set -- --mount "type=bind,src=$source,dst=$target$readonly" "$@"
done < "$temporary/options"
while IFS= read -r pattern; do
    [ -n "$pattern" ] || continue
    case "$pattern" in [!A-Za-z_]* | *[!A-Za-z0-9_\*\?]*) fail 'Invalid forwarded environment pattern.' ;; esac
    while IFS= read -r name; do
        # These validated values intentionally select names with shell globs.
        # shellcheck disable=SC2254
        case "$name" in $pattern)
            case "$name" in DISPLAY | XAUTHORITY)
                if [ -d "$temporary/x11" ]; then continue; fi
                ;;
            esac
            case "$name" in CHAINMAN_* | TOOLCHAIN_CONTAINER | HOME | PATH | GIT_CONFIG_* | NIX_*) continue ;; esac
            set -- --env "$name" "$@"
            ;;
        esac
    done < "$temporary/names"
done < "$temporary/patterns"

if grep -q '^--display$' "$temporary/options"; then
    # Override ordinary environment forwarding with the scoped authority.
    set -- --env "DISPLAY=:$display_number" --env XAUTHORITY=/chainman-x11-authority "$@"
fi

# A linked worktree needs only its Git administrative directory, not its other
# checkout. Retained Git settings keep their repository/command scope.
count=0
if [ "$authority" != "$root" ]; then
    # Candidates have a self-contained, frozen Git directory. Never ask their
    # mutable metadata to select host administrative mounts or signing policy.
    [ -f "$authority/git-directories" ] && [ ! -L "$authority/git-directories" ] || fail 'Missing frozen Git directory inventory.'
    while IFS= read -r git_relative; do
        single_line "$git_relative"
        case "$git_relative" in '' | /* | *','* | *'/../'* | '../'* | *'/..') fail 'Unsafe frozen Git directory.' ;; esac
        case "$git_relative" in .git | */.git) ;; *) fail 'Expected a Git administrative directory.' ;; esac
        git_directory=$root/$git_relative
        [ -d "$git_directory" ] && [ ! -L "$git_directory" ] || fail 'Candidate requires real local Git directories.'
        [ "$(CDPATH='' cd -P -- "$git_directory" && pwd)" = "$git_directory" ] || fail 'Candidate Git directory contains a symlink.'
        set -- --mount "type=bind,src=$git_directory,dst=$git_directory,readonly" "$@"
    done < "$authority/git-directories"
    set -- --env GIT_CONFIG_GLOBAL=/dev/null --env GIT_CONFIG_SYSTEM=/dev/null --env GIT_CONFIG_NOSYSTEM=1 --env GIT_OPTIONAL_LOCKS=0 "$@"
    count=4
    set -- --env GIT_CONFIG_KEY_0=core.fsmonitor --env GIT_CONFIG_VALUE_0=false --env GIT_CONFIG_KEY_1=core.hooksPath --env GIT_CONFIG_VALUE_1=/dev/null \
        --env GIT_CONFIG_KEY_2=gc.auto --env GIT_CONFIG_VALUE_2=0 --env GIT_CONFIG_KEY_3=maintenance.auto --env GIT_CONFIG_VALUE_3=false "$@"
elif command -v git > /dev/null 2>&1; then
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

fi
case "$self" in "$root"/*) ;; *) set -- --mount "type=bind,src=$source_root,dst=$source_root,readonly" "$@" ;; esac
if [ "$authority" != "$root" ]; then
    if [ "$authority" != "$script_dir" ]; then set -- --mount "type=bind,src=$authority,dst=$authority,readonly" "$@"; fi
    set -- --env "CHAINMAN_ENTRY_AUTHORITY=$authority" "$@"
fi
if [ -n "${CHAINMAN_CONTAINER_NAME:-}" ]; then
    case "$CHAINMAN_CONTAINER_NAME" in chainman-[A-Za-z0-9_-]*) ;; *) fail 'Invalid owned container name.' ;; esac
    case "${CHAINMAN_CONTAINER_OWNER:-}" in '' | *[!a-f0-9]*) fail 'Invalid container ownership token.' ;; esac
    [ "${#CHAINMAN_CONTAINER_OWNER}" = 32 ] || fail 'Invalid container ownership token.'
    set -- --name "$CHAINMAN_CONTAINER_NAME" --label "dev.chainman.owner=$CHAINMAN_CONTAINER_OWNER" "$@"
fi
project_mount="type=bind,src=$root,dst=$root"
case "$CHAINMAN_REQUEST_ACTION" in _control-export | _hook-export | _consent-export) project_mount=$project_mount,readonly ;; esac
set -- --rm --init --interactive --user "$container_uid:$container_gid" --label dev.chainman.store.schema=1 --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=volume,src=$volume,dst=/nix" --mount "$project_mount" \
    --mount "type=volume,src=$downloads_volume,dst=/chainman-downloads" --env TOOLCHAIN_DOWNLOAD_CACHE=/chainman-downloads \
    --workdir "$root" \
    --env "CHAINMAN_TIMING=${CHAINMAN_TIMING:-0}" --env "CHAINMAN_TIMING_BOOTSTRAP_STARTED=${CHAINMAN_TIMING_BOOTSTRAP_STARTED:-}" --env "CHAINMAN_TIMING_PARENT=${CHAINMAN_TIMING_PARENT:-}" \
    --env HOME=/tmp/chainman-home --env CHAINMAN_MODE=container-nix --env CHAINMAN_BOOTSTRAP_CONTAINER=1 \
    --env CHAINMAN_HOOK_REMOTE_NAME --env CHAINMAN_HOOK_REMOTE_URL \
    --env CHAINMAN_SETUP --env CHAINMAN_CONTAINER_PLATFORM --env CHAINMAN_CONTAINER_NETWORK_MODE --env CHAINMAN_NIX_VOLUME --env CHAINMAN_UPDATE_ACTIVE --env CHAINMAN_CONTEXT_TASK \
    --env CHAINMAN_WORKSPACE_TRANSACTION_ROOT --env CHAINMAN_SOURCE_REVISION --env CHAINMAN_HOST_PLATFORM \
    --env 'NIX_CONFIG=build-users-group =
store = daemon' --env NIX_REMOTE=daemon \
    --env "CHAINMAN_PROJECT_ROOT=$root" --env TOOLCHAIN_CONTAINER=1 --env "GIT_CONFIG_COUNT=$count" \
    --env CI --env TERM "$@"
if [ -t 0 ] && [ -t 1 ]; then set -- --tty "$@"; fi
if [ "$engine" = podman ]; then set -- --userns=keep-id "$@"; fi
if [ -n "${CHAINMAN_CONTAINER_OPTIONS_FILE:-}" ]; then
    case "$prepare_action" in config | explain)
        set -- --mount "type=bind,src=$temporary/extra,dst=/chainman-inspection-options,readonly" \
            --env CHAINMAN_INSPECTION_OPTIONS=/chainman-inspection-options "$@"
        ;;
    esac
fi
if [ -n "$prepare_profile" ]; then
    # Setup gets no execution mounts. The final readiness check may fail on a
    # concurrent change, but cannot install with workload credentials present.
    lifetime_helper "$self" _transport-prepare "$prepare_action" "$prepare_task" "$prepare_profile" < /dev/null >&2
fi
if [ "$transport_readiness" = error ]; then
    CHAINMAN_SETUP=error
    export CHAINMAN_SETUP
fi
if [ -d "$temporary/x11" ] || { [ -f "$temporary/extra" ] && { [ "$prepare_action" = config ] || [ "$prepare_action" = explain ]; }; }; then
    # The supervisor owns cleanup through interruption and normal exit.
    trap - EXIT HUP INT TERM
    exec sh "$script_dir/setup-prompt.sh" --cleanup-directory "$temporary" "$engine" "$@"
fi
rm -rf -- "$temporary"
trap - EXIT HUP INT TERM
exec sh "$script_dir/setup-prompt.sh" "$engine" "$@"
