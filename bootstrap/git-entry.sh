#!/bin/sh
# This program is read from a verified Git blob by the consumer recipe.
# Git supplies a fixed list of environment variable names for unset.
# shellcheck disable=SC2046
set -eu
project=$1
cache=$2
revision=$3
shift 3
staging=$(mktemp -d "${TMPDIR:-/tmp}/chainman-source.XXXXXXXX")
cleanup() { rm -rf "$staging"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
g() (
    unset $(GIT_CONFIG_PARAMETERS='' GIT_CONFIG_COUNT=0 git rev-parse --local-env-vars)
    export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=0
    export GIT_NO_REPLACE_OBJECTS=1
    git -c core.hooksPath=/dev/null -c core.attributesFile=/dev/null "$@"
)
# Use fresh repository metadata: cached config and info/attributes cannot alter
# the export. Objects have already passed the consumer's full integrity check.
g init --bare --quiet --template= "$staging/repository"
export_tree() (
    unset $(GIT_CONFIG_PARAMETERS='' GIT_CONFIG_COUNT=0 git rev-parse --local-env-vars)
    export GIT_OBJECT_DIRECTORY="$cache/objects"
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_COUNT=0 \
        git --no-replace-objects --git-dir="$staging/repository" \
        -c core.attributesFile=/dev/null "$@"
)
# Source distributions deliberately contain ordinary files only. Refuse Git
# attributes rather than silently honoring export-ignore or export-subst.
export_tree ls-tree -r "$revision" > "$staging/tree"
while IFS= read -r item; do
    case "$item" in
        100644\ * | 100755\ *) ;;
        *)
            echo 'Chainman source contains an unsupported Git tree mode.' >&2
            exit 2
            ;;
    esac
    case "$item" in
        *"$(printf '\t')".gitattributes | */.gitattributes | *'/.gitattributes"')
            echo 'Chainman source must not contain Git export attributes.' >&2
            exit 2
            ;;
    esac
done < "$staging/tree"
mkdir "$staging/source"
export_tree read-tree "$revision"
export_tree --work-tree="$staging/source" checkout-index --all --prefix="$staging/source/"
export CHAINMAN_PROJECT_ROOT="$project" CHAINMAN_SOURCE_REVISION="$revision"
# Retain the private export until all host orchestration exits. The executable
# environments and runtime used inside Nix are rooted separately in its store.
"$staging/source/bootstrap/chainman.sh" "$@"
