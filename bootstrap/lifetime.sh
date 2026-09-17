#!/bin/sh
# Sourced only by verified runtime scripts. Own direct children and private paths.
lifetime_child=
lifetime_relay=
lifetime_directory=
lifetime_channel=
lifetime_grace=3
lifetime_starting=0
lifetime_signal=
lifetime_status=
lifetime_is_helper=0

lifetime_stop() {
    lifetime_remaining=$lifetime_grace
    for lifetime_pid in $lifetime_child $lifetime_relay; do
        kill -"$1" "$lifetime_pid" 2> /dev/null || true
    done
    while [ "$lifetime_remaining" -gt 0 ]; do
        lifetime_alive=
        for lifetime_pid in $lifetime_child $lifetime_relay; do
            if kill -0 "$lifetime_pid" 2> /dev/null; then lifetime_alive=yes; fi
        done
        [ -n "$lifetime_alive" ] || break
        sleep 1
        lifetime_remaining=$((lifetime_remaining - 1))
    done
    for lifetime_pid in $lifetime_child $lifetime_relay; do
        if kill -0 "$lifetime_pid" 2> /dev/null; then
            printf '%s\n' 'chainman: owned helper did not stop; forcing client exit. Check container status if the engine is unresponsive.' >&2
            kill -KILL "$lifetime_pid" 2> /dev/null || true
        fi
        wait "$lifetime_pid" 2> /dev/null || true
    done
    lifetime_child=
    lifetime_relay=
}

lifetime_cleanup() {
    trap '' HUP INT TERM
    lifetime_stop TERM
    [ -z "$lifetime_channel" ] || rm -rf -- "$lifetime_channel"
    [ -z "$lifetime_directory" ] || rm -rf -- "$lifetime_directory"
}

lifetime_interrupt() {
    # A trap between fork and PID assignment must not orphan the new child.
    lifetime_signal=$1
    lifetime_status=$2
    [ "$lifetime_starting" = 0 ] || return 0
    trap '' HUP INT TERM
    # POSIX asynchronous shell helpers inherit ignored SIGINT. Cancel those
    # with TERM, retaining the original signal status at the calling boundary.
    if [ "$lifetime_is_helper" = 1 ] && [ "$lifetime_signal" = INT ]; then lifetime_signal=TERM; fi
    lifetime_stop "$lifetime_signal"
    exit "$lifetime_status"
}

lifetime_run() {
    exec 9<&0
    lifetime_starting=1
    "$@" <&9 9<&- &
    lifetime_child=$!
    lifetime_starting=0
    exec 9<&-
    if [ -n "$lifetime_signal" ]; then lifetime_interrupt "$lifetime_signal" "$lifetime_status"; fi
    lifetime_result=0
    wait "$lifetime_child" || lifetime_result=$?
    lifetime_child=
    return "$lifetime_result"
}

lifetime_helper() {
    # Nested verified helpers need time to finish their own 3-second shutdown.
    lifetime_grace=5
    lifetime_is_helper=1
    lifetime_helper_result=0
    lifetime_run "$@" || lifetime_helper_result=$?
    lifetime_grace=3
    lifetime_is_helper=0
    return "$lifetime_helper_result"
}

trap lifetime_cleanup EXIT
trap 'lifetime_interrupt HUP 129' HUP
trap 'lifetime_interrupt INT 130' INT
trap 'lifetime_interrupt TERM 143' TERM
