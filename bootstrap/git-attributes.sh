#!/bin/sh
# Snapshot only external attribute data for a host-originated formatting operation.
set -eu
cd -- "$1"
output=$2
: > "$output"
for location in GIT_ATTR_SYSTEM GIT_ATTR_GLOBAL; do
    if [ "$location" = GIT_ATTR_SYSTEM ]; then
        case "${GIT_ATTR_NOSYSTEM:-}" in '' | 0 | false | no | off) ;; *) continue ;; esac
    fi
    path=$(git var "$location") || {
        echo 'chainman: cannot resolve host Git attributes; update Git before using container hooks.' >&2
        exit 2
    }
    if [ -e "$path" ]; then
        [ -f "$path" ] && [ -r "$path" ] || {
            echo 'chainman: external Git attributes must be a readable regular file.' >&2
            exit 2
        }
        cat -- "$path" >> "$output"
        printf '\n' >> "$output"
    fi
done
