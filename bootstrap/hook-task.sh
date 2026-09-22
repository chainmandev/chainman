#!/bin/sh
# Callback from native lefthook into the selected, verified runtime.
set -eu
if [ -n "${CHAINMAN_HOOK_INPUT:-}" ]; then exec < "$CHAINMAN_HOOK_INPUT"; fi
if [ "${1:-}" = run ]; then
    shift
    set -- task "$@"
fi
exec "$CHAINMAN_HOOK_HELPER" hook "$CHAINMAN_HOOK_PLAN" "$@"
