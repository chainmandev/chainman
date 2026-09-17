#!/bin/sh
# Pinned runtime entry used by the effective lefthook configuration.
set -eu
if [ -n "${CHAINMAN_HOOK_INPUT:-}" ]; then exec < "$CHAINMAN_HOOK_INPUT"; fi
exec "$CHAINMAN_RUNTIME_PYTHON" "$CHAINMAN_RUNTIME/scripts/chainman.py" --root "$CHAINMAN_ROOT" "$@"
