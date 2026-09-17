#!/bin/sh
# Reuse a verified execution environment while retaining ordinary task admission.
set -eu
root=$1
shift
if [ "${CHAINMAN_ROOT:-}" != "$root" ] || [ -z "${CHAINMAN_RUNTIME_PYTHON:-}" ] || [ -z "${CHAINMAN_ACTIVE_PROFILE:-}" ]; then
    echo 'Chainman reentry requires an active environment for this project.' >&2
    exit 2
fi
authority=${CHAINMAN_ENTRY_AUTHORITY:-$root}
if [ ! -f "$authority/chainman.lock" ] || [ -L "$authority/chainman.lock" ] \
    || ! IFS= read -r pin < "$authority/chainman.lock" \
    || [ "$(wc -c < "$authority/chainman.lock")" -ne 41 ] \
    || [ "$pin" != "${CHAINMAN_ACTIVE_PIN:-}" ]; then
    echo 'Chainman pin changed or is malformed; leave this shell and enter the project again.' >&2
    exit 2
fi
runtime=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec "$CHAINMAN_RUNTIME_PYTHON" -E -s -B "$runtime/scripts/chainman.py" --root "$root" run "$@"
