#!/bin/sh
# Chainman's source development shell; installed consumers use the pinned bootstrap.
# shellcheck disable=SC2016
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
profile=${1:-core}
[ "$#" -eq 0 ] || shift
case "$profile" in core | javascript | rust | python | go | flutter | swift | compose | browser) ;; *)
    echo 'Unknown development profile.' >&2
    exit 2
    ;;
esac
[ "$#" -gt 0 ] || set -- bash
command -v nix > /dev/null 2>&1 || {
    echo 'Chainman development requires host Nix and just.' >&2
    exit 1
}
if [ -n "${TMPDIR:-}" ]; then
    export CHAINMAN_TEMP_BASE="$TMPDIR"
fi
inputs=$(cksum "$root/nix/flake.nix" "$root/nix/flake.lock")
if [ "${TOOLCHAIN_ACTIVE_PROFILE:-}" = "$profile" ] && [ "${TOOLCHAIN_ACTIVE_ROOT:-}" = "$root" ] && [ "${TOOLCHAIN_ACTIVE_INPUTS:-}" = "$inputs" ] && [ "${TOOLCHAIN_FRESH:-0}" != 1 ]; then
    cd "$root"
    exec "$@"
fi
export TOOLCHAIN_ACTIVE_ROOT="$root" TOOLCHAIN_ACTIVE_INPUTS="$inputs" TOOLCHAIN_MODE=host-nix
unset IN_NIX_SHELL TOOLCHAIN_FRESH
cd "$root/nix"
exec nix --extra-experimental-features 'nix-command flakes' develop "path:.#$profile" --no-write-lock-file --command sh -eu -c 'cd "$1"; shift; exec "$@"' sh "$root" "$@"
