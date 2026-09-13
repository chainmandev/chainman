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

generate:
    @./scripts/enter.sh core python3 scripts/generate.py

format *args:
    @./scripts/enter.sh core python3 scripts/source_workflow.py format "$@"

format-write:
    @./scripts/enter.sh core python3 scripts/format.py

format-staged:
    @./scripts/enter.sh core python3 scripts/source_workflow.py format --staged

format-check:
    @./scripts/enter.sh core python3 scripts/format.py --check

type-check:
    @./scripts/enter.sh core mypy --config-file mypy.ini --platform linux
    @./scripts/enter.sh core mypy --config-file mypy.ini --platform darwin

module name action="verify":
    @./scripts/enter.sh core python3 scripts/toolchain.py module "$@"

deps-update *args:
    @./scripts/enter.sh core python3 scripts/source_workflow.py deps-update "$@"

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
    @case "$1" in apple) profile=swift;; android) profile=flutter;; *) exit 2;; esac; ./scripts/enter.sh "$profile" python3 scripts/native_sdks.py "$1"

release output="dist/release":
    @./scripts/enter.sh core python3 scripts/package.py --output "$1"

example destination="dist/nix-just-toolchain":
    @./scripts/enter.sh core python3 scripts/example.py "$1"

bootstrap-test engine="docker":
    @command -v "$1" >/dev/null; ./scripts/enter.sh core env CHAINMAN_TEST_ENGINE_PATH="$PATH" CHAINMAN_TEST_CONTAINER="$1" sh -eu -c 'export PATH="$PATH:$CHAINMAN_TEST_ENGINE_PATH"; python3 -B -m unittest discover -s tests -p test_bootstrap.py -v'

control-test:
    @./scripts/enter.sh control python3 scripts/control_test.py

javascript-test:
    @./scripts/enter.sh javascript env CHAINMAN_TEST_PNPM=1 python3 -B -m unittest discover -s tests -p 'test_javascript*.py' -v
    @./scripts/enter.sh javascript python3 -B -m unittest discover -s tests -p test_pnpm_runtime.py -v

python-test:
    @./scripts/enter.sh python env CHAINMAN_TEST_UV=1 python3 -B -m unittest discover -s tests -p test_python_native.py -v

rust-test:
    @./scripts/enter.sh rust env CHAINMAN_TEST_CARGO=1 python3 -B -m unittest discover -s tests -p test_cargo_native.py -v

swift-test:
    @./scripts/enter.sh swift env CHAINMAN_TEST_SWIFT=1 python3 -B -m unittest discover -s tests -p test_swift_native.py -v

gradle-test:
    @./scripts/enter.sh compose env CHAINMAN_TEST_GRADLE=1 python3 -B -m unittest discover -s tests -p test_gradle_resolution.py -v

# Static consumer validation; never executes project workflows.
consumer-check +args:
    @./scripts/enter.sh core python3 scripts/consumer_contract.py "$@"

# Maturity gate for publication; release itself builds a local candidate artifact.
control-release-check:
    @./scripts/enter.sh core python3 scripts/control_release.py
