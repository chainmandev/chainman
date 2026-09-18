#!/bin/sh
# Project Git policy is scoped to its Git directory, not every child Git process.
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
read_setting() {
    # Keep Git's valueless/empty/numeric boolean semantics. "input" is the
    # additional autocrlf value; --path expands host ~ before HOME changes.
    kind=
    case "$key" in
        core.autocrlf) kind=--type=bool-or-str ;;
        commit.gpgsign) kind=--bool ;;
        core.hooksPath) kind=--path ;;
    esac
    if [ -n "$kind" ]; then "$@" config "$kind" --get "$key"; else "$@" config --get "$key"; fi
}
for key in core.autocrlf core.eol core.hooksPath user.name user.email user.signingkey commit.gpgsign gpg.format gpg.program gpg.openpgp.program gpg.ssh.program gpg.ssh.defaultKeyCommand gpg.x509.program; do
    status=0
    value=$(read_setting git -C "$root" --git-dir=/dev/null) || status=$?
    case "$status" in
        0) git config --file "$output/global" "$key" "$value" ;;
        1) ;;
        *) exit "$status" ;;
    esac
    status=0
    value=$(read_setting g) || status=$?
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
