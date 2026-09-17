#!/bin/sh
# Relay setup consent to the host terminal without consuming command stdin.
set -eu
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=bootstrap/lifetime.sh
. "$script_dir/lifetime.sh"
if [ "${1:-}" = --cleanup-directory ]; then
    lifetime_directory=$2
    shift 2
fi
engine=$1
shift
if [ "${CHAINMAN_SETUP:-prompt}" != prompt ] || ! (: < /dev/tty) 2> /dev/null; then
    if [ -z "$lifetime_directory" ]; then exec "$engine" run "$@"; fi
    lifetime_run "$engine" run "$@"
    exit "$?"
fi
# Docker/Podman owns stdin in TTY mode. Let the verified runtime use the
# container's controlling terminal; a second host reader would steal replies.
# Piped input (including Git pre-push) still uses the host relay below.
if [ -t 0 ] && [ -t 1 ]; then
    if [ -z "$lifetime_directory" ]; then exec "$engine" run "$@"; fi
    lifetime_run "$engine" run "$@"
    exit "$?"
fi
umask 077
channel=$(mktemp -d "${TMPDIR:-/tmp}/chainman-setup-prompt.XXXXXXXX")
lifetime_channel=$channel
mkfifo "$channel/request" "$channel/response"
exec 3<&0 4<> /dev/tty
lifetime_starting=1
(
    trap 'exit 0' HUP INT TERM
    while IFS= read -r question < "$channel/request"; do
        printf '%s' "$question" >&4
        answer=no
        if IFS= read -r answer <&4; then
            case "$answer" in '' | y | Y | yes | YES) answer=yes ;; *) answer=no ;; esac
        fi
        printf '%s\n' "$answer" > "$channel/response"
    done
) &
lifetime_relay=$!
lifetime_starting=0
if [ -n "$lifetime_signal" ]; then lifetime_interrupt "$lifetime_signal" "$lifetime_status"; fi
lifetime_run "$engine" run --mount "type=bind,src=$channel,dst=/chainman-setup-prompt" \
    --env CHAINMAN_SETUP_CHANNEL=/chainman-setup-prompt "$@" <&3
