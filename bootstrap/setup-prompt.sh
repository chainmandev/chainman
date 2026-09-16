#!/bin/sh
# Relay setup consent to the host terminal without consuming command stdin.
set -eu
engine=$1
shift
if [ "${CHAINMAN_SETUP:-prompt}" != prompt ] || ! (: < /dev/tty) 2> /dev/null; then
    exec "$engine" run "$@"
fi
# Docker/Podman owns stdin in TTY mode. Let the verified runtime use the
# container's controlling terminal; a second host reader would steal replies.
# Piped input (including Git pre-push) still uses the host relay below.
if [ -t 0 ] && [ -t 1 ]; then
    exec "$engine" run "$@"
fi
umask 077
channel=$(mktemp -d "${TMPDIR:-/tmp}/chainman-setup-prompt.XXXXXXXX")
mkfifo "$channel/request" "$channel/response"
exec 3<&0 4<> /dev/tty
relay=
child=
# Called by the EXIT trap.
# shellcheck disable=SC2329
cleanup() {
    [ -z "$relay" ] || kill "$relay" 2> /dev/null || true
    [ -z "$child" ] || kill "$child" 2> /dev/null || true
    [ -z "$relay" ] || wait "$relay" 2> /dev/null || true
    rm -rf -- "$channel"
}
trap cleanup EXIT
# Forward the original signal once; let the engine supervise its container.
# shellcheck disable=SC2329
interrupt() {
    if [ -n "$child" ]; then
        kill -"$1" "$child" 2> /dev/null || true
        wait "$child" 2> /dev/null || true
        child=
    fi
    exit "$2"
}
trap 'interrupt HUP 129' HUP
trap 'interrupt INT 130' INT
trap 'interrupt TERM 143' TERM
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
relay=$!
"$engine" run --mount "type=bind,src=$channel,dst=/chainman-setup-prompt" \
    --env CHAINMAN_SETUP_CHANNEL=/chainman-setup-prompt "$@" <&3 &
child=$!
status=0
wait "$child" || status=$?
child=
exit "$status"
