#!/bin/sh
# Runs only on the caller's host, from the verified runtime export.
set -eu
self=$1
root=$2
cd "$root"
# shellcheck source=bootstrap/lifetime.sh
. "$(dirname -- "$self")/lifetime.sh"
shift 2
[ "${CHAINMAN_BOOTSTRAP_CONTAINER:-0}" != 1 ] || {
    echo 'chainman: run Git/hooks on the host (Docker/Podman still supplies hook tools), or use host Nix.' >&2
    exit 2
}
case "$(uname -s):$(uname -m)" in
    Linux:aarch64 | Linux:arm64) target=linux-arm64 ;;
    Linux:x86_64) target=linux-amd64 ;;
    Darwin:arm64) target=darwin-arm64 ;;
    Darwin:x86_64) target=darwin-amd64 ;;
    *)
        echo 'chainman: unsupported native hook platform.' >&2
        exit 2
        ;;
esac
git=$(command -v git)
case "$git" in /*) ;; *) git=$(CDPATH='' cd -- "$(dirname -- "$git")" && pwd)/$(basename -- "$git") ;; esac
output=$(mktemp -d "${TMPDIR:-/tmp}/chainman-hooks.XXXXXXXX")
# The runtime's lifetime supervisor owns signal forwarding and private cleanup.
lifetime_directory=$output
printf '%s\n%s\n' --mount "type=bind,src=$output,dst=$output" > "$output/mounts"
CHAINMAN_FORWARD_ENV='' CHAINMAN_CONTAINER_OPTIONS_FILE=$output/mounts \
    lifetime_helper sh "$self" _hook-export "$output" "$target" "$git" "$self" "$1" < /dev/null >&2
lifetime_grace=10
case "$1" in
    setup)
        IFS= read -r enabled < "$output/enabled"
        if [ "$enabled" = 1 ]; then lifetime_run "$output/chainman-control" hook "$output/plan.json" check-install; fi
        lifetime_helper sh "$self" setup --no-hooks
        lifetime_grace=10
        if [ "$enabled" = 1 ]; then lifetime_run "$output/chainman-control" hook "$output/plan.json" install; fi
        ;;
    hooks)
        shift
        lifetime_run "$output/chainman-control" hook "$output/plan.json" "$@"
        ;;
    *) lifetime_run "$output/chainman-control" hook "$output/plan.json" "$@" ;;
esac
