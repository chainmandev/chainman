#!/bin/sh
# Keep private transport files alive exactly as long as the container client.
set -eu
directory=$1
shift
exec 3<&0
child=
# shellcheck disable=SC2329
cleanup() { rm -rf -- "$directory"; }
trap cleanup EXIT
# shellcheck disable=SC2329
interrupt() {
    # Ignore repeated terminal signals while giving the client a bounded grace
    # period. An unresponsive client must not retain private files indefinitely.
    trap '' HUP INT TERM
    if [ -n "$child" ]; then
        kill -"$1" "$child" 2> /dev/null || true
        remaining=3
        while kill -0 "$child" 2> /dev/null && [ "$remaining" -gt 0 ]; do
            sleep 1
            remaining=$((remaining - 1))
        done
        if kill -0 "$child" 2> /dev/null; then
            printf '%s\n' 'Chainman: container client did not stop within 3 seconds; forcing client exit. Check container status if the engine is unresponsive.' >&2
            kill -KILL "$child" 2> /dev/null || true
        fi
        wait "$child" 2> /dev/null || true
        child=
    fi
    exit "$2"
}
trap 'interrupt HUP 129' HUP
trap 'interrupt INT 130' INT
trap 'interrupt TERM 143' TERM
"$@" <&3 &
child=$!
status=0
wait "$child" || status=$?
child=
exit "$status"
