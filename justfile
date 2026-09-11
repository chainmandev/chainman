set positional-arguments
set shell := ["sh", "-eu", "-c"]

default:
    @just --list

exec +args:
    @./scripts/enter.sh core python3 scripts/toolchain.py exec -- "$@"

exec-in profile +args:
    @profile=$1; shift; ./scripts/enter.sh "$profile" python3 scripts/toolchain.py exec -- "$@"

setup:
    @./scripts/enter.sh core python3 scripts/toolchain.py setup

build:
    @./scripts/enter.sh core python3 scripts/toolchain.py build

test:
    @./scripts/enter.sh core python3 scripts/toolchain.py test

verify:
    @./scripts/enter.sh core python3 scripts/toolchain.py verify

format:
    @./scripts/enter.sh core python3 scripts/toolchain.py format

format-check:
    @./scripts/enter.sh core python3 scripts/format.py --check

module name action="verify":
    @./scripts/enter.sh core python3 scripts/toolchain.py module "$@"

deps-update *args:
    @./scripts/enter.sh core python3 scripts/updates.py "$@"

cache-status:
    @./scripts/enter.sh core python3 scripts/toolchain.py cache-status

cache-prune *args:
    @./scripts/enter.sh core python3 scripts/toolchain.py cache-prune "$@"

clean:
    @./scripts/enter.sh core python3 scripts/toolchain.py clean

doctor:
    @./scripts/enter.sh core python3 scripts/toolchain.py doctor

ci-prune *args:
    @./scripts/enter.sh core python3 scripts/ci_cleanup.py "$@"

sdk-doctor platform:
    @./scripts/enter.sh "$(case "$1" in apple) echo swift;; android) echo flutter;; *) exit 2;; esac)" python3 scripts/native_sdks.py "$1"

release output="dist/release":
    @./scripts/enter.sh core python3 scripts/package.py --output "$1"

example destination="dist/nix-just-toolchain":
    @./scripts/enter.sh core python3 scripts/example.py "$1"

bootstrap-test engine="docker":
    @command -v "$1" >/dev/null; ./scripts/enter.sh core env CHAINMAN_TEST_ENGINE_PATH="$PATH" CHAINMAN_TEST_CONTAINER="$1" sh -eu -c 'export PATH="$PATH:$CHAINMAN_TEST_ENGINE_PATH"; python3 -B -m unittest discover -s tests -p test_bootstrap.py -v'

control-test:
    @./scripts/enter.sh control python3 scripts/control_test.py
