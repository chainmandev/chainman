#!/bin/sh
# Child shell programs intentionally expand their own positional/environment values.
# shellcheck disable=SC2016
# Enter an explicitly selected environment, preserving each command argument.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
profile=${1:-core}
[ "$#" -eq 0 ] || shift
case "$profile" in core | javascript | rust | python | go | flutter | swift | compose | browser) ;; *)
    echo 'Unknown toolchain profile.' >&2
    exit 2
    ;;
esac
[ "$#" -gt 0 ] || set -- bash
cd "$root"
mode=${TOOLCHAIN_MODE:-container-nix}
case "$mode" in host-nix | container-nix) ;; *)
    echo 'TOOLCHAIN_MODE must be container-nix or host-nix.' >&2
    exit 2
    ;;
esac
if [ -n "${TOOLCHAIN_ACTIVE_MODE:-}" ] && [ "$TOOLCHAIN_ACTIVE_MODE" != "$mode" ]; then
    echo 'Start the requested mode from outside the active development shell.' >&2
    exit 2
fi
inputs=$(cksum "$root/nix/flake.nix" "$root/nix/flake.lock" "$root/nix/container-image.txt")
if [ "${TOOLCHAIN_ACTIVE_PROFILE:-}" = "$profile" ] && [ "${TOOLCHAIN_ACTIVE_ROOT:-}" = "$root" ] \
    && [ "${TOOLCHAIN_ACTIVE_INPUTS:-}" = "$inputs" ] && [ "${TOOLCHAIN_ACTIVE_MODE:-}" = "$mode" ] \
    && [ "${TOOLCHAIN_FRESH:-0}" != 1 ]; then
    exec "$@"
fi
# Explicit module commands may enter a different shell within the same container;
# this never changes mounts, execution mode, or native SDK access.
if [ "$mode" = host-nix ] || [ "${TOOLCHAIN_CONTAINER:-0}" = 1 ]; then
    command -v nix > /dev/null 2>&1 || {
        echo 'Host mode requires Nix.' >&2
        exit 1
    }
    TOOLCHAIN_ACTIVE_ROOT=$root TOOLCHAIN_ACTIVE_INPUTS=$inputs TOOLCHAIN_ACTIVE_MODE=$mode TOOLCHAIN_MODE=$mode
    export TOOLCHAIN_ACTIVE_ROOT TOOLCHAIN_ACTIVE_INPUTS TOOLCHAIN_ACTIVE_MODE TOOLCHAIN_MODE
    unset TOOLCHAIN_ACTIVE_PROFILE TOOLCHAIN_FRESH IN_NIX_SHELL
    cd "$root/nix"
    exec nix --extra-experimental-features 'nix-command flakes' develop "path:.#$profile" --no-write-lock-file --command sh -eu -c 'cd "$1"; shift; exec "$@"' sh "$root" "$@"
fi
engine=${TOOLCHAIN_CONTAINER_ENGINE:-}
if [ -z "$engine" ]; then
    for candidate in docker podman; do
        if command -v "$candidate" > /dev/null 2>&1; then
            engine=$candidate
            break
        fi
    done
fi
case "$engine" in docker | podman) ;; *)
    echo 'Container mode requires Docker or Podman; host Nix is an explicit alternative.' >&2
    exit 1
    ;;
esac
image=$(cat "$root/nix/container-image.txt")
printf '%s\n' "$image" | grep -Eq '^[a-zA-Z0-9./_:-]+@sha256:[a-f0-9]{64}$' || {
    echo 'Container image must have an immutable digest.' >&2
    exit 2
}
uid=$(id -u)
gid=$(id -g)
# Owned volumes are never erased automatically, including on image upgrades.
volume="nix-just-store-$uid"
downloads="nix-just-downloads-$uid"
run() {
    if [ "$engine" = podman ]; then
        "$engine" run --userns=keep-id "$@"
    else
        "$engine" run "$@"
    fi
}
run --rm --user 0:0 --mount "type=volume,src=$volume,dst=/nix" --mount "type=volume,src=$downloads,dst=/cache" "$image" sh -eu -c '
    mkdir -p /nix/store /nix/var /cache
    chown "$1:$2" /nix /nix/store /cache
    [ ! -d /nix/store/.links ] || chown "$1:$2" /nix/store/.links
    chown -R "$1:$2" /nix/var
' sh "$uid" "$gid"
set -- "$image" sh -eu -c 'mkdir -p "$HOME"; exec "$@"' sh "$root/scripts/enter.sh" "$profile" "$@"
# Transfer only effective identity/signing settings, never a home directory or
# credential configuration. A configured unavailable signer remains an error.
# Git is optional on the host; a global config that cannot be interpreted blocks
# automatic commits, while ordinary development and --no-commit remain usable.
count=0
policy_unavailable=0
if command -v git > /dev/null 2>&1; then
    for key in user.name user.email user.signingkey commit.gpgsign gpg.format gpg.program gpg.openpgp.program gpg.ssh.program gpg.ssh.defaultKeyCommand gpg.x509.program; do
        status=0
        value=$(git -C "$root" config --get "$key" 2> /dev/null) || status=$?
        case "$status" in
            0)
                set -- --env "GIT_CONFIG_KEY_$count=$key" --env "GIT_CONFIG_VALUE_$count=$value" "$@"
                count=$((count + 1))
                ;;
            1) ;;
            *) policy_unavailable=1 ;;
        esac
    done
elif [ -f "${GIT_CONFIG_GLOBAL:-$HOME/.gitconfig}" ] || [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/git/config" ]; then
    policy_unavailable=1
fi
set -- --env "GIT_CONFIG_COUNT=$count" --env "TOOLCHAIN_GIT_POLICY_UNAVAILABLE=$policy_unavailable" "$@"
# The inner positional parameters are consumed before dispatching the command.
# Environment values are passed by name so their contents never become arguments.
set -- --rm --init --interactive --user "$uid:$gid" \
    --security-opt no-new-privileges --cap-drop ALL \
    --mount "type=volume,src=$volume,dst=/nix" \
    --mount "type=volume,src=$downloads,dst=/cache" \
    --mount "type=bind,src=$root,dst=$root" --workdir "$root" \
    --env HOME=/tmp/toolchain-home --env TOOLCHAIN_MODE=container-nix \
    --env TOOLCHAIN_CONTAINER=1 --env TOOLCHAIN_DOWNLOAD_CACHE=/cache \
    --env CI --env TERM --env GIT_AUTHOR_NAME --env GIT_AUTHOR_EMAIL \
    --env GIT_COMMITTER_NAME --env GIT_COMMITTER_EMAIL "$@"
if [ -t 0 ] && [ -t 1 ]; then set -- --tty "$@"; fi
if [ "$engine" = podman ]; then exec "$engine" run --userns=keep-id "$@"; fi
exec "$engine" run "$@"
