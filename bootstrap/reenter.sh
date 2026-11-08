#!/bin/sh
# Reuse a verified execution environment while retaining ordinary task admission.
set -eu
root=$(CDPATH='' cd -P -- "$1" && pwd)
shift
internal_entry=0
if [ "${1:-}" = --entry ]; then
    case "${2:-}" in
        _workflow-service | _workflow-probe | _workflow-shutdown)
            if [ "${CHAINMAN_INTERNAL_ENTRY:-}" = 1 ]; then internal_entry=1; fi
            ;;
    esac
fi
if [ "${CHAINMAN_ROOT:-}" != "$root" ] || [ -z "${CHAINMAN_RUNTIME_PYTHON:-}" ] \
    || { [ "$internal_entry" != 1 ] && [ -z "${CHAINMAN_ACTIVE_PROFILE:-}" ]; }; then
    echo 'Chainman reentry requires an active environment for this project.' >&2
    exit 2
fi
if [ "$internal_entry" = 1 ]; then
    case "${CHAINMAN_ACTIVE_MODE:-}" in
        host-nix | container-nix) ;;
        *)
            echo 'Chainman internal entry requires its verified execution mode.' >&2
            exit 2
            ;;
    esac
fi
authority=${CHAINMAN_ENTRY_AUTHORITY:-$root}
if [ ! -f "$authority/chainman.lock" ] || [ -L "$authority/chainman.lock" ] \
    || ! IFS= read -r pin < "$authority/chainman.lock" \
    || [ "$(wc -c < "$authority/chainman.lock")" -ne 41 ] \
    || [ "$pin" != "${CHAINMAN_ACTIVE_PIN:-}" ]; then
    echo 'Chainman pin changed or is malformed; leave this shell and enter the project again.' >&2
    exit 2
fi
if [ "$internal_entry" = 1 ]; then
    case "$pin" in
        '' | *[!0-9a-f]*)
            echo 'Chainman internal entry requires a valid revision pin.' >&2
            exit 2
            ;;
    esac
fi
runtime=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
case "${CHAINMAN_ACTIVE_MODE:-}" in
    host-nix | container-nix)
        # Public entry exports Git into a fresh temporary directory. Reuse the
        # immutable runtime only after checking that it is exactly that tree;
        # profile/setup identity must not depend on the temporary export path.
        active=${CHAINMAN_RUNTIME:-}
        case "$active" in
            /nix/store/*-chainman-source)
                if [ "$(dirname -- "$active")" != /nix/store ] || [ ! -d "$active" ] || [ -L "$active" ]; then
                    echo 'Chainman reentry requires an immutable Nix-store runtime.' >&2
                    exit 2
                fi
                ;;
            *)
                echo 'Chainman reentry requires an immutable Nix-store runtime.' >&2
                exit 2
                ;;
        esac
        nix=${CHAINMAN_RUNTIME_NIX_BIN:?Chainman reentry requires its selected Nix}/nix
        # Identical validated store paths already name the same immutable tree.
        if [ "$runtime" != "$active" ]; then
            expected=$("$nix" --extra-experimental-features nix-command hash path "$runtime")
            actual=$("$nix" --extra-experimental-features nix-command hash path "$active")
            if [ "$actual" != "$expected" ]; then
                echo 'Chainman reentry runtime differs from the verified Git source; leave this shell and enter the project again.' >&2
                exit 2
            fi
        fi
        runtime=$active
        ;;
esac
if [ "$internal_entry" = 1 ]; then
    # Check the supplied interpreter before it can execute. The already held
    # bootstrap root, not a caller's PATH or an active-profile label, owns it.
    case "$CHAINMAN_RUNTIME_PYTHON" in
        /nix/store/*/bin/python3*) ;;
        *)
            echo 'Chainman internal entry requires its Nix-store interpreter.' >&2
            exit 2
            ;;
    esac
    interpreter=$(realpath -e -- "$CHAINMAN_RUNTIME_PYTHON")
    case "$interpreter" in
        /nix/store/*/bin/python3*) ;;
        *)
            echo 'Chainman internal interpreter must resolve into its Nix store.' >&2
            exit 2
            ;;
    esac
    if [ "${TOOLCHAIN_CONTAINER:-0}" = 1 ]; then
        runtime_base=/nix/var/nix/chainman-runtime-roots
    else
        runtime_base=${XDG_CACHE_HOME:-$HOME/.cache}/chainman/runtime-roots
    fi
    bootstrap=$runtime_base/.chainman-storage-v1/$pin/bootstrap
    bootstrap_target=$(realpath -e -- "$bootstrap")
    case "$bootstrap_target" in
        /nix/store/*-nix-shell-env)
            [ "$(dirname -- "$bootstrap_target")" = /nix/store ] || exit 2
            ;;
        *)
            echo 'Chainman internal entry requires its held bootstrap environment.' >&2
            exit 2
            ;;
    esac
    interpreter_store=${interpreter#/nix/store/}
    interpreter_store=/nix/store/${interpreter_store%%/*}
    proof=$(mktemp -d "${TMPDIR:-/tmp}/chainman-interpreter.XXXXXXXX")
    trap 'rm -rf -- "$proof"' EXIT
    if ! timeout --kill-after=2 10 "$CHAINMAN_RUNTIME_NIX_BIN/nix-store" \
        --query --requisites "$bootstrap_target" > "$proof/closure" \
        || ! grep -F -x -q -- "$interpreter_store" "$proof/closure"; then
        echo 'Chainman internal interpreter is outside its held bootstrap closure.' >&2
        exit 2
    fi
    rm -rf -- "$proof"
    trap - EXIT
    # Execute the checked immutable path, not the original supplied alias.
    CHAINMAN_RUNTIME_PYTHON=$interpreter
    export CHAINMAN_RUNTIME_PYTHON
fi
exec "$CHAINMAN_RUNTIME_PYTHON" -E -s -B "$runtime/scripts/reentry.py" "$root" "$@"
