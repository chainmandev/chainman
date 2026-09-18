#!/bin/sh
# Project text policy is scoped to its Git directory, not every child Git process.
set -eu
root=$1
output=$2
scope=${3:-repository}
g() {
    if [ "$scope" = global ]; then git -C "$root" --git-dir=/dev/null "$@"; else git -C "$root" "$@"; fi
}
scripts=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
mkdir -m 700 "$output"
sh "$scripts/git-attributes.sh" "$root" "$output/default-attributes" global
sh "$scripts/git-attributes.sh" "$root" "$output/repository-attributes" "$scope"
git config --file "$output/global" core.attributesFile /chainman-git-policy/default-attributes
git config --file "$output/repository" core.attributesFile /chainman-git-policy/repository-attributes
: > "$output/command"
for key in core.autocrlf core.eol; do
    status=0
    value=$(git -C "$root" --git-dir=/dev/null config --get "$key") || status=$?
    case "$status" in
        0) git config --file "$output/global" "$key" "$value" ;;
        1) ;;
        *) exit "$status" ;;
    esac
    status=0
    value=$(g config --get "$key") || status=$?
    case "$status" in
        0) git config --file "$output/repository" "$key" "$value" ;;
        1) ;;
        *) exit "$status" ;;
    esac
    origin=$(g config --show-scope --get "$key" | cut -f1)
    if [ "$origin" = command ]; then git config --file "$output/command" "$key" "$value"; fi
done
origin=$(g config --show-scope --get core.attributesFile | cut -f1)
if [ "$origin" = command ]; then
    git config --file "$output/command" core.attributesFile /chainman-git-policy/repository-attributes
fi
