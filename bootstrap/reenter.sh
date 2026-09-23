#!/bin/sh
# Reuse a verified execution environment while retaining ordinary task admission.
set -eu
root=$(CDPATH='' cd -P -- "$1" && pwd)
shift
if [ "${CHAINMAN_ROOT:-}" != "$root" ] || [ -z "${CHAINMAN_RUNTIME_PYTHON:-}" ] || [ -z "${CHAINMAN_ACTIVE_PROFILE:-}" ]; then
    echo 'Chainman reentry requires an active environment for this project.' >&2
    exit 2
fi
authority=${CHAINMAN_ENTRY_AUTHORITY:-$root}
if [ ! -f "$authority/chainman.lock" ] || [ -L "$authority/chainman.lock" ] \
    || ! IFS= read -r pin < "$authority/chainman.lock" \
    || [ "$(wc -c < "$authority/chainman.lock")" -ne 41 ] \
    || [ "$pin" != "${CHAINMAN_ACTIVE_PIN:-}" ]; then
    echo 'Chainman pin changed or is malformed; leave this shell and enter the project again.' >&2
    exit 2
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
        expected=$("$nix" --extra-experimental-features nix-command hash path "$runtime")
        actual=$("$nix" --extra-experimental-features nix-command hash path "$active")
        if [ "$actual" != "$expected" ]; then
            echo 'Chainman reentry runtime differs from the verified Git source; leave this shell and enter the project again.' >&2
            exit 2
        fi
        runtime=$active
        ;;
esac
exec "$CHAINMAN_RUNTIME_PYTHON" -E -s -B "$runtime/scripts/reentry.py" "$root" "$@"
