#!/bin/sh
# Snapshot external attribute data without copying host configuration or drivers.
set -eu
if [ "${1:-}" = --check ]; then
    if ! GIT_ATTR_NOSYSTEM=0 git var GIT_ATTR_SYSTEM > /dev/null 2>&1 \
        || ! git -c core.attributesFile=/dev/null var GIT_ATTR_GLOBAL > /dev/null 2>&1; then
        echo 'chainman: container mode requires Git 2.42+ with attribute-location queries; update host Git before retrying.' >&2
        exit 2
    fi
    exit 0
fi
cd -- "$1"
output=$2
scope=${3:-repository}
g() {
    if [ "$scope" = global ]; then git --git-dir=/dev/null "$@"; else git "$@"; fi
}
: > "$output"
for location in GIT_ATTR_SYSTEM GIT_ATTR_GLOBAL; do
    status=0
    path=$(g var "$location") || status=$?
    # A supported query returns no value when that attribute source is disabled.
    if [ "$status" = 1 ] && [ -z "$path" ]; then continue; fi
    [ "$status" = 0 ] || {
        echo 'chainman: cannot resolve host Git attribute policy.' >&2
        exit 2
    }
    case "$path" in '' | /dev/null) continue ;; esac
    if [ -e "$path" ]; then
        [ -f "$path" ] && [ -r "$path" ] || {
            echo 'chainman: external Git attributes must be a readable regular file.' >&2
            exit 2
        }
        cat -- "$path" >> "$output"
        printf '\n' >> "$output"
    fi
done
