#!/bin/sh
# Source-development entry; consumers always enter through their verified pin.
set -eu
source=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
if [ "${1:-}" = _hook-worker ]; then
    shift
    exec "$source/scripts/enter.sh" bootstrap python3 "$source/scripts/hook_worker.py" "$CHAINMAN_PROJECT_ROOT" "$@"
fi
[ "$#" = 0 ] || {
    echo 'Source staged formatting accepts no arguments.' >&2
    exit 2
}
git=$(command -v git)
case "$(uname -s):$(uname -m)" in
    Linux:aarch64 | Linux:arm64) target=linux-arm64 ;;
    Linux:x86_64) target=linux-amd64 ;;
    Darwin:arm64) target=darwin-arm64 ;;
    Darwin:x86_64) target=darwin-amd64 ;;
    *) exit 2 ;;
esac
# shellcheck source=bootstrap/lifetime.sh
. "$source/bootstrap/lifetime.sh"
output=$(mktemp -d "${TMPDIR:-/tmp}/chainman-source-hooks.XXXXXXXX")
lifetime_directory=$output
lifetime_helper "$source/scripts/enter.sh" bootstrap python3 "$source/scripts/hook_worker.py" "$source" export "$output" "$target" "$git" "$source/scripts/source-hooks.sh" format-staged
lifetime_grace=10
lifetime_run "$output/chainman-control" hook "$output/plan.json" format-staged
