#!/bin/sh
# Validate the effective hook directory and entrypoints against declared mounts.
set -eu
root=$1
mounts=$2
scope=${3:-repository}
g() {
    if [ "$scope" = global ]; then git -C "$root" --git-dir=/dev/null "$@"; else git -C "$root" "$@"; fi
}
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
check_path() {
    ancestor=$1
    suffix=
    while [ ! -d "$ancestor" ]; do
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
}
status=0
hooks=$(g config --path --get core.hooksPath) || status=$?
case "$status" in
    0)
        origin=$(g config --show-scope --get core.hooksPath | cut -f1)
        raw=$(g config --get core.hooksPath)
        case "$origin:$raw" in local:~* | worktree:~*) fail ;; esac
        ;;
    1)
        [ "$scope" != global ] || exit 0
        hooks=$(g rev-parse --path-format=absolute --git-path hooks 2> /dev/null) || exit 0
        ;;
    *) exit "$status" ;;
esac
case "$hooks" in /dev/null) exit 0 ;; /*) path=$hooks ;; *) path=$root/$hooks ;; esac
check_path "$path"
directory=$physical
# Git ignores sample files. Check active hook names, including broken links that
# would otherwise be silently skipped when their destination is not mounted.
for event in applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge pre-push pre-receive update proc-receive post-receive post-update reference-transaction push-to-checkout pre-auto-gc post-rewrite sendemail-validate fsmonitor-watchman p4-changelist p4-prepare-changelist p4-post-changelist p4-pre-submit post-index-change; do
    file=$directory/$event
    if [ ! -L "$file" ] && [ ! -x "$file" ]; then continue; fi
    links=0
    while :; do
        check_path "$(dirname -- "$file")"
        file=$physical/$(basename -- "$file")
        visible "$file" || fail
        [ -L "$file" ] || break
        links=$((links + 1))
        [ "$links" -le 40 ] || fail
        link=$(readlink -- "$file") || fail
        case "$link" in /*) file=$link ;; *) file=$(dirname -- "$file")/$link ;; esac
    done
    [ -f "$file" ] || fail
done
