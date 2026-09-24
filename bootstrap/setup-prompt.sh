#!/bin/sh
# The foreground native caller owns consent; only regular messages cross a VM.
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
if [ -n "${CHAINMAN_DEV_CHANNEL:-}" ]; then
    [ -d "$CHAINMAN_DEV_CHANNEL" ] && [ ! -L "$CHAINMAN_DEV_CHANNEL" ] || {
        echo 'chainman: development status channel is unavailable.' >&2
        exit 1
    }
    # This private channel is observational only. Controller state and leases
    # remain on the host; a workload cannot issue service-control requests here.
    set -- --mount "type=bind,src=$CHAINMAN_DEV_CHANNEL,dst=/chainman-development" \
        --env CHAINMAN_DEV_CHANNEL=/chainman-development \
        --env "CHAINMAN_DEV_OPERATION=$CHAINMAN_DEV_OPERATION" \
        --env "CHAINMAN_DEV_TASK=$CHAINMAN_DEV_TASK" "$@"
fi
if [ "${CHAINMAN_SETUP:-prompt}" = prompt ] && [ -n "${CHAINMAN_SETUP_CHANNEL:-}" ]; then
    channel=$CHAINMAN_SETUP_CHANNEL
    [ -d "$channel/incoming" ] && [ -f "$channel/outgoing/alive" ] || {
        echo 'chainman: setup consent channel is unavailable; run just setup and retry.' >&2
        exit 1
    }
    # The workload can post requests but cannot replace host replies or liveness.
    lifetime_run "$engine" run \
        --mount "type=bind,src=$channel/incoming,dst=/chainman-setup-prompt/incoming" \
        --mount "type=bind,src=$channel/outgoing,dst=/chainman-setup-prompt/outgoing,readonly" \
        --env CHAINMAN_SETUP_CHANNEL=/chainman-setup-prompt "$@"
else
    lifetime_run "$engine" run "$@"
fi
