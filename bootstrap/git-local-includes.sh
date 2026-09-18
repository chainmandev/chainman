#!/bin/sh
# Local/worktree config stays live. Its included files must already be mounted.
set -eu
root=$1
mounts=$2
if [ "$#" = 2 ]; then
    records=$(mktemp "${TMPDIR:-/tmp}/chainman-git-origins.XXXXXXXX")
    trap 'rm -f -- "$records"' EXIT HUP INT TERM
    git -C "$root" config --null --show-scope --show-origin --list > "$records"
    if [ -s "$records" ]; then xargs -0 -n 3 sh "$0" "$root" "$mounts" < "$records"; fi
    exit 0
fi
case "$3" in local | worktree) ;; *) exit 0 ;; esac
case "$4" in file:*) path=${4#file:} ;; *) exit 0 ;; esac
case "$path" in /*) ;; *) path=$root/$path ;; esac
fail() {
    printf 'chainman: local Git configuration %s is unavailable in the container; use CHAINMAN_MODE=host-nix or declare a mount at the same path.\n' "$path" >&2
    exit 2
}
parent=$(CDPATH='' cd -P -- "$(dirname -- "$path")" && pwd -P) || fail
physical=$parent/$(basename -- "$path")
# File symlinks can point beyond the declared mounts too.
links=0
while [ -L "$physical" ]; do
    links=$((links + 1))
    [ "$links" -le 40 ] || fail
    link=$(readlink -- "$physical") || fail
    case "$link" in /*) physical=$link ;; *) physical=$(dirname -- "$physical")/$link ;; esac
    parent=$(CDPATH='' cd -P -- "$(dirname -- "$physical")" && pwd -P) || fail
    physical=$parent/$(basename -- "$physical")
done
for candidate in "$path" "$physical"; do
    best=
    mapped=
    while IFS= read -r target && IFS= read -r source; do
        source=${source%,readonly}
        case "$candidate" in
            "$target" | "$target"/*)
                if [ "${#target}" -gt "${#best}" ]; then
                    best=$target
                    mapped=$source${candidate#"$target"}
                fi
                ;;
        esac
    done < "$mounts"
    [ -n "$best" ] && [ "$mapped" = "$candidate" ] || fail
done
