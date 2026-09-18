#!/bin/sh
# Validate captured hook directories against existing, explicit container mounts.
# Do not grant a configured Git path new access to the host filesystem.
set -eu
root=$1
policy=$2
mounts=$3
fail() {
    printf 'chainman: Git hooks path %s is unavailable in the container; use CHAINMAN_MODE=host-nix, keep hooks in the repository, or declare a mount at the same path. Use core.hooksPath=/dev/null only to deliberately disable hooks.\n' "$hooks" >&2
    exit 2
}
visible() {
    candidate=$1
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
    [ -n "$best" ] && [ "$mapped" = "$candidate" ]
}
for config in global repository command; do
    status=0
    hooks=$(git config --file "$policy/$config" --get core.hooksPath) || status=$?
    case "$status" in 0) ;; 1) continue ;; *) exit "$status" ;; esac
    case "$hooks" in /dev/null) continue ;; /*) path=$hooks ;; *) path=$root/$hooks ;; esac
    # Check both the literal route and symlink destination. Either can cross
    # the boundary; a directory alias outside a mount is not made visible by
    # its destination being inside one.
    ancestor=$path
    suffix=
    while [ ! -d "$ancestor" ]; do
        # A missing in-repository directory must remain repairable by setup.
        # Existing files and dangling symlinks are not ordinary directories.
        [ ! -e "$ancestor" ] && [ ! -L "$ancestor" ] || fail
        component=$(basename -- "$ancestor")
        case "$component" in . | ..) fail ;; esac
        suffix=/$component$suffix
        ancestor=$(dirname -- "$ancestor")
    done
    logical=$(CDPATH='' cd -L -- "$ancestor" 2> /dev/null && pwd -L) || fail
    physical=$(CDPATH='' cd -P -- "$ancestor" 2> /dev/null && pwd -P) || fail
    logical=$logical$suffix
    physical=$physical$suffix
    if ! visible "$logical" || ! visible "$physical"; then fail; fi
done
