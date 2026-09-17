#!/bin/sh
# General temporary-file supervision; container entry owns its prompt relay.
set -eu
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=bootstrap/lifetime.sh
. "$script_dir/lifetime.sh"
lifetime_directory=$1
shift
lifetime_run "$@"
