#!/bin/sh
# Chainman's source development shell; installed consumers use the pinned bootstrap.
# shellcheck disable=SC2016
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
profile=${1:-core}
[ "$#" -eq 0 ] || shift
case "$profile" in core | javascript | rust | python | go | flutter | swift | compose | browser | control) ;; *)
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
# Keep the installed host Nix through project profiles; never provision a replacement.
nix_bin=${CHAINMAN_NIX_BIN:-$(command -v nix)}
case "$nix_bin" in /*) ;; *) nix_bin=$(CDPATH='' cd -- "$(dirname -- "$nix_bin")" && pwd)/$(basename -- "$nix_bin") ;; esac
selected_nix=$nix_bin
while [ -L "$selected_nix" ]; do
    target=$(readlink "$selected_nix")
    case "$target" in /*) selected_nix=$target ;; *) selected_nix=$(dirname -- "$selected_nix")/$target ;; esac
done
CHAINMAN_RUNTIME_NIX_BIN=$(CDPATH='' cd -P -- "$(dirname -- "$selected_nix")" && pwd)
export CHAINMAN_RUNTIME_NIX_BIN
inputs=$(cksum "$root/nix/flake.nix" "$root/nix/flake.lock")
if [ "${TOOLCHAIN_ACTIVE_PROFILE:-}" = "$profile" ] && [ "${TOOLCHAIN_ACTIVE_ROOT:-}" = "$root" ] && [ "${TOOLCHAIN_ACTIVE_INPUTS:-}" = "$inputs" ] && [ "${TOOLCHAIN_FRESH:-0}" != 1 ]; then
    cd "$root"
    exec "$@"
fi
export TOOLCHAIN_ACTIVE_ROOT="$root" TOOLCHAIN_ACTIVE_INPUTS="$inputs" TOOLCHAIN_MODE=host-nix
unset IN_NIX_SHELL TOOLCHAIN_FRESH
cd "$root/nix"
exec "$nix_bin" --extra-experimental-features 'nix-command flakes' develop "path:.#$profile" --no-write-lock-file --command sh -eu -c 'cd "$1"; shift; exec "$@"' sh "$root" "$@"
