#!/bin/sh
# Keep external policy in its native scope. Repository configuration stays live.
set -eu
root=$1
output=$2
scope=${3:-repository}
cd -- "$root"
g() {
    if [ "$scope" = global ]; then git --git-dir=/dev/null "$@"; else git "$@"; fi
}
scripts=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
mkdir -m 700 "$output"
# Retain the explicit rejection of system attributes, and Git's implicit default
# attributes when no config file specifies an attributesFile.
sh "$scripts/git-attributes.sh" "$root" "$output/default-attributes" global
: > "$output/system"
: > "$output/global"
: > "$output/command"
git config --file "$output/system" core.attributesFile /chainman-git-policy/default-attributes
for scope_file in SYSTEM GLOBAL; do
    status=0
    git var "GIT_CONFIG_$scope_file" > "$output/locations" || status=$?
    case "$status" in 0 | 1) ;; *) exit "$status" ;; esac
    case "$scope_file" in SYSTEM) destination=system ;; GLOBAL) destination=global ;; esac
    while IFS= read -r source; do
        sh "$scripts/git-policy-config.sh" file "$source" "$output" "$output/$destination" 0
    done < "$output/locations"
done
rm -- "$output/locations"
# Deliberate command-scope overrides still apply to every child Git invocation.
for key in core.autocrlf core.eol core.hooksPath core.attributesFile user.name user.email user.signingkey commit.gpgsign gpg.format gpg.program gpg.openpgp.program gpg.ssh.program gpg.ssh.defaultKeyCommand gpg.x509.program; do
    origin=$(g config --show-scope --get "$key" | cut -f1)
    [ "$origin" = command ] || continue
    kind=
    case "$key" in
        core.autocrlf) kind=--type=bool-or-str ;;
        commit.gpgsign) kind=--bool ;;
        core.hooksPath | core.attributesFile) kind=--path ;;
    esac
    if [ -n "$kind" ]; then value=$(g config "$kind" --get "$key"); else value=$(g config --get "$key"); fi
    if [ "$key" = core.attributesFile ]; then
        sh "$scripts/git-attributes.sh" "$root" "$output/command-attributes" "$scope"
        value=/chainman-git-policy/command-attributes
    fi
    git config --file "$output/command" "$key" "$value"
done
