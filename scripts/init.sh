#!/bin/sh
# Enter pinned initialization tooling without a host language interpreter.
# shellcheck disable=SC2016
set -eu
fail() {
    printf 'Chainman initialization: %s\n' "$*" >&2
    exit 2
}
[ "$#" = 2 ] || fail 'usage: just init DEST VERSION'
source_root=$(CDPATH='' cd -P -- "$(dirname -- "$0")/.." && pwd)
case "$1" in /*) destination=$1 ;; *) destination=$PWD/$1 ;; esac
case "$destination" in *'
'* | *''*) fail 'Newlines are not supported in destination paths.' ;; esac
version=$2
mode=${CHAINMAN_MODE:-container-nix}
case "$mode" in host-nix | container-nix) ;; *) fail 'CHAINMAN_MODE must be host-nix or container-nix.' ;; esac
check_path=$destination
while [ "$check_path" != / ]; do
    [ ! -L "$check_path" ] || fail 'Destination paths must not contain symlinks.'
    check_path=$(dirname -- "$check_path")
done
[ -d "$(dirname -- "$destination")" ] || fail 'The destination parent directory must exist.'
if [ -e "$destination" ]; then
    [ -d "$destination" ] || fail 'Choose a new or empty project directory.'
    [ -z "$(ls -A -- "$destination")" ] || fail 'Choose a new or empty project directory.'
fi
staging=$(mktemp -d "$(dirname -- "$destination")/.chainman-init.XXXXXXXX")
container=
engine=
cleanup() {
    if [ -n "$container" ]; then "$engine" rm -f "$container" > /dev/null 2>&1 || true; fi
    rm -rf -- "$staging"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
if [ "$mode" = host-nix ]; then
    "$source_root/scripts/enter.sh" updates python3 -B "$source_root/scripts/initialize.py" "$staging/project" "$version" >&2
else
    candidates=${CHAINMAN_CONTAINER_ENGINE:-docker podman}
    for candidate in $candidates; do
        case "$candidate" in docker | podman) ;; *) fail 'Container mode requires Docker or Podman.' ;; esac
        if command -v "$candidate" > /dev/null 2>&1 && "$candidate" info > /dev/null 2>&1; then
            engine=$candidate
            break
        fi
    done
    [ -n "$engine" ] || fail 'Start Docker or Podman, or select CHAINMAN_MODE=host-nix.'
    IFS= read -r image < "$source_root/nix/container-image.txt"
    set -- create --cap-drop ALL --security-opt no-new-privileges \
        --mount "type=bind,src=$source_root,dst=/chainman,readonly" \
        --env HOME=/tmp/chainman-home --env 'NIX_CONFIG=build-users-group =' \
        --workdir /chainman
    if [ -n "${CHAINMAN_CONTAINER_PLATFORM:-}" ]; then
        case "$CHAINMAN_CONTAINER_PLATFORM" in linux/amd64 | linux/arm64) ;; *) fail 'Choose linux/amd64 or linux/arm64.' ;; esac
        set -- "$@" --platform "$CHAINMAN_CONTAINER_PLATFORM"
    fi
    if [ -n "${GITHUB_TOKEN:-}" ]; then set -- "$@" --env GITHUB_TOKEN; fi
    # One temporary container owns its private local Nix store. No project output,
    # host HOME, engine socket, or shared Nix store is mounted into the initializer.
    container=$("$engine" "$@" "$image" sh -eu -c '
        chmod 0555 /
        mkdir -p "$HOME"
        export CHAINMAN_RUNTIME_NIX_BIN="$(dirname "$(readlink -f "$(command -v nix)")")"
        exec nix --extra-experimental-features "nix-command flakes" develop \
            path:/chainman/nix#updates --no-write-lock-file --command \
            python3 -B /chainman/scripts/initialize.py /tmp/chainman-output "$1"
    ' sh "$version")
    "$engine" start --attach "$container" >&2
    [ "$("$engine" inspect --format '{{.State.ExitCode}}' "$container")" = 0 ] || fail 'The release could not be initialized.'
    # Engine copy creates host-owned files without a writable output mount.
    "$engine" cp "$container:/tmp/chainman-output" "$staging/project"
fi
# Recheck immediately before publishing the fully verified, generated directory.
[ ! -L "$destination" ] || fail 'Destination changed during initialization.'
if [ -e "$destination" ]; then rmdir -- "$destination" || fail 'Destination is no longer empty.'; fi
mv -- "$staging/project" "$destination"
printf 'Created %s with Chainman %s.\nRun: cd "%s" && git init && git add . && git commit -m "Adopt Chainman" && just setup && just verify\n' "$destination" "$version" "$destination"
